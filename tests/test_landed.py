import unittest

from hawksense.currency import FX
from hawksense.landed import DESTINATIONS, destination_from_config, landed_cost
from hawksense.stores import resolve_store


class StubFX(FX):
    RATES = {"USD": 1.0, "ILS": 4.0, "EUR": 0.5}

    def __init__(self):
        super().__init__(offline=True)
        self._rates = self.RATES


IL = DESTINATIONS["IL"]


class LandedCostTest(unittest.TestCase):
    def setUp(self):
        self.fx = StubFX()

    def test_domestic_has_no_taxes(self):
        lc = landed_cost(1000, "ILS", resolve_store("https://ksp.co.il/x"), IL, self.fx, shipping=30)
        self.assertTrue(lc.domestic)
        self.assertEqual(lc.total, 1030)

    def test_import_under_vat_threshold(self):
        lc = landed_cost(50, "USD", resolve_store("https://www.ebay.com/itm/1"), IL, self.fx, shipping=10)
        self.assertEqual(lc.vat, 0)
        self.assertEqual(lc.total, (50 + 10) * 4)

    def test_import_over_vat_threshold_adds_vat_and_clearance_fee(self):
        lc = landed_cost(100, "USD", resolve_store("https://www.ebay.com/itm/1"), IL, self.fx,
                         category="electronics", shipping=20)
        self.assertAlmostEqual(lc.vat, (100 + 20) * 4 * 0.18)
        self.assertEqual(lc.duty, 0)
        self.assertEqual(lc.fees, IL.clearance_fee)

    def test_store_collecting_vat_has_no_clearance_fee(self):
        lc = landed_cost(100, "USD", resolve_store("amazon_us"), IL, self.fx, category="electronics")
        self.assertEqual(lc.shipping, 0)  # free over $49
        self.assertGreater(lc.vat, 0)
        self.assertEqual(lc.fees, 0)

    def test_duty_over_customs_threshold(self):
        lc = landed_cost(600, "USD", resolve_store("https://www.ebay.com/itm/1"), IL, self.fx,
                         category="clothing", shipping=0)
        self.assertAlmostEqual(lc.duty, 600 * 4 * 0.12)
        self.assertAlmostEqual(lc.vat, (600 * 4 + lc.duty) * 0.18)

    def test_unknown_shipping_is_flagged(self):
        lc = landed_cost(100, "ILS", resolve_store("ivory"), IL, self.fx)
        self.assertFalse(lc.shipping_known)

    def test_state_fees_by_value(self):
        self.assertEqual([IL.state_fee(v) for v in (90, 100, 101, 500, 999, 1500)], [0, 0, 21, 21, 70, 91])
        lc = landed_cost(600, "USD", resolve_store("https://www.ebay.com/itm/1"), IL, self.fx,
                         category="electronics", shipping=0)
        self.assertEqual(lc.fees, IL.clearance_fee + 70)  # courier fee + computer and security fees
        amazon = landed_cost(600, "USD", resolve_store("amazon_us"), IL, self.fx, category="electronics")
        self.assertEqual(amazon.fees, 0)  # taxes prepaid at checkout: no fees on arrival

    def test_config_overrides(self):
        dest = destination_from_config({"code": "IL", "vat_exempt_usd": 150, "duty_rates": {"clothing": 0}})
        self.assertEqual(dest.vat_exempt_usd, 150)
        self.assertEqual(dest.duty_for("clothing"), 0)
        self.assertEqual(dest.duty_for("furniture"), IL.duty_for("furniture"))

    def test_unknown_store_guessed_from_tld(self):
        store = resolve_store("https://shop.example.co.il/p/1")
        self.assertEqual((store.country, store.currency), ("IL", "ILS"))


if __name__ == "__main__":
    unittest.main()
