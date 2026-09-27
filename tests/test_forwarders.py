import tempfile
import unittest
from pathlib import Path

from hawksense.db import Database
from hawksense.forwarders import (FORWARDERS, Account, RateCard, Route, forwarders_from_config, parse_dims,
                                 state_from_address)
from hawksense.landed import DESTINATIONS, forwarded_cost
from hawksense.stores import resolve_store
from hawksense.tracker import Tracker
from tests.test_landed import StubFX

IL = DESTINATIONS["IL"]


def route(key, code="US", **account):
    fwd = FORWARDERS[key]
    return Route(fwd, fwd.warehouse(code), Account(1, key, code, **account))


class RateCardTest(unittest.TestCase):
    card = RateCard("USD", 10.0, 4.0)

    def test_rounds_up_to_steps(self):
        self.assertEqual(self.card.chargeable_kg(0.2), 0.5)
        self.assertEqual(self.card.chargeable_kg(1.0), 1.0)
        self.assertEqual(self.card.chargeable_kg(1.01), 1.5)
        self.assertEqual(self.card.price(1.5), 10 + 2 * 4)

    def test_volumetric_weight(self):
        self.assertEqual(self.card.chargeable_kg(0.5, (40, 30, 20)), 5.0)  # 24000 cm³ / 5000
        self.assertEqual(RateCard("USD", 1, 1, vol_divisor=None).chargeable_kg(0.5, (40, 30, 20)), 0.5)

    def test_small_parcels_skip_volumetric_weight(self):
        card = RateCard("USD", 10, 4, vol_free_cm=(43, 30, 10))
        self.assertEqual(card.chargeable_kg(0.5, (40, 30, 10)), 0.5)  # fits 43x30x10: weight only
        self.assertEqual(card.chargeable_kg(0.5, (10, 30, 40)), 0.5)  # in any orientation
        self.assertEqual(card.chargeable_kg(0.5, (44, 30, 10)), 3.0)  # bigger: 13200 cm³ / 5000

    def test_minimum_weight(self):
        self.assertEqual(RateCard("USD", 1, 1, min_kg=2).chargeable_kg(0.3), 2.0)


class HelpersTest(unittest.TestCase):
    def test_state_from_address(self):
        self.assertEqual(state_from_address("Ron, 16 Rd #IL1, New Castle, DE 19720"), "DE")
        self.assertEqual(state_from_address("1 Main St\nTorrance, CA 90501-1234\nUSA"), "CA")
        self.assertIsNone(state_from_address("Unit 5, London NW1 1AA"))

    def test_parse_dims(self):
        self.assertEqual(parse_dims("30x20x10"), (30, 20, 10))
        self.assertEqual(parse_dims("30 × 20.5 × 10"), (30, 20.5, 10))
        self.assertIsNone(parse_dims(""))
        with self.assertRaises(ValueError):
            parse_dims("30x20")

    def test_account_sales_tax(self):
        wh = FORWARDERS["myus"].warehouse("US")
        self.assertEqual(Account(1, "myus", "US").sales_tax_for(wh)[0], 0.07)
        self.assertEqual(Account(1, "myus", "US", address="x, Portland, OR 97201").sales_tax_for(wh)[0], 0.0)
        self.assertEqual(Account(1, "myus", "US", address="x, OR 97201", sales_tax=0.05).sales_tax_for(wh)[0], 0.05)

    def test_config_overrides_and_custom_service(self):
        fwds = forwarders_from_config({
            "dealtas": {"tax_handling_fee": 0, "warehouses": {"US": {"first": 9}}},
            "myfwd": {"name": "My Forwarder", "handling_fee": 1,
                      "warehouses": {"de": {"country": "DE", "currency": "EUR", "first": 8, "additional": 2}}},
        })
        self.assertEqual(fwds["dealtas"].tax_handling_fee, 0)
        self.assertEqual(fwds["dealtas"].warehouse("US").rate.first, 9)
        self.assertEqual(fwds["dealtas"].warehouse("US").rate.additional, 5.5)  # untouched
        self.assertEqual(fwds["myfwd"].warehouse("DE").rate.currency, "EUR")
        self.assertTrue(fwds["myfwd"].warehouse("DE").serves("FR"))  # EU single market
        with self.assertRaises(SystemExit):
            forwarders_from_config({"nowarehouse": {"name": "x"}})


class ForwardedCostTest(unittest.TestCase):
    def setUp(self):
        self.fx = StubFX()  # 1 USD = 4 ILS
        self.amazon = resolve_store("amazon_us")

    def test_service_that_pays_taxes(self):
        lc = forwarded_cost(100, "USD", self.amazon, route("dealtas", address="x, New Castle, DE 19720"), IL,
                            self.fx, "electronics", weight_kg=2.0)
        intl = (13 + 3 * 5.5) * 4  # 2 kg on a 0.5 kg card (above Dealtas's $20 minimum)
        self.assertEqual(lc.sales_tax, 0)
        self.assertEqual(lc.shipping, intl)  # $100 >= Amazon's free domestic shipping threshold
        self.assertAlmostEqual(lc.vat, (400 + intl) * 0.18)
        self.assertEqual(lc.fees, 5 * 4)  # Dealtas tax handling, no courier clearance fee
        self.assertEqual(lc.route, "dealtas:US")
        self.assertAlmostEqual(lc.total, 400 + intl + lc.vat + 20)

    def test_courier_clearance_and_sales_tax(self):
        lc = forwarded_cost(100, "USD", self.amazon, route("myus"), IL, self.fx, "electronics", weight_kg=0.4)
        self.assertAlmostEqual(lc.sales_tax, 400 * 0.07)
        goods = 400 + lc.sales_tax
        self.assertAlmostEqual(lc.vat, (goods + 24 * 4) * 0.18)
        self.assertEqual(lc.fees, IL.clearance_fee + 21)  # $107 with sales tax: 21 ILS computer fee
        self.assertIn(("courier clearance fee", IL.clearance_fee), lc.lines)
        self.assertIn(("state import fees", 21), lc.lines)

    def test_local_shipping_and_under_vat_threshold(self):
        lc = forwarded_cost(20, "USD", self.amazon, route("stackry"), IL, self.fx, weight_kg=0.5)
        self.assertEqual(lc.shipping, 6.99 * 4 + 18 * 4)  # under Amazon's $35 free-shipping line
        self.assertEqual((lc.vat, lc.duty), (0, 0))
        self.assertEqual(lc.fees, 1.5 * 4)  # Stackry's receiving fee only: no taxes due

    def test_minimum_charge(self):
        lc = forwarded_cost(100, "USD", self.amazon, route("dealtas"), IL, self.fx, weight_kg=0.3)
        self.assertIn(("Dealtas shipping, 0.5 kg", 20 * 4), lc.lines)  # $13 card price, $20 minimum

    def test_redbox_published_table(self):
        card = FORWARDERS["redbox"].warehouse("US").rate
        for kg, price in ((0.2, 15), (0.3, 18), (1.0, 21), (1.05, 22), (2.0, 31), (20.0, 186)):
            self.assertEqual(card.price(card.chargeable_kg(kg)), price, kg)
        self.assertEqual(card.price(card.chargeable_kg(22.0)), 206 + 10 * 1.0)  # past the table: per 100 g

    def test_over_the_weight_limit_is_flagged(self):
        lc = forwarded_cost(100, "USD", self.amazon, route("redbox"), IL, self.fx, weight_kg=25)
        self.assertFalse(lc.shipping_known)
        self.assertTrue(any("20 kg limit" in n for n in lc.notes))

    def test_config_can_replace_a_price_table(self):
        fwds = forwarders_from_config({"redbox": {"warehouses": {"US": {"table": [[1, 10], [2, 12]]}}}})
        card = fwds["redbox"].warehouse("US").rate
        self.assertEqual(card.price(card.chargeable_kg(1.5)), 12)

    def test_stale_override_is_skipped_when_not_strict(self):
        stale = {"redbox": {"warehouses": {"UK": {"first": 9}}}}  # a warehouse RedBox no longer has
        with self.assertRaises(SystemExit):
            forwarders_from_config(stale)
        self.assertIsNone(forwarders_from_config(stale, strict=False)["redbox"].warehouse("UK"))

    def test_unknown_weight_uses_category_default(self):
        lc = forwarded_cost(100, "USD", self.amazon, route("dealtas"), IL, self.fx, "computers")
        self.assertTrue(any("assumed 3 kg" in n for n in lc.notes))


class TrackerRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.t = Tracker(self.db, StubFX(), IL)
        self.item = self.db.add_item("GPU", "computers")
        self.db.update_item(self.item, weight_kg=2.0)
        self.item = self.db.get_item(self.item.id)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_us_only_store_needs_a_forwarder(self):
        self.t.record_price(self.item, "https://www.newegg.com/p/1", 500)
        q = self.t.quotes(self.item)[0]
        self.assertEqual(q.landed.route, "direct")
        self.assertFalse(q.landed.shipping_known)
        self.assertTrue(any("forwarder" in n for n in q.landed.notes))

        self.t.save_account("stackry", "US", "Me, 1 Rd, Salem, NH 03079")
        q = self.t.quotes(self.item)[0]
        self.assertEqual(q.landed.route, "stackry:US")
        self.assertEqual([r.route for r in q.routes], ["stackry:US"])  # direct isn't possible

    def test_best_route_wins_and_suggestions_never_do(self):
        self.t.record_price(self.item, "amazon_us", 500)
        self.t.save_account("myus", "US", "Me, 1 Rd, Sarasota, FL 34243")
        q = self.t.quotes(self.item, explore=True)[0]
        totals = [(r.route, r.set_up, round(r.total)) for r in q.routes]
        set_up = [r for r in q.routes if r.set_up]
        self.assertEqual(q.landed, min(set_up, key=lambda r: r.total))
        self.assertTrue(any(not r.set_up for r in q.routes), totals)
        self.assertTrue(all(r.set_up for r in q.routes[:len(set_up)]))

    def test_domestic_store_has_no_forwarder_routes(self):
        self.t.save_account("zipy", "US")
        self.t.record_price(self.item, "ksp", 3000, shipping=0)
        self.assertEqual([r.route for r in self.t.quotes(self.item, explore=True)[0].routes], ["direct"])

    def test_account_validation(self):
        with self.assertRaises(ValueError):
            self.t.save_account("dealtas", "US")  # needs an address
        with self.assertRaises(ValueError):
            self.t.save_account("dealtas", "JP", "x")
        with self.assertRaises(ValueError):
            self.t.save_account("nope", "US", "x")
        self.t.save_account("redbox", "US", "a, Wilmington, DE 19801")
        self.t.save_account("redbox", "EU", "b, Nieuw-Vennep")
        self.assertEqual(len(self.t.accounts()), 2)
        self.assertEqual(self.t.remove_account("redbox", "EU"), 1)
        self.assertEqual([a.warehouse for a in self.t.accounts()], ["US"])


if __name__ == "__main__":
    unittest.main()
