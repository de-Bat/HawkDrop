import tempfile
import unittest
from pathlib import Path

from hawksense import settings
from hawksense.config import Config
from hawksense.db import Database
from hawksense.notify import Notifier
from hawksense.rules import build
from hawksense.stores import with_overrides, STORES


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "s.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def field(self, view, path):
        return next(f for s in view["sections"] for f in s["fields"] if f["path"] == path)

    def test_app_values_override_config(self):
        cfg = Config(advisor={"max_wait_days": 60}, destination={"code": "IL", "clearance_fee": 30},
                     notify={"telegram": {"chat_id": "1"}})
        settings.update(self.db, {"advisor.max_wait_days": 90, "notify.telegram.chat_id": "2",
                                  "destination.code": "US"}, env={})
        eff = settings.effective(self.db, cfg)
        self.assertEqual(eff.advisor["max_wait_days"], 90)
        self.assertEqual(eff.notify["telegram"]["chat_id"], "2")
        self.assertEqual(eff.destination, {"code": "US", "clearance_fee": 30})
        self.assertEqual(build(self.db, eff)[0].code, "US")
        # removing the app's value falls back to config.toml
        settings.update(self.db, {"advisor.max_wait_days": None}, env={})
        self.assertEqual(settings.effective(self.db, cfg).advisor["max_wait_days"], 60)

    def test_secrets_are_write_only_and_env_locks(self):
        settings.update(self.db, {"notify.telegram.bot_token": "123:secret"}, env={})
        view = settings.view(self.db, Config(), env={})
        token = self.field(view, "notify.telegram.bot_token")
        self.assertEqual((token["value"], token["is_set"], token["source"]), (None, True, "app"))
        self.assertNotIn("123:secret", str(view))
        env = {"HAWKSENSE_TELEGRAM_CHAT_ID": "99"}
        chat = self.field(settings.view(self.db, Config(), env=env), "notify.telegram.chat_id")
        self.assertEqual((chat["value"], chat["locked"], chat["source"]), ("99", True, "environment"))
        with self.assertRaisesRegex(ValueError, "environment"):
            settings.update(self.db, {"notify.telegram.chat_id": "5"}, env=env)

    def test_channels_configured_from_the_app(self):
        settings.update(self.db, {"notify.telegram.bot_token": "t", "notify.telegram.chat_id": "1"}, env={})
        eff = settings.effective(self.db, Config())
        self.assertTrue(Notifier(self.db, eff.notify, env={}).channels["telegram"].configured)
        view = settings.view(self.db, Config(), env={})
        self.assertTrue(next(s for s in view["sections"] if s["key"] == "telegram")["configured"])

    def test_validation(self):
        bad = [{"advisor.max_wait_days": 5000}, {"advisor.min_saving": 0.9}, {"notify.email.to": "nope"},
               {"notify.app_url": "hawksense.local"}, {"destination.code": "XX"}, {"nonsense": 1},
               {"stores.nostore.shipping_flat": 1}, {"stores.amazon_us.bogus": 1},
               {"rules.sources.x": {"url": "https://a", "target": "IL.vat_rate", "regex": "no group"}},
               {"rules.sources.x": {"url": "ftp://a", "target": "IL.vat_rate", "regex": "(\\d+)"}}]
        for change in bad:
            with self.assertRaises(ValueError, msg=change):
                settings.update(self.db, change, env={})
        self.assertEqual(settings.load(self.db), {})

    def test_store_overrides(self):
        settings.update(self.db, {"stores.amazon_us.shipping_flat": 15, "stores.newegg.ships_abroad": True}, env={})
        eff = settings.effective(self.db, Config())
        store = with_overrides(STORES["amazon_us"], eff.stores["amazon_us"])
        self.assertEqual(store.shipping_flat, 15)
        self.assertTrue(eff.stores["newegg"]["ships_abroad"])
        view = settings.view(self.db, Config(), env={})
        amazon = next(s for s in view["stores"] if s["key"] == "amazon_us")
        self.assertEqual((amazon["overrides"]["shipping_flat"], amazon["app"]), (15, ["shipping_flat"]))

    def test_rule_sources_and_feed_off(self):
        settings.update(self.db, {"rules.sources.il": {"url": "https://gov.example/p", "target": "IL.vat_exempt_usd",
                                                        "regex": r"up to \$(\d+)"},
                                  "rules.feed_url": "off"}, env={})
        eff = settings.effective(self.db, Config())
        self.assertEqual(eff.rules["feed_url"], "")
        self.assertEqual(eff.rules["sources"]["il"]["target"], "destination.IL.vat_exempt_usd")
        settings.update(self.db, {"rules.sources.il": None}, env={})
        self.assertNotIn("sources", settings.effective(self.db, Config()).rules)

    def test_schedule(self):
        self.assertEqual(settings.schedule(self.db, "check_every", 6.0), 6.0)
        settings.update(self.db, {"schedule.check_every": 2}, env={})
        self.assertEqual(settings.schedule(self.db, "check_every", 6.0), 2.0)
        settings.update(self.db, {"schedule.check_every": 0}, env={})
        self.assertIsNone(settings.schedule(self.db, "check_every", 6.0))  # 0 = off


if __name__ == "__main__":
    unittest.main()
