import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from hawkdrop.config import Config
from hawkdrop.server import ServerContext, make_server


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
        self.assertEqual(self.call("GET", "/api/snapshot", headers={"X-HawkDrop-Token": "wrong"})[0], 401)
        self.assertEqual(self.call("GET", "/api/snapshot", headers={"X-HawkDrop-Token": "s3cret"})[0], 200)
        self.assertEqual(self.call("GET", "/api/snapshot?token=s3cret")[0], 200)
        self.assertEqual(self.call("GET", "/")[0], 200)

    def test_health_is_public(self):
        status, body = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])


class BasePathTest(ServerTest):
    base_path = "/hawkdrop"

    def test_prefixed_routes(self):
        self.assertEqual(self.call("GET", "/hawkdrop/api/health")[0], 200)
        self.assertEqual(self.call("GET", "/hawkdrop/")[0], 200)
        self.assertEqual(self.call("GET", "/hawkdrop/sw.js")[0], 200)
        self.assertEqual(self.call("GET", "/api/health")[0], 404)
        req = urllib.request.Request(self.base + "/hawkdrop?token=x")
        opener = urllib.request.build_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            opener.open(req)
        self.assertEqual(cm.exception.code, 308)
        self.assertEqual(cm.exception.headers["Location"], "/hawkdrop/?token=x")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    unittest.main()
