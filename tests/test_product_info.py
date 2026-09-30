import json
import unittest

from hawksense.fetch import FetchError, OutOfStock, read_product
from hawksense.product_info import condition_label, in_stock_from, read_details

# trimmed from real product pages (markup as the stores served it)
AMAZON = """<html><span id="productTitle" class="a-size-large product-title-word-break"> JBL Live 670NC Wireless
On-Ear Headphones - Black </span>
<div id="feature-bullets" class="a-section"><h1>About this item</h1><ul class="a-unordered-list a-vertical">
<li class="a-spacing-mini"><span class="a-list-item"> ADAPTIVE NOISE CANCELLING: two mics adjust in real time. </span></li>
<li class="a-spacing-mini"><span class="a-list-item"> Up to 65 hours of battery life with fast charging. </span></li>
</ul></div>
<div id="availability" class="a-section a-spacing-base"> <span class="a-size-medium a-color-success"> Only 3 left in
stock - order soon. </span> <style>.x { width: 12px }</style></div>
<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$129.95</span></span></div>
<script>var data = {"hiRes":"https://m.media-amazon.com/images/I/61yHM6yIpoL._AC_SL1500_.jpg"};</script>
<table class="a-keyvalue prodDetTable"><tr><th class="prodDetSectionEntry"> Item Dimensions L x W x H </th>
<td class="prodDetAttrValue"> 7.2 x 6.9 x 2.1 inches </td></tr><tr><th class="prodDetSectionEntry"> Item Weight </th>
<td class="prodDetAttrValue"> 8.8 ounces </td></tr></table></html>"""

# an unavailable product: no buy box, but a carousel of other products' prices further down
AMAZON_UNAVAILABLE = """<html><span id="productTitle"> JBL Live 670NC </span>
<div id="availability" class="a-section"><span class="a-size-medium a-color-price"> Currently unavailable.
We don't know when or if this item will be back in stock. </span></div>
<div class="carousel"><span class="a-price"><span class="a-offscreen">£311.00</span></span></div></html>"""

NEWEGG = """<html><h1 class="product-title">JBL LIVE670NCBLK Live 670NC On-Ear Headphones - Black</h1>
<div class="price-current"><span></span>$<strong>97</strong><sup>.40</sup></div>
<div class="product-bullets"><ul><li>JBL Signature Sound and surround sound.</li><li>Bluetooth 5.3 with LE audio.</li></ul></div>
<script>window.__initialState__ = {"IsNew":false,"IsRefurbished":true,"IsOpenBoxed":false,"Instock":true};</script>
<meta property="og:image" content="https://c1.neweggimages.com/ProductImage/x.jpg"></html>"""

EBAY = """<html><h1 class="x-item-title__mainTitle"><span class="ux-textspans ux-textspans--BOLD">JBL Live 670NC
Headphones</span></h1><div class="x-item-condition-text"><span class="ux-textspans">Used - Very Good</span></div>
<div class="x-quantity__availability"><span class="ux-textspans ux-textspans--SECONDARY">More than 10 available</span></div>
<div class="x-price-primary"><span class="ux-textspans">US $64.99</span></div>
<img alt="" src="https://i.ebayimg.com/images/g/abcAAOSw/s-l500.jpg"></html>"""

JSON_LD = """<html><head><meta property="og:description" content="A meta description">
<script type="application/ld+json">""" + json.dumps({
    "@type": "Product", "name": "Some Speaker", "description": "Portable speaker with 20h battery.",
    "image": ["https://shop.example/s.jpg"],
    "offers": {"@type": "Offer", "price": "59.00", "priceCurrency": "EUR",
               "availability": "https://schema.org/OutOfStock",
               "itemCondition": "https://schema.org/RefurbishedCondition"}}) + "</script></head></html>"


class ProductDetailsTest(unittest.TestCase):
    def test_amazon_product_page(self):
        ex = read_product(AMAZON, "https://www.amazon.com/dp/B0CQ1CVW3Z")
        self.assertEqual((ex.price, ex.currency), (129.95, "USD"))
        self.assertEqual(ex.title, "JBL Live 670NC Wireless On-Ear Headphones - Black")
        self.assertIn("ADAPTIVE NOISE CANCELLING", ex.description)
        self.assertIn("65 hours", ex.description)
        self.assertTrue(ex.image.endswith("_AC_SL1500_.jpg"))
        self.assertEqual(ex.availability, "Only 3 left in stock - order soon")
        self.assertTrue(ex.in_stock)
        self.assertIsNone(ex.condition)
        self.assertEqual(ex.specs.dims_cm, (18.3, 17.5, 5.3))
        self.assertEqual(ex.specs.weight_kg, 0.249)

    def test_unavailable_amazon_page_is_out_of_stock_not_a_carousel_price(self):
        with self.assertRaises(OutOfStock) as caught:
            read_product(AMAZON_UNAVAILABLE, "https://www.amazon.co.uk/dp/B0CQ1CVW3Z")
        self.assertEqual(caught.exception.availability, "Currently unavailable")
        self.assertEqual(caught.exception.details["title"], "JBL Live 670NC")

    def test_amazon_renewed(self):
        page = AMAZON.replace("<html>", "<html><script>{\"x\":\"This Excellent condition Amazon Renewed pre-owned\"}</script>")
        self.assertEqual(read_product(page, "https://www.amazon.com/dp/B0DGQVFDVD").condition, "Renewed")

    def test_newegg(self):
        ex = read_product(NEWEGG, "https://www.newegg.com/jbl/p/0TH-02Y6-00058")
        self.assertEqual(ex.price, 97.40)
        self.assertEqual(ex.title, "JBL LIVE670NCBLK Live 670NC On-Ear Headphones - Black")
        self.assertEqual(ex.description, "JBL Signature Sound and surround sound. · Bluetooth 5.3 with LE audio.")
        self.assertEqual((ex.condition, ex.availability, ex.in_stock), ("Refurbished", "In stock", True))

    def test_ebay_item_page(self):
        d = read_details(EBAY, "https://www.ebay.com/itm/1")
        self.assertEqual(d["title"], "JBL Live 670NC Headphones")
        self.assertEqual(d["condition"], "Used - Very good")
        self.assertEqual(d["availability"], "More than 10 available")
        self.assertEqual(d["image"], "https://i.ebayimg.com/images/g/abcAAOSw/s-l1600.jpg")

    def test_schema_org_json_ld_for_any_store(self):
        ex = read_product(JSON_LD, "https://shop.example/p/1")
        self.assertEqual((ex.title, ex.description), ("Some Speaker", "Portable speaker with 20h battery."))
        self.assertEqual((ex.condition, ex.availability, ex.in_stock), ("Refurbished", "Out of stock", False))
        self.assertEqual(ex.image, "https://shop.example/s.jpg")

    def test_aliexpress_block_page_is_reported_as_blocked(self):
        page = '<script>var url = "//www.aliexpress.com//item/1.html/_____tmd_____/punish?x5secdata=abc";</script>'
        with self.assertRaisesRegex(FetchError, "blocked"):
            read_product(page, "https://www.aliexpress.com/item/1.html")

    def test_wording(self):
        self.assertIsNone(condition_label("https://schema.org/NewCondition"))
        self.assertIsNone(condition_label("Brand New"))
        self.assertEqual(condition_label("Pre-Owned"), "Pre-owned")
        self.assertEqual(condition_label("Open box"), "Open box")
        self.assertEqual(condition_label("Generalüberholt"), "Refurbished")
        self.assertEqual(condition_label("For parts or not working"), "For parts")
        self.assertFalse(in_stock_from("Derzeit nicht verfügbar."))
        self.assertFalse(in_stock_from("This listing was ended"))
        self.assertTrue(in_stock_from("More than 10 available"))
        self.assertIsNone(in_stock_from("Ships from Amazon"))


class CheckStoresDetailsTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        from hawksense.db import Database
        from hawksense.landed import DESTINATIONS
        from hawksense.tracker import Tracker
        from tests.test_landed import StubFX

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "p.db")
        self.t = Tracker(self.db, StubFX(), DESTINATIONS["IL"])
        self.item = self.db.add_item("JBL Live 670NC")
        self.offer = self.t.add_offer(self.item, "https://www.amazon.co.uk/dp/B0CQ1CVW3Z")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _check(self, page):
        import unittest.mock

        with unittest.mock.patch("hawksense.fetch.fetch_html", return_value=page):
            return self.t.check(self.db.get_item(self.item.id))

    def test_details_are_stored_with_the_price(self):
        self._check(AMAZON.replace("Only 3 left in", "In").replace("stock - order soon.", "Stock"))
        point = self.db.prices(self.offer)[-1]
        self.assertEqual((point.price, point.in_stock, point.availability), (129.95, True, "In Stock"))
        self.assertEqual(self.db.offers(self.item)[0].title, "JBL Live 670NC Wireless On-Ear Headphones - Black")
        item = self.db.get_item(self.item.id)
        self.assertIn("ADAPTIVE NOISE CANCELLING", item.description)
        self.assertTrue(item.image_url.endswith(".jpg"))

    def test_going_out_of_stock_keeps_the_store_listed_as_out_of_stock(self):
        self._check(AMAZON)
        results = self._check(AMAZON_UNAVAILABLE)
        self.assertIn("out of stock", results[0].error)
        last = self.db.prices(self.offer)[-1]
        self.assertEqual((last.price, last.in_stock, last.availability), (129.95, False, "Currently unavailable"))

    def test_never_priced_and_unavailable_records_nothing(self):
        self._check(AMAZON_UNAVAILABLE)
        self.assertEqual(self.db.prices(self.offer), [])


if __name__ == "__main__":
    unittest.main()
