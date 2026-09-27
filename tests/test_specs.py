import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hawksense import tracker as tracker_mod
from hawksense.db import Database
from hawksense.fetch import Extraction
from hawksense.landed import DESTINATIONS
from hawksense.specs import Observation, consensus, extract_specs, parse_dimensions, parse_weight
from hawksense.tracker import Tracker
from tests.test_landed import StubFX

AMAZON = """<table id="productDetails_techSpec_section_1">
<tr><th class="a-color-secondary a-size-base prodDetSectionEntry">Package Dimensions</th>
    <td class="a-size-base prodDetAttrValue">&lrm;10.24 x 8.66 x 3.54 inches; 8.82 ounces</td></tr>
<tr><th>Item Weight</th><td>250 Grams</td></tr>
<tr><th>Maximum weight recommendation</th><td>100 Kilograms</td></tr></table>"""
KSP = '<ul class="specs"><li><span>משקל:</span> <span>0.25 ק"ג</span></li><li><span>מידות:</span><span>26 x 22 x 9 ס"מ</span></li></ul>'
JSON_LD = ('<script type="application/ld+json">{"@type":"Product","name":"x","weight":{"@type":"QuantitativeValue",'
           '"value":"0.26","unitCode":"KGM"},"width":{"value":22,"unitCode":"CMT"},"height":{"value":9,"unitCode":"CMT"},'
           '"depth":{"value":26,"unitCode":"CMT"}}</script>')


class ParseTest(unittest.TestCase):
    def test_weights(self):
        self.assertEqual(parse_weight("1.2 pounds"), 0.544)
        self.assertEqual(parse_weight("350 g"), 0.35)
        self.assertEqual(parse_weight('1,5 ק"ג'), 1.5)
        self.assertEqual(parse_weight("250 גרם"), 0.25)
        self.assertIsNone(parse_weight("heavy"))
        self.assertIsNone(parse_weight("0.001 g"))  # implausible

    def test_dimensions(self):
        self.assertEqual(parse_dimensions("10 x 8 x 4 inches"), (25.4, 20.3, 10.2))
        self.assertEqual(parse_dimensions("26 × 22 × 9 cm"), (26, 22, 9))
        self.assertEqual(parse_dimensions("260x220x90mm"), (26, 22, 9))
        self.assertEqual(parse_dimensions("30 x 20 x 10"), (30, 20, 10))  # no unit: cm
        self.assertIsNone(parse_dimensions("30 x 20"))


class ExtractTest(unittest.TestCase):
    def test_amazon_package_row_wins_and_ignores_unrelated_weights(self):
        s = extract_specs(AMAZON)
        self.assertEqual((s.weight_kg, s.weight_kind), (0.25, "package"))
        self.assertEqual((s.dims_cm, s.dims_kind), ((26.0, 22.0, 9.0), "package"))

    def test_hebrew_spec_list(self):
        s = extract_specs(KSP)
        self.assertEqual((s.weight_kg, s.dims_cm, s.weight_kind), (0.25, (26, 22, 9), "item"))

    def test_json_ld(self):
        s = extract_specs(JSON_LD)
        self.assertEqual((s.weight_kg, s.dims_cm), (0.26, (26, 22, 9)))

    def test_label_with_unit_hint(self):
        self.assertEqual(extract_specs("<dl><dt>Weight (kg)</dt><dd>2.4</dd></dl>").weight_kg, 2.4)

    def test_nothing_found(self):
        self.assertFalse(extract_specs("<p>Great headphones!</p>"))


def obs(source, w=None, d=None, wk="package", dk="package"):
    return Observation(source, f"https://{source}", w, d, wk, dk)


class LiveLayoutTest(unittest.TestCase):
    """Layouts seen on the stores' real pages."""

    def test_ivory_spec_rows(self):
        page = ('<li class="col-md-12 col-12"><div style="width:40%"><b>מידות כ-</b></div>'
                '<div dir="rtl">מידות מוצר 175.2x54.8x62 מ"מ (ללא בסיס)<br>מידות בסיס 95x95x8.5 מ"מ<br>'
                'משקל 225 גרם</div></li>')
        specs = extract_specs(page)
        self.assertEqual((specs.weight_kg, specs.dims_cm), (0.225, (17.5, 5.5, 6.2)))

    def test_newegg_shipping_box(self):
        page = ('<script>window.__initialState__ = {"AllSellerList":[{"Item":"19-113-844","UnitCost":279,'
                '"Weight":0.2,"Length":4.9,"Width":4.9,"Height":1.4,"ShippingCharge":0.01}]}</script>')
        specs = extract_specs(page)
        self.assertEqual((specs.weight_kg, specs.weight_kind), (0.091, "package"))
        self.assertEqual(specs.dims_cm, (12.4, 12.4, 3.6))


class ConsensusTest(unittest.TestCase):
    def test_verified_when_sources_agree(self):
        c = consensus([obs("a", 1.0, (30, 20, 10)), obs("b", 1.1, (30, 21, 10))])
        self.assertEqual(c.status, "verified")
        self.assertAlmostEqual(c.weight_kg, 1.05)
        self.assertFalse(c.alert)

    def test_single_source_is_unverified(self):
        self.assertEqual(consensus([obs("a", 1.0)]).status, "unverified")

    def test_even_split_uses_nothing_and_alerts(self):
        c = consensus([obs("A", 1.0), obs("B", 2.5), obs("C")])
        self.assertEqual((c.status, c.weight_kg), ("conflict", None))
        self.assertTrue(c.alert)
        self.assertEqual(c.messages[0], "No weight is used: the store pages split evenly - A (1 kg boxed) vs "
                                        "B (2.5 kg boxed); C doesn't list a weight. Please set it yourself.")

    def test_bigger_group_wins_and_is_flagged(self):
        c = consensus([obs("Amazon", 1.0, (30, 20, 10)), obs("B&H", 1.05, (30, 21, 10)),
                       obs("KSP", 2.5, (50, 40, 30)), obs("Bug")])
        self.assertEqual((c.status, c.weight_kg, c.dims_cm), ("majority", 1.025, (30, 20, 10)))
        self.assertFalse(c.alert)
        self.assertEqual(c.to_confirm(), [
            "Weight 1.025 kg: Amazon (1 kg boxed) and B&H (1.05 kg boxed) agree, but KSP (2.5 kg boxed) differs; "
            "Bug doesn't list a weight.",
            "Size 30x20x10 cm: Amazon (30x20x10 cm boxed) and B&H (30x21x10 cm boxed) agree, but "
            "KSP (50x40x30 cm boxed) differs; Bug doesn't list a size."])
        self.assertEqual(len(c.to_confirm(weight_manual=True)), 1)  # what you set yourself isn't asked

    def test_agreement_with_pages_that_list_nothing_is_flagged(self):
        c = consensus([obs("Amazon", 1.0), obs("B&H", 1.1), obs("KSP")])
        self.assertEqual(c.status, "verified")
        self.assertEqual(c.confirm["weight"], "Weight 1.05 kg: Amazon (1 kg boxed) and B&H (1.1 kg boxed) agree; "
                                              "KSP doesn't list a weight.")
        self.assertEqual(consensus([obs("Amazon", 1.0), obs("B&H", 1.1)]).confirm, {})  # all pages agree

    def test_even_split_on_size_alerts(self):
        c = consensus([obs("A", 1.0, (10, 10, 10)), obs("B", 1.0, (40, 40, 40))])
        self.assertEqual((c.status, c.weight_kg, c.dims_cm), ("conflict", 1.0, None))
        self.assertTrue(c.alert)

    def test_item_weight_gets_packaging_allowance(self):
        c = consensus([obs("a", 1.0, wk="item"), obs("b", 1.0, wk="item")])
        self.assertAlmostEqual(c.weight_kg, 1.2)

    def test_package_weight_preferred_over_item_weight(self):
        self.assertEqual(consensus([obs("a", 1.5), obs("b", 1.0, wk="item")]).weight_kg, 1.5)

    def test_missing(self):
        c = consensus([obs("a"), obs("b")])
        self.assertEqual(c.status, "missing")
        self.assertTrue(c.alert)


class TrackerSpecsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "s.db")
        self.t = Tracker(self.db, StubFX(), DESTINATIONS["IL"])
        self.item = self.db.add_item("Headphones")
        self.t.add_offer(self.item, "https://www.amazon.com/dp/X")
        self.t.add_offer(self.item, "https://ksp.co.il/web/item/1")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def fake_fetch(self, pages):
        def fetch(url, regex=None):
            ex = Extraction(100.0, None)
            ex.specs = extract_specs(next(p for k, p in pages.items() if k in url))
            return ex
        return mock.patch.object(tracker_mod, "fetch_price", side_effect=fetch)

    def test_check_learns_weight_from_pages(self):
        with self.fake_fetch({"amazon": AMAZON, "ksp": KSP}):
            self.t.check(self.item)
        item = self.db.get_item(self.item.id)
        self.assertEqual(item.weight_source, "auto")
        self.assertAlmostEqual(item.weight_kg, 0.25)
        self.assertEqual(item.dims, "26x22x9")
        self.assertEqual(item.specs_status, "verified")
        self.assertEqual(len(self.db.spec_observations(item)), 2)

    def test_manual_weight_is_kept(self):
        self.db.update_item(self.item, weight_kg=3.0)
        with self.fake_fetch({"amazon": AMAZON, "ksp": KSP}):
            self.t.check(self.item)
        item = self.db.get_item(self.item.id)
        self.assertEqual((item.weight_kg, item.weight_source), (3.0, "manual"))
        self.assertEqual(item.dims, "26x22x9")  # size wasn't set by hand, so it's learned

    def test_split_pages_put_forwarder_prices_on_hold(self):
        self.db.update_item(self.item, weight_kg=0.25, source="auto")
        self.db.set_specs_status(self.item, "conflict")  # e.g. the pages split evenly
        self.db.save_spec_observation(self.item, "Amazon", "https://www.amazon.com/dp/X", 0.25, None)
        self.db.save_spec_observation(self.item, "KSP", "https://ksp.co.il/web/item/1", 2.5, None)
        self.t.update_specs(self.item)
        item = self.db.get_item(self.item.id)
        self.assertIsNone(item.weight_kg)  # the old automatic value no longer stands
        self.assertIn("on hold", self.t.specs_hold(item))
        offer = self.db.offers(item)[0]
        self.t.record_price(item, offer.url, 100.0, "USD")
        [q] = [q for q in self.t.quotes(item, explore=True) if q.store.key == "amazon_us"]
        self.assertTrue(all(r.hold for r in q.routes if r.route != "direct"))
        self.assertEqual(q.landed.route, "direct")  # buying directly doesn't depend on the weight
        self.t.confirm_specs(item)  # nothing to confirm yet: still no weight
        self.db.update_item(item, weight_kg=0.4)  # you set it
        self.assertIsNone(self.t.specs_hold(self.db.get_item(item.id)))

    def test_confirm_keeps_the_values(self):
        with self.fake_fetch({"amazon": AMAZON, "ksp": KSP}):
            self.t.check(self.item)
        item = self.t.confirm_specs(self.item)
        self.assertEqual((item.weight_source, item.dims_source), ("manual", "manual"))
        self.assertAlmostEqual(item.weight_kg, 0.25)

    def test_nothing_found_is_reported(self):
        with self.fake_fetch({"amazon": "<p>no specs</p>", "ksp": "<p>none</p>"}):
            self.t.check(self.item)
        found, rows = self.t.specs(self.item)
        self.assertEqual((found.status, len(rows)), ("missing", 2))
        self.assertIsNone(self.db.get_item(self.item.id).weight_kg)


if __name__ == "__main__":
    unittest.main()
