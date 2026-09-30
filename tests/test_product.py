import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hawksense import tracker as tracker_mod
from hawksense.db import Database
from hawksense.ebay import EbayApi, parse_search_page
from hawksense.fetch import Extraction, add_details
from hawksense.landed import DESTINATIONS
from hawksense.product import (Details, availability_of, clean_image, clean_title, condition_of, extract_details,
                               image_type)
from hawksense.tracker import Tracker
from tests.test_ebay import http_with
from tests.test_landed import StubFX

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40

# B&H, Best Buy, Shopify stores...: schema.org Product JSON-LD
JSON_LD = """<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Product",
  "name": "Sony WH-1000XM5 Wireless Headphones", "image": ["/images/wh1000xm5.jpg", "/images/b.jpg"],
  "description": "<p>Industry-leading noise canceling &amp; 30-hour battery.</p>",
  "offers": {"@type": "Offer", "price": "328.00", "priceCurrency": "USD",
             "availability": "https://schema.org/PreOrder", "itemCondition": "https://schema.org/RefurbishedCondition"}}
</script>"""

OPEN_GRAPH = """<html><head><title>Ignored | Shop</title>
<meta property="og:title" content="Kindle Paperwhite (16 GB) - Shop.com">
<meta content="https://cdn.shop.com/kindle.webp" property="og:image">
<meta name="description" content="It's thinner &amp; lighter.">
<meta property="product:availability" content="out of stock">
<meta property="product:condition" content="used"></head></html>"""

AMAZON = """<span id="productTitle" class="a-size-large">   Apple AirPods Pro 2 (Renewed)   </span>
<div id="availability" class="a-section"><span class="a-size-medium a-color-price">Only 2 left in stock - order soon.</span></div>
<img alt="AirPods" src="https://m.media-amazon.com/images/I/small.jpg" data-old-hires="https://m.media-amazon.com/images/I/61SUj2aKoEL._AC_SL1500_.jpg" id="landingImage">
<div id="feature-bullets"><ul class="a-unordered-list"><li><span class="a-list-item"> Active Noise Cancellation </span></li>
<li><span class="a-list-item"> Up to 6 hours of listening </span></li></ul></div>"""

EBAY_LISTING = """<h1 class="x-item-title__mainTitle"><span class="ux-textspans ux-textspans--BOLD">Nintendo Switch OLED White</span></h1>
<div class="ux-image-carousel-item image-treatment active image" data-idx="0">
  <img data-zoom-src="https://i.ebayimg.com/images/g/abc/s-l1600.jpg" src="https://i.ebayimg.com/images/g/abc/s-l500.jpg"></div>
<div class="x-item-condition-text"><div class="ux-icon-text"><span class="ux-textspans">Pre-owned</span></div></div>
<div id="qtyAvailability"><span class="ux-textspans ux-textspans--SECONDARY">Last one</span></div>"""

ALIEXPRESS = """<script>window.runParams = {"data":{"titleModule":{"subject":"USB C Hub 7 in 1 \\u0026 HDMI"},
"imageModule":{"imagePathList":["https://ae01.alicdn.com/kf/Sabc.jpg","https://ae01.alicdn.com/kf/Sdef.jpg"]},
"quantityModule":{"totalAvailQuantity":3}}};</script>"""

NEWEGG = """<meta property="og:title" content="AMD Ryzen 5 9600X - Radeon Graphics Processor - Newegg.com"/>
<meta property="og:image" content="https://c1.neweggimages.com/ProductImage/19-113-844-05.jpg"/>
<meta property="og:description" content="Buy AMD Ryzen 5 9600X with fast shipping."/>
<div class="product-bullets"><ul><li>6 cores and 12 threads</li><li>Socket AM5</li></ul></div>
<script>{"Item":"19-113-844","FinalPrice":220,"Instock":true,"IsRefurbished":false}</script>"""


class NormalizeTest(unittest.TestCase):
    def test_conditions(self):
        for text, want in [("https://schema.org/NewCondition", "new"), ("UsedCondition", "used"),
                           ("Renewed", "refurbished"), ("Certified - Refurbished", "refurbished"),
                           ("Open box", "open_box"), ("New other (see details)", "open_box"),
                           ("Pre-owned", "used"), ("For parts or not working", "for_parts"),
                           ("Brand New", "new"), ("3000", "used"), ("1000", "new"), ("2500", "refurbished"),
                           ("something else", None), (None, None)]:
            self.assertEqual(condition_of(text), want, text)

    def test_availability(self):
        for text, want in [("https://schema.org/InStock", "in_stock"), ("http://schema.org/OutOfStock", "out_of_stock"),
                           ("PreOrder", "preorder"), ("BackOrder", "backorder"), ("SoldOut", "out_of_stock"),
                           ("LimitedAvailability", "limited"), ("Only 2 left in stock - order soon.", "limited"),
                           ("Currently unavailable.", "out_of_stock"), ("Last one", "limited"),
                           ("More than 10 available", "in_stock"), ("In Stock", "in_stock"),
                           ("Temporarily out of stock. Order now and we'll deliver when available.", "backorder"),
                           ("LIMITED_STOCK", "limited"), (None, None)]:
            self.assertEqual(availability_of(text), want, text)

    def test_images(self):
        self.assertEqual(clean_image("/a.jpg", "https://shop.com/p/1"), "https://shop.com/a.jpg")
        self.assertEqual(clean_image("//cdn.shop.com/a.jpg"), "https://cdn.shop.com/a.jpg")
        self.assertEqual(clean_image([{"url": "https://x.com/a.png"}]), "https://x.com/a.png")
        self.assertEqual(clean_image("https://www.bug.co.il/https://cdn.bug.co.il/a.webp"),
                         "https://cdn.bug.co.il/a.webp")  # seen on bug.co.il
        for bad in ("javascript:alert(1)", "data:image/png;base64,AA", "", None, "x" * 2000):
            self.assertIsNone(clean_image(bad), bad)

    def test_image_types(self):
        self.assertEqual(image_type(PNG), "image/png")
        self.assertEqual(image_type(b"\xff\xd8\xff\xe0rest"), "image/jpeg")
        self.assertEqual(image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertIsNone(image_type(b"<svg onload=alert(1)>"))  # SVG can run script: never served
        self.assertIsNone(image_type(b"<html>"))

    def test_control_characters_are_dropped(self):
        self.assertEqual(clean_title("Lamp\x1b]0;pwned\x07\x1b[2J \u202eevil"), "Lamp]0;pwned[2J evil")

    def test_titles(self):
        self.assertEqual(clean_title("Amazon.com : Sony WH-1000XM5 : Electronics"), "Sony WH-1000XM5")
        self.assertEqual(clean_title("AMD Ryzen 5 - Newegg.com"), "AMD Ryzen 5")


class ExtractTest(unittest.TestCase):
    def test_json_ld(self):
        d = extract_details(JSON_LD, "https://www.bhphotovideo.com/c/product/1")
        self.assertEqual(d, Details("Sony WH-1000XM5 Wireless Headphones",
                                    "https://www.bhphotovideo.com/images/wh1000xm5.jpg",
                                    "Industry-leading noise canceling & 30-hour battery.", "refurbished", "preorder"))

    def test_open_graph(self):
        d = extract_details(OPEN_GRAPH, "https://shop.com/k")
        self.assertEqual(d, Details("Kindle Paperwhite (16 GB)", "https://cdn.shop.com/kindle.webp",
                                    "It's thinner & lighter.", "used", "out_of_stock"))

    def test_microdata(self):
        page = ('<div itemscope itemtype="https://schema.org/Product"><img itemprop="image" src="/p.jpg">'
                '<link itemprop="availability" href="https://schema.org/BackOrder">'
                '<meta itemprop="itemCondition" content="https://schema.org/NewCondition">'
                '<div itemprop="description">A <b>fine</b> lamp.</div></div><title>Lamp | Lights.com</title>')
        d = extract_details(page, "https://lights.com/lamp")
        self.assertEqual(d, Details("Lamp", "https://lights.com/p.jpg", "A fine lamp.", "new", "backorder"))

    def test_amazon(self):
        d = extract_details(AMAZON, "https://www.amazon.com/dp/B0CHWRXH8B")
        self.assertEqual(d.title, "Apple AirPods Pro 2 (Renewed)")
        self.assertEqual(d.image, "https://m.media-amazon.com/images/I/61SUj2aKoEL._AC_SL1500_.jpg")
        self.assertEqual(d.description, "Active Noise Cancellation · Up to 6 hours of listening")
        self.assertEqual((d.condition, d.availability), ("refurbished", "limited"))

    def test_ebay_listing_page(self):
        d = extract_details(EBAY_LISTING, "https://www.ebay.com/itm/123456789012")
        self.assertEqual(d, Details("Nintendo Switch OLED White", "https://i.ebayimg.com/images/g/abc/s-l1600.jpg",
                                    None, "used", "limited"))
        ended = EBAY_LISTING + "<div>This listing was ended by the seller</div>"
        self.assertEqual(extract_details(ended, "https://www.ebay.com/itm/1").availability, "discontinued")

    def test_aliexpress(self):
        d = extract_details(ALIEXPRESS, "https://www.aliexpress.com/item/1005001.html")
        self.assertEqual(d, Details("USB C Hub 7 in 1 & HDMI", "https://ae01.alicdn.com/kf/Sabc.jpg", None,
                                    "new", "limited"))

    def test_newegg(self):  # layout of the live pages
        d = extract_details(NEWEGG, "https://www.newegg.com/p/N82E16819113844")
        self.assertEqual(d, Details("AMD Ryzen 5 9600X - Radeon Graphics Processor",
                                    "https://c1.neweggimages.com/ProductImage/19-113-844-05.jpg",
                                    "6 cores and 12 threads · Socket AM5", "new", "in_stock"))
        refurb = NEWEGG.replace('"IsRefurbished":false', '"IsRefurbished":true').replace('"Instock":true',
                                                                                        '"Instock":false')
        d = extract_details(refurb, "https://www.newegg.com/p/1")
        self.assertEqual((d.condition, d.availability), ("refurbished", "out_of_stock"))

    def test_unavailable_page_is_out_of_stock(self):
        ex = add_details(Extraction(10.0, "USD"), OPEN_GRAPH, "https://shop.com/k")
        self.assertFalse(ex.in_stock)
        self.assertEqual(ex.title, "Kindle Paperwhite (16 GB)")

    def test_long_and_hostile_input_is_bounded(self):
        page = '<meta property="og:description" content="' + "word " * 3000 + '">'
        d = extract_details(page)
        self.assertLessEqual(len(d.description), 601)
        self.assertTrue(d.description.endswith("…"))
        extract_details("<script type=application/ld+json>" + "[" * 100000)  # no crash, no hang


class EbayDetailsTest(unittest.TestCase):
    def test_api_item(self):
        item = {"title": "Switch OLED", "itemWebUrl": "https://www.ebay.com/itm/1",
                "price": {"value": "250", "currency": "USD"}, "condition": "Used", "conditionId": "3000",
                "image": {"imageUrl": "https://i.ebayimg.com/images/g/x/s-l1600.jpg"},
                "shortDescription": "Works great, box included.",
                "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "IN_STOCK", "availableQuantity": 1}]}
        ex = EbayApi("id", "secret", http=http_with(item)).item("1")
        self.assertEqual(ex.details, Details("Switch OLED", "https://i.ebayimg.com/images/g/x/s-l1600.jpg",
                                             "Works great, box included.", "used", "limited"))

    def test_search_page_cards(self):
        page = ('<ul><li class="s-item"><a class="s-item__link" href="https://www.ebay.com/itm/222222222222">'
                '<img src="https://i.ebayimg.com/thumbs/images/g/y/s-l225.jpg">'
                '<div class="s-item__title"><span>Switch OLED</span></div>'
                '<span class="SECONDARY_INFO">Brand New</span>'
                '<span class="s-item__price">$299.00</span></a></li></ul>')
        ex = parse_search_page(page, "USD")
        self.assertEqual((ex.details.condition, ex.details.image),
                         ("new", "https://i.ebayimg.com/thumbs/images/g/y/s-l225.jpg"))


class TrackerDetailsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "p.db")
        self.t = Tracker(self.db, StubFX(), DESTINATIONS["IL"])
        self.item = self.db.add_item("Headphones")
        self.offer = self.t.add_offer(self.item, "https://www.bhphotovideo.com/c/product/1")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def check(self, page, image=PNG):
        def fetch(url, regex=None):
            return add_details(Extraction(328.0, "USD", method="json-ld"), page, url)
        with mock.patch.object(tracker_mod, "fetch_price", side_effect=fetch), \
                mock.patch.object(tracker_mod, "fetch_image",
                                  side_effect=lambda url: ("a" * 64, "image/png", image)) as fetch_image:
            self.t.check(self.item)
        return fetch_image

    def test_check_saves_details_and_downloads_the_image_once(self):
        fetch_image = self.check(JSON_LD)
        row = self.db.offer_details(self.item)[self.offer.id]
        self.assertEqual((row["title"], row["condition"], row["availability"]),
                         ("Sony WH-1000XM5 Wireless Headphones", "refurbished", "preorder"))
        self.assertEqual(self.db.image("a" * 64), ("image/png", PNG))
        fetch_image = self.check(JSON_LD)
        fetch_image.assert_not_called()  # same image URL: already stored

    def test_missing_fields_keep_their_last_value_but_availability_does_not(self):
        self.check(JSON_LD)
        self.check('<meta property="og:title" content="Sony XM5">')
        row = self.db.offer_details(self.item)[self.offer.id]
        self.assertEqual(row["title"], "Sony XM5")  # the latest title
        self.assertEqual(row["condition"], "refurbished")  # not on the page this time: kept
        self.assertIsNone(row["availability"])  # only as good as the latest check

    def test_api_shows_details_and_image(self):
        from hawksense import api
        self.check(JSON_LD)
        detail = api.item_detail(self.t, self.item)
        self.assertEqual(detail["image"], "api/images/" + "a" * 64)
        self.assertEqual(detail["quotes"][0]["details"]["condition"], "refurbished")
        json.dumps(detail)  # serializable


if __name__ == "__main__":
    unittest.main()
