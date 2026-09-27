import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hawksense import ebay
from hawksense.db import Database
from hawksense.ebay import (EbayApi, EbayError, EbaySource, listing_id, parse_search_page, search_params,
                           search_url)
from hawksense.fetch import Extraction, FetchError
from hawksense.landed import DESTINATIONS
from hawksense.stores import resolve_store
from hawksense.tracker import Tracker
from hawksense.vault import Vault
from tests.test_landed import StubFX

ITEM = {
    "title": "Sony WH-1000XM5", "itemWebUrl": "https://www.ebay.com/itm/123456789012",
    "price": {"value": "279.99", "currency": "USD"},
    "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "IN_STOCK"}],
    "shippingOptions": [{"shippingCost": {"value": "35.00", "currency": "USD"}},
                        {"shippingCost": {"value": "24.50", "currency": "USD"}}],
}
SEARCH = {"itemSummaries": [
    {"title": "cheap, no shipping info", "price": {"value": "199", "currency": "USD"},
     "itemWebUrl": "https://www.ebay.com/itm/1"},
    {"title": "a", "price": {"value": "250", "currency": "USD"}, "itemWebUrl": "https://www.ebay.com/itm/2",
     "shippingOptions": [{"shippingCost": {"value": "40", "currency": "USD"}}]},
    {"title": "b", "price": {"value": "270", "currency": "USD"}, "itemWebUrl": "https://www.ebay.com/itm/3",
     "shippingOptions": [{"shippingCostType": "FREE"}]},
]}
SEARCH_PAGE = """<ul>
<li class="s-item s-item__pl-on-bottom"><a class="s-item__link" href="https://www.ebay.com/itm/123456">
  <span class="s-item__price">$20.00</span></a></li>
<li class="s-item"><a class="s-item__link" href="https://www.ebay.com/itm/111111111111?hash=x&amp;a=1">
  <div class="s-item__title"><span>Headphones A</span></div>
  <span class="s-item__price">$289.00</span>
  <span class="s-item__shipping s-item__logisticsCost">+$15.00 shipping</span></a></li>
<li class="s-item"><a class="s-item__link" href="https://www.ebay.com/itm/222222222222">
  <div class="s-item__title"><span>Headphones B</span></div>
  <span class="s-item__price">$299.00</span><span class="s-item__shipping">Free International Shipping</span></a></li>
<li class="s-item"><a class="s-item__link" href="https://www.ebay.com/itm/333333333333">
  <span class="s-item__price">$100.00 to $400.00</span></a></li>
</ul>"""


class FakeHttp:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers))
        for prefix, response in self.responses.items():
            if prefix in url:
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(url)


def http_with(data):
    return FakeHttp({"oauth2/token": {"access_token": "T", "expires_in": 7200}, "/buy/browse/": data})


class UrlTest(unittest.TestCase):
    def test_listing_ids(self):
        self.assertEqual(listing_id("https://www.ebay.com/itm/123456789012"), "123456789012")
        self.assertEqual(listing_id("https://www.ebay.co.uk/itm/sony-headphones/123456789012?hash=x"), "123456789012")
        self.assertEqual(listing_id("https://www.ebay.com/itm.html?item=123456789012"), "123456789012")
        self.assertIsNone(listing_id("https://www.ebay.com/sch/i.html?_nkw=x"))

    def test_search_urls(self):
        url = search_url("sony wh-1000xm5", "ebay.de", "used")
        self.assertTrue(url.startswith("https://www.ebay.de/sch/i.html?"))
        self.assertEqual(search_params(url), ("sony wh-1000xm5", "used"))
        self.assertEqual(search_params(search_url("x", condition=None)), ("x", None))
        self.assertIsNone(search_params("https://www.ebay.com/itm/123456789012"))

    def test_store_profiles(self):
        self.assertEqual(resolve_store("https://www.ebay.co.uk/itm/1").key, "ebay_uk")
        self.assertEqual(resolve_store("https://www.ebay.de/itm/1").currency, "EUR")
        self.assertEqual(resolve_store(search_url("x")).key, "ebay")


class ApiTest(unittest.TestCase):
    def test_item_with_shipping_to_destination(self):
        http = http_with(ITEM)
        ex = EbayApi("id", "secret", country="IL", http=http).item("123456789012")
        self.assertEqual((ex.price, ex.currency, ex.shipping, ex.in_stock), (279.99, "USD", 24.5, True))
        method, url, headers = http.calls[-1]
        self.assertIn("legacy_item_id=123456789012", url)
        self.assertEqual(headers["Authorization"], "Bearer T")
        self.assertEqual(headers["X-EBAY-C-ENDUSERCTX"], "contextualLocation=country%3DIL")

    def test_token_is_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "e.db")
            http = http_with(ITEM)
            EbayApi("id", "secret", db, http=http).item("1")
            EbayApi("id", "secret", db, http=http).item("1")
            self.assertEqual(sum("oauth2/token" in c[1] for c in http.calls), 1)
            stored = db.get_kv("ebay_token")
            self.assertNotIn('"T"', stored)  # encrypted at rest
            self.assertEqual(json.loads(Vault.for_db(db).decrypt(stored))["token"], "T")
            db.close()

    def test_search_picks_cheapest_delivered_with_known_shipping(self):
        http = http_with(SEARCH)
        ex = EbayApi("id", "secret", http=http).search("headphones", "new")
        self.assertEqual((ex.price, ex.shipping, ex.url), (270.0, 0.0, "https://www.ebay.com/itm/3"))
        self.assertIn("conditionIds%3A%7B1000%7D", http.calls[-1][1])

    def test_out_of_stock(self):
        data = {**ITEM, "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}]}
        self.assertFalse(EbayApi("i", "s", http=http_with(data)).item("1").in_stock)


class PageTest(unittest.TestCase):
    def test_search_page(self):
        ex = parse_search_page(SEARCH_PAGE, "USD")
        self.assertEqual((ex.price, ex.shipping, ex.title), (299.0, 0.0, "Headphones B"))
        self.assertEqual(ex.url, "https://www.ebay.com/itm/222222222222")

    def test_listing_page_pattern(self):
        from hawksense.fetch import extract_price
        page = ('<div class="x-price-primary" data-testid="x-price-primary">'
                '<span class="ux-textspans">£249.99</span></div>')
        ex = extract_price(page, "https://www.ebay.co.uk/itm/123456789012")
        self.assertEqual((ex.price, ex.currency), (249.99, "GBP"))

    def test_blocked_search_page(self):
        with self.assertRaises(FetchError):
            parse_search_page("<html>Pardon Our Interruption...</html>")


class SourceTest(unittest.TestCase):
    def test_credentials_from_env_or_config(self):
        self.assertIsNone(EbaySource({}, env={}).api)
        self.assertIsNotNone(EbaySource({"client_id": "a", "client_secret": "b"}, env={}).api)
        src = EbaySource({}, env={"HAWKSENSE_EBAY_CLIENT_ID": "a", "HAWKSENSE_EBAY_CLIENT_SECRET": "b"},
                         dest_code="UK")
        self.assertEqual(src.api.country, "GB")

    def test_api_used_for_listing_and_marketplace_from_site(self):
        http = http_with(ITEM)
        src = EbaySource({"client_id": "a", "client_secret": "b"}, env={}, http=http)
        src.fetch("https://www.ebay.de/itm/123456789012")
        self.assertEqual(http.calls[-1][2]["X-EBAY-C-MARKETPLACE-ID"], "EBAY_DE")

    def test_falls_back_to_page_when_api_fails(self):
        http = FakeHttp({"oauth2/token": EbayError("eBay API error 401: invalid client")})
        src = EbaySource({"client_id": "a", "client_secret": "b"}, env={}, http=http)
        with mock.patch.object(ebay, "fetch_html", return_value=SEARCH_PAGE):
            ex = src.fetch(search_url("headphones"))
        self.assertEqual(ex.price, 299.0)
        with mock.patch.object(ebay, "fetch_html", return_value="<html></html>"):
            with self.assertRaisesRegex(FetchError, "invalid client"):
                src.fetch("https://www.ebay.com/itm/123456789012")


class TrackerCheckTest(unittest.TestCase):
    def test_check_routes_ebay_urls_to_the_ebay_source(self):
        class FakeSource:
            api = None

            def fetch(self, url, regex=None):
                return Extraction(250.0, "USD", True, 20.0, "listing", "ebay-api search", "https://www.ebay.com/itm/9")

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "c.db")
            t = Tracker(db, StubFX(), DESTINATIONS["IL"], ebay=FakeSource())
            item = db.add_item("Headphones")
            t.add_offer(item, search_url("headphones"))
            [result] = t.check(item)
            self.assertIsNone(result.error)
            q = t.quotes(item)[0]
            self.assertEqual((q.point.price, q.point.shipping, q.store.key), (250.0, 20.0, "ebay"))
            self.assertEqual(q.landed.shipping, 80.0)  # listing's shipping to IL, in ILS
            db.close()


if __name__ == "__main__":
    unittest.main()
