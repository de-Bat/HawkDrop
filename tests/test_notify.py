import json
import tempfile
import unittest
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

from hawksense.db import Database
from hawksense.landed import DESTINATIONS
from hawksense.notify import Email, Notifier, NotifyError
from hawksense.rules import Change, Report
from hawksense.tracker import CheckResult, Tracker
from tests.test_landed import StubFX

CFG = {
    "app_url": "https://hd.example",
    "telegram": {"bot_token": "T0K", "chat_id": "42"},
    "whatsapp": {"phone": "+972500000000", "apikey": "k"},
    "ntfy": {"topic": "my topic"},
    "webhook": {"url": "https://hook.example/x"},
    "email": {"host": "smtp.example", "port": 587, "username": "me", "password": "pw", "to": "me@example.com"},
}


class FakeHttp:
    def __init__(self, fail=()):
        self.calls, self.fail = [], fail

    def __call__(self, method, url, headers, body):
        if any(f in url for f in self.fail):
            raise NotifyError("HTTP 500: boom")
        self.calls.append((method, url, headers, body))


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "n.db")
        self.http = FakeHttp()
        self.n = Notifier(self.db, CFG, http=self.http, env={})
        Email.smtp_factory = FakeSMTP
        FakeSMTP.sent = []

    def tearDown(self):
        Email.smtp_factory = None
        self.db.close()
        self.tmp.cleanup()

    def test_defaults_inbox_only(self):
        self.assertTrue(all(v == ["inbox"] for v in self.n.subscriptions().values()))
        nid = self.n.emit("buy_now", "Buy now: X", "cheap", dedup="k1", item_id=3)
        self.assertEqual(self.http.calls, [])
        [row] = self.db.notifications()
        self.assertEqual((row["id"], row["read"], row["url"]), (nid, False, "https://hd.example/#/item/3"))
        self.assertIsNone(self.n.emit("buy_now", "Buy now: X", dedup="k1"))  # deduplicated

    def test_every_channel(self):
        self.n.subscribe("buy_now", ["inbox", "email", "telegram", "whatsapp", "ntfy", "webhook"])
        nid = self.n.emit("buy_now", "Buy now: שלום", "at KSP", dedup="k")
        urls = [c[1] for c in self.http.calls]
        self.assertTrue(any("api.telegram.org/botT0K/sendMessage" in u for u in urls))
        self.assertTrue(any("callmebot.com" in u and "apikey=k" in u for u in urls))
        self.assertTrue(any(u == "https://ntfy.sh/my%20topic" for u in urls))
        self.assertIn("https://hook.example/x", urls)
        tg = next(c for c in self.http.calls if "telegram" in c[1])
        self.assertEqual(json.loads(tg[3])["chat_id"], "42")
        self.assertEqual(FakeSMTP.sent[0]["Subject"], "HawkSense: Buy now: שלום")
        ntfy = next(c for c in self.http.calls if "ntfy" in c[1])
        ntfy[2]["Title"].encode("latin-1")  # header-safe
        deliveries = self.db.notifications()[0]["deliveries"]
        self.assertEqual(set(deliveries.values()), {"sent"})
        self.assertEqual(nid, self.db.notifications()[0]["id"])

    def test_failures_and_unconfigured_are_recorded(self):
        n = Notifier(self.db, {"telegram": CFG["telegram"]}, http=FakeHttp(fail=("telegram",)), env={})
        n.subscribe("price_drop", ["telegram", "email"])
        n.emit("price_drop", "drop", dedup="d")
        row = self.db.notifications()[0]
        self.assertTrue(row["deliveries"]["telegram"].startswith("failed"))
        self.assertEqual(row["deliveries"]["email"], "not configured")
        self.assertTrue(row["read"])  # not subscribed to the inbox -> history only

    def test_twilio_whatsapp_and_env_secrets(self):
        env = {"HAWKSENSE_WHATSAPP_AUTH_TOKEN": "secret"}
        n = Notifier(self.db, {"whatsapp": {"provider": "twilio", "account_sid": "AC1", "sender": "+1555",
                                            "to": "+97250"}}, http=self.http, env=env)
        self.assertTrue(n.channels["whatsapp"].configured)
        n.test("whatsapp")
        method, url, headers, body = self.http.calls[-1]
        self.assertIn("/Accounts/AC1/Messages.json", url)
        self.assertEqual(urllib.parse.parse_qs(body.decode())["To"], ["whatsapp:+97250"])

    def test_subscription_validation_and_off(self):
        with self.assertRaises(ValueError):
            self.n.subscribe("nope", ["inbox"])
        with self.assertRaises(ValueError):
            self.n.subscribe("buy_now", ["pigeon"])
        self.n.subscribe("sale_soon", [])
        self.assertEqual(self.n.subscriptions()["sale_soon"], [])
        with self.assertRaises(NotifyError):
            Notifier(self.db, {}, env={}).test("telegram")

    def test_settings(self):
        self.n.update_settings({"price_drop_pct": 10})
        self.assertEqual(self.n.settings()["price_drop_pct"], 10)
        with self.assertRaises(ValueError):
            self.n.update_settings({"price_drop_pct": -1})


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "e.db")
        self.t = Tracker(self.db, StubFX(), DESTINATIONS["IL"])
        self.t.notifier = Notifier(self.db, {}, env={})
        self.item = self.db.add_item("Kindle", "electronics", target_price=400)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def events(self):
        return [n["event"] for n in self.db.notifications()]

    def price(self, value, days_ago=0):
        when = date.today() - timedelta(days=days_ago)
        self.t.record_price(self.item, "ksp", value, shipping=0, when=when)

    def test_price_drop_target_and_no_repeats(self):
        self.price(500, 3)
        self.t.notifier.evaluate(self.t)
        self.assertNotIn("price_drop", self.events())
        self.price(470, 1)  # -6% from 500
        self.t.notifier.evaluate(self.t)
        self.assertEqual(self.events().count("price_drop"), 1)
        self.t.notifier.evaluate(self.t)
        self.assertEqual(self.events().count("price_drop"), 1)
        self.price(390)
        self.t.notifier.evaluate(self.t)
        self.assertIn("target_hit", self.events())
        self.t.notifier.evaluate(self.t)
        self.assertEqual(self.events().count("target_hit"), 1)

    def test_muted_item_is_quiet(self):
        self.db.update_item(self.item, muted=True)
        self.price(500, 3)
        self.price(300)
        self.t.notifier.evaluate(self.t)
        self.assertEqual(self.events(), [])

    def test_specs_alert(self):
        self.t.add_offer(self.item, "https://ksp.co.il/x")
        self.price(500)
        self.t._save_specs(self.item, "KSP", "https://ksp.co.il/x", None)
        self.t.notifier.evaluate(self.t)
        self.assertIn("specs_alert", self.events())
        alert = next(n for n in self.db.notifications() if n["event"] == "specs_alert")
        self.assertIn("couldn't find the weight", alert["title"])
        self.assertIn("on hold", alert["body"])

    def test_values_to_confirm_are_notified_once(self):
        from hawksense.specs import Specs
        self.t.add_offer(self.item, "https://ksp.co.il/x")
        self.price(500)
        self.t._save_specs(self.item, "Amazon", "https://www.amazon.com/a", Specs(1.0, None, "package"))
        self.t._save_specs(self.item, "B&H", "https://www.bhphotovideo.com/b", Specs(1.1, None, "package"))
        self.t._save_specs(self.item, "KSP", "https://ksp.co.il/x", None)
        self.t.notifier.evaluate(self.t)
        self.t.notifier.evaluate(self.t)
        alerts = [n for n in self.db.notifications() if n["event"] == "specs_alert"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["title"], f"{self.item.name}: please confirm the weight and size")
        self.assertIn("Amazon (1 kg boxed) and B&H (1.1 kg boxed) agree; KSP doesn't list a weight.",
                      alerts[0]["body"])
        self.assertIn("--confirm", alerts[0]["body"])

    def test_check_failed_after_three(self):
        offer = self.t.add_offer(self.item, "https://ksp.co.il/x")
        store = self.t.store_for(offer)
        bad = [CheckResult(offer, store, error="blocked")]
        for _ in range(2):
            self.t.notifier.record_check(self.t, self.item, bad)
        self.assertEqual(self.events(), [])
        self.t.notifier.record_check(self.t, self.item, bad)
        self.assertEqual(self.events(), ["check_failed"])

    def test_sale_soon(self):
        from hawksense.calendar_events import upcoming_events

        occ = next(o for o in upcoming_events(date.today(), 400) if o.event.key == "black_friday"
                   and o.start > date.today())
        self.price(500)
        self.t.notifier.update_settings({"sale_soon_days": 5})
        self.t.notifier.evaluate(self.t, today=occ.start - timedelta(days=2))
        sale = [n for n in self.db.notifications() if n["event"] == "sale_soon"]
        self.assertTrue(any("Black Friday" in n["title"] for n in sale))
        self.assertIn("Kindle", sale[0]["body"])

    def test_rules_report(self):
        report = Report(applied=[Change("destination.IL.clearance_fee", 35.0, 40.0, "feed", id=1)],
                        pending=[Change("destination.IL.vat_exempt_usd", 75.0, 150.0, "feed", id=2, note="big")])
        self.t.notifier.rules_report(report)
        self.assertEqual(sorted(self.events()), ["rules_changed", "rules_review"])
        self.assertEqual(self.t.notifier.rules_report(report), [])  # once


if __name__ == "__main__":
    unittest.main()
