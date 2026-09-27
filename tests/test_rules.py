import copy
import json
import tempfile
import unittest
from pathlib import Path

from hawksense import rules
from hawksense.config import Config
from hawksense.db import Database
from hawksense.fetch import FetchError

FEED = json.loads((Path(__file__).parent.parent / "rules" / "rules.json").read_text())

RATE_PAGE = """<table><tr><th>Weight</th><th>Price</th></tr>
<tr><td>0.5 kg</td><td>$12.00</td></tr><tr><td>1 kg</td><td>$18.00</td></tr>
<tr><td>1.5 kg</td><td>$24.00</td></tr><tr><td>2 kg</td><td>$30.00</td></tr></table>"""


def feed_with(**changes):
    feed = copy.deepcopy(FEED)
    for path, value in changes.items():
        node = feed
        *parents, leaf = path.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    return feed


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "r.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def check(self, feed, cfg=None, pages=None):
        cfg = cfg or Config()
        return rules.check_updates(self.db, cfg, fetch_json=lambda url: feed,
                                   fetch_page=lambda url: (pages or {})[url])

    def test_shipped_feed_matches_built_in_values(self):
        report = self.check(FEED)
        self.assertEqual((report.applied, report.pending, report.errors), ([], [], []))

    def test_small_change_applied_big_change_held_for_review(self):
        feed = feed_with(destination__IL__clearance_fee=40.0, destination__IL__vat_exempt_usd=150.0)
        report = self.check(feed)
        self.assertEqual([c.path for c in report.applied], ["destination.IL.clearance_fee"])
        self.assertEqual([c.path for c in report.pending], ["destination.IL.vat_exempt_usd"])
        dest, _ = rules.build(self.db, Config())
        self.assertEqual((dest.clearance_fee, dest.vat_exempt_usd), (40.0, 75.0))
        # the same pending value isn't raised again, and accepting applies it
        self.assertEqual(self.check(feed).pending, [])
        rules.decide(self.db, report.pending[0].id, accept=True)
        self.assertEqual(rules.build(self.db, Config())[0].vat_exempt_usd, 150.0)

    def test_rejected_value_is_not_offered_again(self):
        report = self.check(feed_with(destination__IL__vat_rate=0.30))
        rules.decide(self.db, report.pending[0].id, accept=False)
        self.assertEqual(self.check(feed_with(destination__IL__vat_rate=0.30)).pending, [])
        self.assertEqual(rules.build(self.db, Config())[0].vat_rate, 0.18)

    def test_invalid_values_are_dropped(self):
        report = self.check(feed_with(destination__IL__vat_rate=3.0))
        self.assertEqual(report.applied + report.pending, [])
        self.assertIn("between", report.errors[0])

    def test_bad_feed(self):
        self.assertIn("version", self.check({"version": 99}).errors[0])

        def boom(url):
            raise FetchError("offline")
        report = rules.check_updates(self.db, Config(), fetch_json=boom)
        self.assertEqual(report.errors, ["offline"])

    def test_layer_order(self):
        self.check(feed_with(forwarders__dealtas__warehouses__US__first=14.0))
        cfg = Config(forwarders={"dealtas": {"warehouses": {"US": {"first": 15.0}}}})
        self.assertEqual(rules.build(self.db, cfg)[1]["dealtas"].warehouse("US").rate.first, 15.0)
        rules.set_manual(self.db, {"dealtas.US.first": 16.0})
        self.assertEqual(rules.build(self.db, cfg)[1]["dealtas"].warehouse("US").rate.first, 16.0)
        self.assertEqual(rules.explain(self.db, cfg)["forwarders.dealtas.warehouses.US.first"], (16.0, "manual"))
        rules.set_manual(self.db, {"dealtas.US.first": None})
        self.assertEqual(rules.build(self.db, cfg)[1]["dealtas"].warehouse("US").rate.first, 15.0)

    def test_manual_validation_and_shorthands(self):
        self.assertEqual(rules.normalize_path("IL.vat_rate"), "destination.IL.vat_rate")
        self.assertEqual(rules.normalize_path("il.duty_rates.clothing"), "destination.IL.duty_rates.clothing")
        self.assertEqual(rules.normalize_path("redbox.uk.first"), "forwarders.redbox.warehouses.UK.first")
        with self.assertRaises(ValueError):
            rules.set_manual(self.db, {"IL.vat_rate": 0.9})
        with self.assertRaises(ValueError):
            rules.set_manual(self.db, {"IL.nonsense": 1})
        self.assertEqual(rules.parse_value("17%"), 0.17)
        self.assertIs(rules.parse_value("true"), True)

    def test_page_sources(self):
        cfg = Config(rules={"feed_url": "", "sources": {
            "exempt": {"url": "u1", "target": "IL.vat_exempt_usd", "regex": r"up to \$(\d+)"},
            "table": {"url": "u2", "target": "forwarders.dealtas.warehouses.US", "format": "table"},
            "broken": {"url": "u3", "target": "IL.clearance_fee", "regex": r"fee (\d+)"}}})
        pages = {"u1": "<p>Personal imports up to $90 are VAT free</p>", "u2": RATE_PAGE, "u3": "<p>nothing</p>"}
        report = self.check(None, cfg, pages)
        applied = {c.path: c.new for c in report.applied}
        self.assertEqual(applied["destination.IL.vat_exempt_usd"], 90.0)
        self.assertEqual(applied["forwarders.dealtas.warehouses.US.additional"], 6.0)
        pending = {c.path: c.new for c in report.pending}  # $25 -> $12 and a new table: over 50%, so asked
        self.assertEqual(pending["forwarders.dealtas.warehouses.US.first"], 12.0)
        self.assertEqual(pending["forwarders.dealtas.warehouses.US.table"][0], [0.5, 12.0])
        self.assertTrue(any("broken" in e for e in report.errors))

    def test_rate_table(self):
        self.assertEqual(rules.parse_rate_table(RATE_PAGE),
                         {"first": 12.0, "first_kg": 0.5, "step_kg": 0.5, "additional": 6.0,
                          "table": [[0.5, 12.0], [1.0, 18.0], [1.5, 24.0], [2.0, 30.0]]})
        self.assertIsNone(rules.parse_rate_table("<table><tr><td>1 kg</td><td>$5</td></tr></table>"))

    def test_feed_price_table_updates(self):
        feed = copy.deepcopy(FEED)
        table = feed["forwarders"]["redbox"]["warehouses"]["US"]["table"]
        feed["forwarders"]["redbox"]["warehouses"]["US"]["table"] = [[kg, p + 1] for kg, p in table]
        report = self.check(feed)
        self.assertIn("forwarders.redbox.warehouses.US.table", {c.path for c in report.applied})
        card = rules.build(self.db, Config())[1]["redbox"].warehouse("US").rate
        self.assertEqual(card.price(card.chargeable_kg(1.0)), 22.0)
        feed["forwarders"]["redbox"]["warehouses"]["US"]["table"] = [[1, 60.0], [2, 70.0]]  # nearly triples
        report = self.check(feed)
        self.assertIn("forwarders.redbox.warehouses.US.table", {c.path for c in report.pending})
        feed["forwarders"]["redbox"]["warehouses"]["US"]["table"] = [[2, 10], [1, 5]]  # not ascending
        self.assertTrue(self.check(feed).errors)

    def test_incomplete_new_forwarder_from_feed_is_refused(self):
        feed = copy.deepcopy(FEED)
        feed["forwarders"]["newco"] = {"handling_fee": 1.0}
        report = self.check(feed)
        self.assertTrue(any("newco" in e or "incomplete" in e for e in report.errors))
        self.assertNotIn("newco", rules.build(self.db, Config())[1])


if __name__ == "__main__":
    unittest.main()
