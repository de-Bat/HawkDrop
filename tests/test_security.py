import base64
import gzip
import http.server
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from hawksense import netguard, settings
from hawksense.config import Config
from hawksense.db import Database
from hawksense.fetch import FetchError, fetch_html
from hawksense.rules import _fetch_json
from hawksense.vault import PREFIX, Vault, VaultError, master_key


class GuardTest(unittest.TestCase):
    def blocked(self, url):
        with self.assertRaises(netguard.BlockedAddress, msg=url):
            netguard.check_url(url)

    def test_private_and_odd_addresses_are_refused(self):
        for url in ["http://127.0.0.1/", "http://localhost:8765/api/snapshot", "http://10.1.2.3/",
                    "http://192.168.1.1/", "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/",
                    "http://100.64.0.1/", "http://0.0.0.0/", "http://[::1]/", "http://[fe80::1]/",
                    "http://[::ffff:127.0.0.1]/", "http://2130706433/", "http://0x7f.1/", "http://224.0.0.1/"]:
            self.blocked(url)

    def test_only_http_and_https(self):
        for url in ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/", "data:text/html,hi", "//x"]:
            self.blocked(url)

    def test_public_address_allowed(self):
        with mock.patch.object(netguard.socket, "getaddrinfo",
                               return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]):
            netguard.check_url("https://shop.example/p/1")

    def test_name_resolving_to_a_private_address(self):
        with mock.patch.object(netguard.socket, "getaddrinfo",
                               return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 80))]):
            self.blocked("http://innocent-looking.example/")

    def test_opt_in_for_local_shops(self):
        with mock.patch.dict(os.environ, {"HAWKSENSE_ALLOW_PRIVATE_FETCH": "1"}):
            netguard.check_url("http://192.168.1.20/shop")

    def test_redirect_to_a_private_address_is_refused(self):
        handler = netguard._RedirectHandler()
        with self.assertRaises(netguard.BlockedAddress):
            handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/")

    def test_fetchers_report_refusals(self):
        with self.assertRaisesRegex(FetchError, "won't fetch"):
            fetch_html("http://127.0.0.1:1/")
        with self.assertRaisesRegex(FetchError, "rules feed"):
            _fetch_json("file:///etc/passwd")

    def test_size_limit_and_zip_bomb(self):
        class Resp:
            def __init__(self, body, enc=""):
                self.body, self.headers = io.BytesIO(body), {"Content-Encoding": enc}

            def read(self, n):
                return self.body.read(n)

        with mock.patch.object(netguard, "MAX_BYTES", 1000):
            with self.assertRaises(netguard.BlockedAddress):
                netguard._read_limited(Resp(b"x" * 1001))
            bomb = gzip.compress(b"\0" * 100_000)  # tiny on the wire, huge unpacked
            self.assertLess(len(bomb), 1000)
            with self.assertRaises(netguard.BlockedAddress):
                netguard._read_limited(Resp(bomb, "gzip"))
            self.assertEqual(netguard._read_limited(Resp(gzip.compress(b"<p>ok</p>"), "gzip")), b"<p>ok</p>")


class _Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html>secret admin page</html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class RebindingTest(unittest.TestCase):
    """The name looks public when checked, but the connection lands on a local server."""

    def test_connection_to_a_local_address_is_refused(self):
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _Page)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        real = socket.getaddrinfo
        calls = []

        def rebinding(host, *a, **kw):
            calls.append(host)
            if len(calls) == 1:  # the guard's check sees a public address...
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 80))]
            return real("127.0.0.1", *a, **kw)  # ...the actual connection goes local

        try:
            with mock.patch.object(netguard.urllib.request, "getproxies", return_value={}), \
                    mock.patch.object(socket, "getaddrinfo", side_effect=rebinding):
                with self.assertRaisesRegex(netguard.BlockedAddress, "connected to a private"):
                    netguard.fetch(f"http://rebind.example:{httpd.server_address[1]}/")
        finally:
            httpd.shutdown()
            httpd.server_close()


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("HAWKSENSE_SECRET_KEY", "HAWKSENSE_SECRET_KEY_FILE"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_round_trip_and_randomness(self):
        v = Vault(b"k" * 32)
        a, b = v.encrypt("123:TOKEN-שלום"), v.encrypt("123:TOKEN-שלום")
        self.assertTrue(a.startswith(PREFIX))
        self.assertNotEqual(a, b)  # random nonce
        self.assertNotIn("TOKEN", a)
        self.assertEqual(v.decrypt(a), "123:TOKEN-שלום")
        self.assertEqual(v.decrypt(v.encrypt("")), "")

    def test_tampering_and_wrong_key_are_detected(self):
        v = Vault(b"k" * 32)
        token = v.encrypt("secret")
        blob = bytearray(base64.urlsafe_b64decode(token[len(PREFIX):]))
        blob[20] ^= 1
        forged = PREFIX + base64.urlsafe_b64encode(bytes(blob)).decode()
        with self.assertRaises(VaultError):
            v.decrypt(forged)
        with self.assertRaises(VaultError):
            Vault(b"x" * 32).decrypt(token)
        with self.assertRaises(VaultError):
            v.decrypt("plain text")

    def test_key_file_is_created_private_and_reused(self):
        db = Path(self.tmp.name) / "h.db"
        k1 = master_key(db)
        key_file = db.parent / "secret.key"
        self.assertTrue(key_file.exists())
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(master_key(db), k1)

    def test_key_from_environment(self):
        db = Path(self.tmp.name) / "h.db"
        with mock.patch.dict(os.environ, {"HAWKSENSE_SECRET_KEY": "from-env"}):
            self.assertEqual(master_key(db), master_key(Path(self.tmp.name) / "other" / "x.db"))
            self.assertFalse((db.parent / "secret.key").exists())


class EncryptedSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "s.db"
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_secrets_are_encrypted_at_rest_but_usable(self):
        settings.update(self.db, {"notify.telegram.bot_token": "123:SECRET", "notify.telegram.chat_id": "42"}, env={})
        raw = self.db.get_kv(settings.KV_KEY)
        self.assertNotIn("123:SECRET", raw)
        self.assertIn('"chat_id": "42"', raw)  # ordinary settings stay readable
        self.assertEqual(settings.effective(self.db, Config()).notify["telegram"]["bot_token"], "123:SECRET")
        backup = Path(self.tmp.name) / "backup.db"
        self.db.backup(backup)
        self.assertNotIn(b"123:SECRET", backup.read_bytes())

    def test_plaintext_from_older_versions_is_encrypted_on_first_read(self):
        self.db.set_kv(settings.KV_KEY, json.dumps({"ebay": {"client_secret": "PRD-plain"}}))
        self.assertEqual(settings.effective(self.db, Config()).ebay["client_secret"], "PRD-plain")
        self.assertNotIn("PRD-plain", self.db.get_kv(settings.KV_KEY))

    def test_lost_key_means_enter_it_again(self):
        settings.update(self.db, {"notify.telegram.bot_token": "123:SECRET"}, env={})
        self.db.close()
        (self.path.parent / "secret.key").unlink()  # e.g. a backup restored without its key
        self.db = Database(self.path)
        self.assertNotIn("bot_token", settings.effective(self.db, Config()).notify.get("telegram", {}))
        view = settings.view(self.db, Config(), env={})
        token = next(f for s in view["sections"] for f in s["fields"] if f["path"] == "notify.telegram.bot_token")
        self.assertTrue(token["unreadable"])
        self.assertFalse(token["is_set"])


class HostileContentTest(unittest.TestCase):
    """Pages come from anywhere: parsing must stay fast however broken the HTML is."""

    def fast(self, fn, page, limit=3.0):
        import time

        start = time.monotonic()
        try:
            fn(page)
        except FetchError:
            pass
        self.assertLess(time.monotonic() - start, limit, f"{fn.__name__} took too long")

    def test_unclosed_tags_parse_in_linear_time(self):
        from hawksense.ebay import parse_search_page
        from hawksense.fetch import extract_price
        from hawksense.specs import extract_specs

        n = 400_000  # the old parser needed minutes for pages like these
        self.fast(extract_specs, "<div>" * (n // 5))
        self.fast(extract_specs, "<dt>x</dt>" * (n // 10))
        self.fast(extract_specs, "<script>" * (n // 8))
        self.fast(extract_price, '<script type="application/ld+json">' * (n // 35))
        self.fast(lambda p: extract_price(p, "https://www.amazon.com/dp/X"), 'id="corePrice_feature_div"' * (n // 26))
        self.fast(lambda p: extract_price(p, "https://www.ebay.com/itm/1"), '<div class="x-price-primary">' * (n // 29))
        self.fast(parse_search_page, '<li class="s-item">' * (n // 19))

    def test_store_patterns_follow_the_host_not_the_path(self):
        from hawksense.fetch import extract_store_specific

        page = '<span class="a-price"><span class="a-offscreen">$12.00</span></span>'
        self.assertIsNotNone(extract_store_specific(page, "https://www.amazon.com/dp/X"))
        self.assertIsNone(extract_store_specific(page, "https://evil.example/amazon.html"))


class OfferLinkTest(unittest.TestCase):
    def test_only_web_links_and_store_names(self):
        from hawksense.tracker import clean_offer_ref

        self.assertEqual(clean_offer_ref(" https://ksp.co.il/item/1 "), "https://ksp.co.il/item/1")
        self.assertEqual(clean_offer_ref("shop.co.il/p/1"), "https://shop.co.il/p/1")
        self.assertEqual(clean_offer_ref("ivory"), "ivory")
        self.assertEqual(clean_offer_ref("Local shop"), "Local shop")
        for bad in ["javascript:alert(1)//", "JavaScript:alert(1)", "java\tscript:alert(1)", "data:text/html,x",
                    "vbscript:x", "//evil.example/x", "file:///etc/passwd", "https://", "", "x\ny"]:
            with self.assertRaises(ValueError, msg=bad):
                clean_offer_ref(bad)


class NotifyLeakTest(unittest.TestCase):
    def test_http_errors_do_not_echo_the_response_body(self):
        from hawksense import notify

        err = notify.urllib.error.HTTPError("http://x", 500, "Server Error", {}, io.BytesIO(b"INTERNAL SECRET PAGE"))
        with mock.patch.object(notify.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(notify.NotifyError) as ctx:
                notify._urllib_http("GET", "http://x", {}, None)
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_newline_in_an_item_name_does_not_break_email(self):
        from hawksense.notify import Email, Message

        sent = []

        class SMTP:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def send_message(self, m):
                sent.append(m)

        ch = Email({"host": "smtp.example", "to": "me@example.com"}, None, {})
        with mock.patch.object(Email, "smtp_factory", SMTP):
            ch.send(Message("buy_now", "Buy now: Evil\nBcc: victim@example.com"))
        self.assertEqual(sent[0]["Subject"], "HawkSense: Buy now: Evil Bcc: victim@example.com")
        self.assertIsNone(sent[0]["Bcc"])


class FilePermissionTest(unittest.TestCase):
    def test_database_and_backups_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "p.db")
            db.backup(Path(tmp) / "b.db")
            db.close()
            for name in ("p.db", "b.db"):
                self.assertEqual((Path(tmp) / name).stat().st_mode & 0o777, 0o600, name)


if __name__ == "__main__":
    unittest.main()
