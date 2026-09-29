import json
import os
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

from hawksense.config import Config
from hawksense.fetch import FetchError
from hawksense.server import ServerContext, make_server


class ServerTest(unittest.TestCase):
    token = None
    base_path = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ctx = ServerContext(Path(self.tmp.name) / "s.db", Config(), offline_fx=True, token=self.token,
                            base_path=self.base_path)
        self.httpd = make_server(ctx, "127.0.0.1", 0, quiet=True)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as res:
                raw = res.read()
                return res.status, (json.loads(raw) if raw and "json" in res.headers["Content-Type"] else raw)
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw or b"{}")
            except ValueError:
                return err.code, raw


class ApiTest(ServerTest):
    def test_item_lifecycle_and_idempotent_replay(self):
        status, item = self.call("POST", "/api/items", {"name": "Kindle", "category": "electronics", "target_price": 400})
        self.assertEqual(status, 201)
        # an offline client may replay the create - same item comes back
        _, again = self.call("POST", "/api/items", {"name": "kindle"})
        self.assertEqual(again["id"], item["id"])

        body = {"store": "ksp", "price": 499, "client_id": "abc-1"}
        _, detail = self.call("POST", f"/api/items/{item['id']}/prices", body)
        self.call("POST", f"/api/items/{item['id']}/prices", body)  # replayed
        self.assertEqual(len(detail["quotes"]), 1)
        self.assertEqual(detail["quotes"][0]["landed"]["total"], 499)
        self.assertIn(detail["advice"]["action"], ("BUY_NOW", "WAIT"))

        snap = self.call("GET", "/api/snapshot")[1]
        self.assertEqual(len(snap["items"]), 1)
        self.assertEqual(snap["meta"]["destination"]["currency"], "ILS")
        self.assertTrue(snap["events"])
        # replay did not add a second price row
        self.assertEqual(len(snap["items"][0]["history"]), 1)

        _, patched = self.call("PATCH", f"/api/items/{item['id']}", {"target_price": None})
        self.assertIsNone(patched["target_price"])
        self.assertEqual(self.call("DELETE", f"/api/items/{item['id']}")[0], 200)
        self.assertEqual(self.call("DELETE", f"/api/items/{item['id']}")[0], 200)  # replay is harmless
        self.assertEqual(self.call("GET", f"/api/items/{item['id']}")[0], 404)

    def test_item_picture(self):
        _, item = self.call("POST", "/api/items", {"name": "Camera"})
        self.assertIsNone(item["image_url"])
        status, err = self.call("PATCH", f"/api/items/{item['id']}", {"image_url": "not a url"})
        self.assertEqual(status, 400)
        _, patched = self.call("PATCH", f"/api/items/{item['id']}", {"image_url": "https://cdn.example.com/x.jpg"})
        self.assertEqual(patched["image_url"], "https://cdn.example.com/x.jpg")
        _, cleared = self.call("PATCH", f"/api/items/{item['id']}", {"image_url": ""})
        self.assertIsNone(cleared["image_url"])

    def test_search_all_stores(self):
        _, item = self.call("POST", "/api/items", {"name": "Sony WH-1000XM5", "search_all_stores": True})
        self.assertGreater(len(item["offers"]), 0)
        self.assertTrue(all(o["url"] for o in item["offers"]))
        # explicit search on an existing item skips stores it already has
        _, again = self.call("POST", f"/api/items/{item['id']}/search", {"check": False})
        self.assertEqual(len(again["offers"]), len(item["offers"]))

    def test_search_candidates(self):
        from tests.test_ebay import SEARCH_PAGE

        self.assertEqual(self.call("POST", "/api/search", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/search", {"query": "x", "condition": "bogus"})[0], 400)
        with unittest.mock.patch("hawksense.ebay.fetch_html", return_value=SEARCH_PAGE):
            status, res = self.call("POST", "/api/search", {"query": "headphones"})
        self.assertEqual(status, 200)
        self.assertEqual([r["title"] for r in res["results"]], ["Headphones B", "Headphones A"])
        self.assertEqual(res["results"][0]["store"], "eBay")
        with unittest.mock.patch("hawksense.ebay.fetch_html", side_effect=FetchError("blocked")):
            self.assertEqual(self.call("POST", "/api/search", {"query": "headphones"})[0], 502)

    def test_validation(self):
        self.assertEqual(self.call("POST", "/api/items", {"name": ""})[0], 400)
        _, item = self.call("POST", "/api/items", {"name": "X"})
        status, err = self.call("POST", f"/api/items/{item['id']}/prices", {"store": "ksp", "price": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("price", err["error"])
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)

    def test_forwarder_accounts_and_routes(self):
        _, catalog = self.call("GET", "/api/forwarders")
        self.assertIn("dealtas", [f["key"] for f in catalog["services"]])
        status, err = self.call("POST", "/api/forwarders/accounts", {"forwarder": "dealtas", "warehouse": "US"})
        self.assertEqual(status, 400)
        self.assertIn("address", err["error"])
        body = {"forwarder": "dealtas", "warehouse": "US", "address": "Me, 1 Rd #IL9, New Castle, DE 19720"}
        _, res = self.call("POST", "/api/forwarders/accounts", body)
        self.call("POST", "/api/forwarders/accounts", body)  # replayed offline change
        self.assertEqual(len(res["accounts"]), 1)
        self.assertEqual(res["accounts"][0]["sales_tax_source"], "DE address")

        _, item = self.call("POST", "/api/items", {"name": "SSD", "category": "computers"})
        status, item = self.call("PATCH", f"/api/items/{item['id']}", {"weight_kg": 0.3, "dims": "20 x 15 x 5"})
        self.assertEqual((item["weight_kg"], item["dims"]), (0.3, "20x15x5"))
        self.assertEqual(self.call("PATCH", f"/api/items/{item['id']}", {"dims": "20x15"})[0], 400)
        _, detail = self.call("POST", f"/api/items/{item['id']}/prices",
                              {"store": "https://www.newegg.com/p/1", "price": 120, "currency": "USD"})
        best = detail["quotes"][0]["landed"]
        self.assertEqual(best["route"], "dealtas:US")
        self.assertTrue(best["lines"])
        _, snap = self.call("GET", "/api/snapshot")
        self.assertEqual(len(snap["forwarders"]["accounts"]), 1)

        _, res = self.call("DELETE", "/api/forwarders/accounts/dealtas/US")
        self.assertEqual(res["accounts"], [])
        self.assertEqual(self.call("DELETE", "/api/forwarders/accounts/dealtas/US")[0], 200)
        _, detail = self.call("GET", f"/api/items/{item['id']}")
        self.assertEqual(detail["quotes"][0]["landed"]["route"], "direct")
        _, cleared = self.call("PATCH", f"/api/items/{item['id']}", {"weight_kg": ""})
        self.assertIsNone(cleared["weight_kg"])

    def test_rules_endpoints(self):
        _, summary = self.call("GET", "/api/rules")
        self.assertEqual(summary["destination"], "IL")
        self.assertTrue(any(v["path"] == "destination.IL.vat_rate" for v in summary["values"]))
        status, err = self.call("POST", "/api/rules/manual", {"changes": {"IL.vat_rate": 5}})
        self.assertEqual(status, 400)
        _, summary = self.call("POST", "/api/rules/manual", {"changes": {"IL.vat_exempt_usd": 150}})
        row = next(v for v in summary["values"] if v["path"] == "destination.IL.vat_exempt_usd")
        self.assertEqual((row["value"], row["from"]), (150, "manual"))
        _, meta = self.call("GET", "/api/meta")
        self.assertEqual(meta["destination"]["vat_exempt_usd"], 150)  # used for pricing right away
        self.assertEqual(self.call("POST", "/api/rules/changes/999/accept")[0], 200)  # replay-safe

    def test_notification_endpoints(self):
        _, n = self.call("GET", "/api/notifications")
        self.assertEqual(n["unread"], 0)
        self.assertIn("telegram", [c["key"] for c in n["channels"]])
        _, n = self.call("PUT", "/api/notify/settings", {"subscriptions": {"buy_now": ["inbox", "telegram"]},
                                                         "settings": {"price_drop_pct": 8}})
        self.assertEqual(n["subscriptions"]["buy_now"], ["inbox", "telegram"])
        self.assertEqual(n["settings"]["price_drop_pct"], 8)
        self.assertEqual(self.call("PUT", "/api/notify/settings", {"subscriptions": {"x": []}})[0], 400)
        self.assertEqual(self.call("POST", "/api/notify/test", {"channel": "telegram"})[0], 400)  # not configured
        self.call("POST", "/api/notify/test", {"channel": "inbox"})
        _, n = self.call("GET", "/api/notifications")
        self.assertEqual(n["unread"], 1)
        _, n = self.call("POST", "/api/notifications/read", {})
        self.assertEqual(n["unread"], 0)
        _, snap = self.call("GET", "/api/snapshot")
        self.assertIn("notifications", snap)
        self.assertIn("rules", snap)

    def test_item_specs_and_mute(self):
        _, item = self.call("POST", "/api/items", {"name": "Mouse"})
        self.assertIsNone(item["specs"]["status"])
        _, item = self.call("PATCH", f"/api/items/{item['id']}", {"muted": True, "weight_kg": 0.2})
        self.assertTrue(item["muted"])
        self.assertEqual(item["specs"]["weight_source"], "manual")

    def test_settings_endpoints(self):
        _, view = self.call("GET", "/api/settings")
        self.assertIn("telegram", [s["key"] for s in view["sections"]])
        status, err = self.call("PUT", "/api/settings", {"changes": {"advisor.max_wait_days": 9999}})
        self.assertEqual(status, 400)
        _, view = self.call("PUT", "/api/settings", {"changes": {
            "destination.code": "US", "notify.telegram.bot_token": "123:secret", "notify.telegram.chat_id": "7"}})
        self.assertNotIn("123:secret", json.dumps(view))
        self.assertTrue(next(s for s in view["sections"] if s["key"] == "telegram")["configured"])
        _, meta = self.call("GET", "/api/meta")
        self.assertEqual(meta["destination"]["currency"], "USD")  # applies without a restart
        _, n = self.call("GET", "/api/notifications")
        self.assertTrue(next(c for c in n["channels"] if c["key"] == "telegram")["configured"])
        _, snap = self.call("GET", "/api/snapshot")
        self.assertIn("settings", snap)

    def test_demo_has_history_and_windows(self):
        _, item = self.call("POST", "/api/demo")
        self.assertGreater(len(item["history"]), 100)
        self.assertTrue(item["event_windows"])
        self.assertEqual(item["advice"]["action"], item["advice"]["action"].upper())

    def test_static_files_and_spa_fallback(self):
        status, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"apple-mobile-web-app-capable", body)
        self.assertEqual(self.call("GET", "/sw.js")[0], 200)
        self.assertEqual(self.call("GET", "/icons/apple-touch-icon.png")[0], 200)
        self.assertEqual(self.call("GET", "/%2e%2e/server.py")[0], 404)
        self.assertEqual(self.call("GET", "/missing.js")[0], 404)

    def test_manifest_carries_token(self):
        req = urllib.request.urlopen(self.base + "/manifest.webmanifest?token=s3cret")
        self.assertEqual(json.loads(req.read())["start_url"], "./?token=s3cret")


    def test_security_headers(self):
        res = urllib.request.urlopen(self.base + "/")
        self.assertEqual(res.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("script-src 'self'", res.headers["Content-Security-Policy"])
        self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")


class TokenTest(ServerTest):
    token = "s3cret"

    def test_token_required_for_api_only(self):
        self.assertEqual(self.call("GET", "/api/snapshot")[0], 401)
        self.assertEqual(self.call("GET", "/api/snapshot", headers={"X-HawkSense-Token": "wrong"})[0], 401)
        self.assertEqual(self.call("GET", "/api/snapshot", headers={"X-HawkSense-Token": "s3cret"})[0], 200)
        self.assertEqual(self.call("GET", "/api/snapshot?token=s3cret")[0], 200)
        self.assertEqual(self.call("GET", "/")[0], 200)

    def test_health_is_public(self):
        status, body = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])


class RequestForgeryTest(ServerTest):
    """A web page you visit must not be able to drive a token-less local server."""

    def test_simple_cross_site_posts_are_refused(self):
        status, _ = self.call("POST", "/api/items", {"name": "x"}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _ = self.call("POST", "/api/items", {"name": "x"}, {"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        status, _ = self.call("POST", "/api/items", {"name": "x"}, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.call("POST", "/api/items", {"name": "x"}, {"Origin": "null"})
        self.assertEqual(status, 403)
        _, snap = self.call("GET", "/api/snapshot")
        self.assertEqual(snap["items"], [])

    def test_same_origin_and_non_browser_clients_work(self):
        host = self.base.split("//")[1]
        status, _ = self.call("POST", "/api/items", {"name": "a"},
                              {"Sec-Fetch-Site": "same-origin", "Origin": f"http://{host}"})
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/api/items", {"name": "b"}, {"Origin": f"http://{host}"})  # older browsers
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/api/items", {"name": "c"})  # curl, scripts
        self.assertEqual(status, 201)

    def test_dns_rebinding_is_refused(self):
        status, body = self.call("GET", "/api/snapshot", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("HAWKSENSE_ALLOWED_HOSTS", body["error"])
        for host in ("localhost:8765", "127.0.0.1", "[::1]:8765", "192.168.1.20", "raspberrypi.local",
                     "nas", "hawksense.home.arpa"):
            self.assertEqual(self.call("GET", "/api/meta", headers={"Host": host})[0], 200, host)

    def test_javascript_links_are_refused(self):
        status, body = self.call("POST", "/api/items", {"name": "x", "urls": ["javascript:alert(1)//"]})
        self.assertEqual(status, 400)
        _, item = self.call("POST", "/api/items", {"name": "y"})
        status, _ = self.call("POST", f"/api/items/{item['id']}/offers", {"url": "data:text/html,<script>"})
        self.assertEqual(status, 400)
        status, _ = self.call("POST", f"/api/items/{item['id']}/prices", {"store": "javascript:x//", "price": 1})
        self.assertEqual(status, 400)

    def test_internal_errors_do_not_leak_details(self):
        with unittest.mock.patch("hawksense.api.meta", side_effect=RuntimeError("/secret/path/db")):
            status, body = self.call("GET", "/api/meta")
        self.assertEqual(status, 500)
        self.assertNotIn("secret", json.dumps(body))


class TokenHardeningTest(ServerTest):
    token = "S3CRET-TOKEN"

    def test_token_never_reaches_the_log(self):
        from hawksense.server import Handler

        lines = []
        with unittest.mock.patch("http.server.BaseHTTPRequestHandler.log_message",
                                 lambda self, fmt, *args: lines.append(fmt % args)):
            h = Handler.__new__(Handler)
            Handler.log_message(h, '"%s" %s', "GET /?token=S3CRET-TOKEN&x=1 HTTP/1.1", "200")
        self.assertNotIn("S3CRET", lines[0])
        self.assertIn("token=[redacted]&x=1", lines[0])

    def test_any_host_is_fine_with_a_token(self):
        status, _ = self.call("GET", "/api/meta", headers={"Host": "hawksense.example.com",
                                                           "X-HawkSense-Token": self.token})
        self.assertEqual(status, 200)

    def test_wrong_tokens_are_rate_limited(self):
        codes = [self.call("GET", "/api/meta", headers={"X-HawkSense-Token": "nope"})[0] for _ in range(21)]
        self.assertEqual(codes[:20], [401] * 20)
        self.assertEqual(codes[20], 429)


class BasePathTest(ServerTest):
    base_path = "/hawksense"

    def test_prefixed_routes(self):
        self.assertEqual(self.call("GET", "/hawksense/api/health")[0], 200)
        self.assertEqual(self.call("GET", "/hawksense/")[0], 200)
        self.assertEqual(self.call("GET", "/hawksense/sw.js")[0], 200)
        self.assertEqual(self.call("GET", "/api/health")[0], 404)
        req = urllib.request.Request(self.base + "/hawksense?token=x")
        opener = urllib.request.build_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            opener.open(req)
        self.assertEqual(cm.exception.code, 308)
        self.assertEqual(cm.exception.headers["Location"], "/hawksense/?token=x")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    unittest.main()
