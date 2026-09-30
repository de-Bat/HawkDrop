import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from hawksense.db import Database
from hawksense.fetch import FetchError, OutOfStock
from hawksense.keepa import KEEPA_EPOCH_MINUTES, KeepaError, KeepaSource, history, keepa_time, product_ref, to_extraction
from hawksense.landed import DESTINATIONS
from hawksense.tracker import Tracker
from tests.test_landed import StubFX


def km(days_ago: float) -> int:
    """A Keepa minute ``days_ago`` days back."""
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return int(ts.timestamp() // 60) - KEEPA_EPOCH_MINUTES


def product(**over) -> dict:
    """A product object in Keepa's documented format (prices in cents, sizes in mm/g)."""
    csv = [None] * 36
    csv[1] = [km(400), 14995, km(30), 12995, km(10), -1, km(5), 12995]  # NEW: [time, price] pairs
    csv[18] = [km(60), 13995, 0, km(20) - 1, 12495, 0, km(20), 12995, 0, km(3), -1, -1]  # buy box: triples
    p = {"asin": "B0CQ1CVW3Z", "domainId": 1, "title": "JBL Live 670NC Wireless On-Ear Headphones - Black",
         "features": ["ADAPTIVE NOISE CANCELLING", "Up to 65 hours of battery", "Fast charging", "Foldable"],
         "description": "<p>Deep focus.</p>", "images": [{"l": "61yHM6yIpoL.jpg", "m": "61yHM6yIpoL._m.jpg"}],
         "packageLength": 210, "packageWidth": 190, "packageHeight": 80, "packageWeight": 420,
         "itemWeight": 249, "availabilityAmazon": 0, "csv": csv,
         "stats": {"current": [12995, 12995, 4795, -1], "buyBoxPrice": 12995, "buyBoxShipping": 0,
                   "buyBoxIsUsed": False, "buyBoxAvailabilityMessage": "In Stock"}}
    p.update(over)
    return p


class KeepaParsingTest(unittest.TestCase):
    def test_product_links(self):
        self.assertEqual(product_ref("https://www.amazon.com/dp/B0CQ1HP1QC?plpRedirect=x&th=1"), ("B0CQ1HP1QC", 1, "USD"))
        self.assertEqual(product_ref("https://www.amazon.co.uk/JBL-Live/dp/B0CQ1CVW3Z/ref=sr_1_1"), ("B0CQ1CVW3Z", 2, "GBP"))
        self.assertEqual(product_ref("https://www.amazon.de/gp/product/B0CQ1CVW3Z"), ("B0CQ1CVW3Z", 3, "EUR"))
        self.assertIsNone(product_ref("https://www.amazon.com/s?k=jbl"))  # a search page, not a product
        self.assertIsNone(product_ref("https://www.ebay.com/itm/1"))
        self.assertIsNone(product_ref("https://evil.example/dp/B0CQ1CVW3Z"))

    def test_keepa_time(self):
        self.assertEqual(keepa_time(0), datetime(2011, 1, 1, tzinfo=timezone.utc))

    def test_current_offer_with_details_and_package_size(self):
        ex = to_extraction(product(), 1, "USD", "https://www.amazon.com/dp/B0CQ1CVW3Z")
        self.assertEqual((ex.price, ex.shipping, ex.currency, ex.method), (129.95, 0.0, "USD", "keepa-buybox"))
        self.assertEqual((ex.in_stock, ex.availability, ex.condition), (True, "In Stock", None))
        self.assertEqual(ex.description, "ADAPTIVE NOISE CANCELLING · Up to 65 hours of battery · Fast charging")
        self.assertEqual(ex.image, "https://m.media-amazon.com/images/I/61yHM6yIpoL.jpg")
        self.assertEqual((ex.specs.dims_cm, ex.specs.dims_kind), ((21.0, 19.0, 8.0), "package"))
        self.assertEqual((ex.specs.weight_kg, ex.specs.weight_kind), (0.42, "package"))

    def test_used_buy_box_is_labelled(self):
        p = product()
        p["stats"]["buyBoxIsUsed"] = True
        self.assertEqual(to_extraction(p, 1, "USD", "u").condition, "Used")

    def test_no_buy_box_falls_back_to_amazon_then_new(self):
        p = product()
        p["stats"].update(buyBoxPrice=-1, current=[-1, 11995])
        ex = to_extraction(p, 1, "USD", "u")
        self.assertEqual((ex.price, ex.method), (119.95, "keepa-new"))

    def test_nothing_on_sale_is_out_of_stock(self):
        p = product(availabilityAmazon=-1)
        p["stats"].update(buyBoxPrice=-1, current=[-1, -1], buyBoxAvailabilityMessage=None)
        with self.assertRaises(OutOfStock) as caught:
            to_extraction(p, 2, "GBP", "u")
        self.assertEqual(caught.exception.availability, "Currently unavailable")
        self.assertEqual(caught.exception.details["title"], p["title"])

    def test_yen_is_not_divided(self):
        p = product()
        p["stats"]["buyBoxPrice"] = 15800
        self.assertEqual(to_extraction(p, 5, "JPY", "u").price, 15800)

    def test_daily_history_from_the_buy_box(self):
        past = history(product(), 1)
        self.assertEqual([round(p, 2) for _, p, _ in past], [139.95, 129.95])  # last per day; no-offer days skipped
        self.assertEqual(past[0][2], 0.0)
        self.assertTrue(all(a[0] < b[0] for a, b in zip(past, past[1:])))

    def test_history_falls_back_to_new_offers_and_respects_the_window(self):
        p = product()
        p["csv"][18] = None
        self.assertEqual([x[1] for x in history(p, 1, days=365)], [129.95, 129.95])


class KeepaSourceTest(unittest.TestCase):
    def test_off_without_a_key(self):
        src = KeepaSource({}, env={})
        self.assertFalse(src.covers("https://www.amazon.com/dp/B0CQ1CVW3Z"))

    def test_request_and_errors(self):
        calls = []

        def http(url):
            calls.append(url)
            return {"products": [product()], "tokensLeft": 100}

        src = KeepaSource({"key": "secret"}, http=http, env={})
        self.assertEqual(src.fetch("https://www.amazon.co.uk/dp/B0CQ1CVW3Z").price, 129.95)
        q = parse_qs(urlparse(calls[0]).query)
        self.assertEqual((q["asin"], q["domain"], q["history"], q["buybox"]), (["B0CQ1CVW3Z"], ["2"], ["0"], ["1"]))
        self.assertTrue(calls[0].startswith("https://api.keepa.com/product?"))
        src = KeepaSource({}, http=lambda url: {"products": [{"asin": "X"}]}, env={"HAWKSENSE_KEEPA_KEY": "k"})
        with self.assertRaisesRegex(KeepaError, "no product"):
            src.fetch("https://www.amazon.com/dp/B0CQ1CVW3Z")


class TrackerWithKeepaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "k.db")
        self.calls = []
        self.response = {"products": [product()]}

        def http(url):
            self.calls.append(url)
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

        self.t = Tracker(self.db, StubFX(), DESTINATIONS["IL"], keepa=KeepaSource({"key": "k"}, http=http, env={}))
        self.item = self.db.add_item("JBL Live 670NC")
        self.offer = self.t.add_offer(self.item, "https://www.amazon.com/dp/B0CQ1CVW3Z?th=1")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_first_check_fills_history_then_only_current(self):
        self.t.check(self.item)
        points = self.db.prices(self.offer)
        self.assertEqual([p.source for p in points], ["keepa-history", "keepa-history", "keepa-buybox"])
        self.assertEqual(points[-1].price, 129.95)
        self.assertIn("history=1", self.calls[0])
        item = self.db.get_item(self.item.id)
        self.assertIn("ADAPTIVE", item.description)
        self.assertEqual(item.weight_kg, 0.42)  # the package weight, straight from Keepa
        self.t.check(item)
        self.assertIn("history=0", self.calls[1])
        self.assertEqual(len(self.db.prices(self.offer)), 4)

    def test_keepa_failure_falls_back_to_the_page(self):
        from tests.test_product_info import AMAZON

        self.response = KeepaError("Keepa: out of tokens")
        with unittest.mock.patch("hawksense.fetch.fetch_html", return_value=AMAZON):
            results = self.t.check(self.item)
        self.assertIsNone(results[0].error)
        self.assertEqual(self.db.prices(self.offer)[-1].source, "amazon-pattern")

    def test_both_failing_reports_both(self):
        self.response = KeepaError("Keepa: out of tokens")
        with unittest.mock.patch("hawksense.fetch.fetch_html", side_effect=FetchError("blocked")):
            results = self.t.check(self.item)
        self.assertIn("out of tokens", results[0].error)
        self.assertIn("page: blocked", results[0].error)


if __name__ == "__main__":
    unittest.main()
