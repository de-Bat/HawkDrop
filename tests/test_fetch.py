import unittest

from hawksense.fetch import FetchError, extract_price, parse_number

JSON_LD = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"Product","name":"Headphones",
 "offers":[{"@type":"Offer","price":"1,299.90","priceCurrency":"ILS",
            "availability":"https://schema.org/InStock",
            "shippingDetails":{"shippingRate":{"value":"29","currency":"ILS"}}},
           {"@type":"Offer","price":"999","priceCurrency":"ILS","availability":"https://schema.org/OutOfStock"}]}]}
</script></head></html>"""


class ParseNumberTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_number("1,299.90"), 1299.90)
        self.assertEqual(parse_number("1.299,90"), 1299.90)
        self.assertEqual(parse_number("₪ 1,299"), 1299)
        self.assertEqual(parse_number("$49"), 49)
        self.assertEqual(parse_number("12,5"), 12.5)
        self.assertIsNone(parse_number("n/a"))


class ExtractTest(unittest.TestCase):
    def test_json_ld_prefers_in_stock_offer(self):
        ex = extract_price(JSON_LD)
        self.assertEqual((ex.price, ex.currency, ex.in_stock, ex.shipping), (1299.90, "ILS", True, 29))
        self.assertEqual(ex.title, "Headphones")

    def test_aggregate_offer(self):
        page = ('<script type="application/ld+json">{"@type":"Product","offers":'
                '{"@type":"AggregateOffer","lowPrice":"249","priceCurrency":"USD"}}</script>')
        self.assertEqual(extract_price(page).price, 249)

    def test_meta_tags(self):
        page = ('<meta property="product:price:amount" content="89.99">'
                '<meta property="product:price:currency" content="EUR">')
        ex = extract_price(page)
        self.assertEqual((ex.price, ex.currency, ex.method), (89.99, "EUR", "meta"))

    def test_custom_regex(self):
        ex = extract_price('<div class="p">מחיר: 1,450 ₪</div>', price_regex=r'class="p">[^\d]*([\d,]+)')
        self.assertEqual(ex.price, 1450)

    def test_captcha_page(self):
        with self.assertRaises(FetchError):
            extract_price("<html>Enter the characters - Robot Check</html>")
        with self.assertRaisesRegex(FetchError, "captcha"):
            extract_price("<html><head><title>Just a moment...</title></head>" + " " * 30000 + "</html>")

    def test_product_page_with_a_captcha_script_is_not_a_block(self):
        page = "<html><head><title>Grill</title></head>" + " " * 30000 + '<style>.grecaptcha-badge{}</style></html>'
        with self.assertRaisesRegex(FetchError, "no price found"):
            extract_price(page)

    def test_entity_encoded_json_ld_type(self):  # as on zap.co.il
        page = ('<script type="application/ld&#x2B;json">{"@type": "Product", "name": "Ninja AG653", "offers": '
                '{"@type": "AggregateOffer", "offerCount": "49", "lowPrice": "1019.00", "highPrice": "1845.00", '
                '"priceCurrency": "ILS"}}</script>')
        ex = extract_price(page, "https://www.zap.co.il/model.aspx?modelid=1164619")
        self.assertEqual((ex.price, ex.currency, ex.title), (1019.0, "ILS", "Ninja AG653"))

    def test_newegg_buy_box(self):
        page = ('<ul class="price"><li class="price-current"><span class="price-current-label"></span>$<strong>19</strong>'
                '<sup>.99</sup></li></ul>'  # an add-on product listed before the buy box (seen on the live page)
                '<div class="product-buy-box is-product-blackfriday-first"><div class="product-pane">'
                '<div class="price-current_2026"><span class="price-current-label"></span>$<strong>1,220</strong>'
                '<sup>.50</sup></div><div class="price-was"><span class="price-was-data">$279.00</span></div>')
        ex = extract_price(page, "https://www.newegg.com/p/N82E16819113844")
        self.assertEqual((ex.price, ex.currency, ex.method), (1220.5, "USD", "newegg-pattern"))
        data_only = '<li class="price-current">$<strong>19</strong></li><script>{"FinalPrice":220,"Instock":true}</script>'
        self.assertEqual(extract_price(data_only, "https://www.newegg.com/p/1").price, 220)


if __name__ == "__main__":
    unittest.main()
