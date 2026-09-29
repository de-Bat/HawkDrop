import unittest

from hawksense.amazon import parse_search_candidates
from hawksense.fetch import FetchError

CARD = ('<div role="listitem" data-asin="{asin}" data-component-type="s-search-result" class="s-result-item">'
        '<img class="s-image" src="https://m.media-amazon.com/images/I/x.jpg" alt="{title}"/>'
        '<h2><span>Brand</span></h2><span class="a-offscreen">{price}</span></div>')
PAGE = "<html>" + CARD.format(asin="B0DGQVFDVD", title="JBL Live 670NC", price="$129.95") \
    + CARD.format(asin="B0F6LV656P", title="Edifier W820NB", price="$89.99") \
    + '<div data-asin="B0AAAAAAAA" data-component-type="s-search-result"><h2>No price</h2></div>' + "x" * 6000 + "</html>"


class AmazonSearchTest(unittest.TestCase):
    def test_parses_cards_with_title_price_image_and_canonical_url(self):
        found = parse_search_candidates(PAGE, "amazon.com")
        self.assertEqual([(e.title, e.price, e.currency) for e in found],
                         [("JBL Live 670NC", 129.95, "USD"), ("Edifier W820NB", 89.99, "USD")])
        self.assertEqual(found[0].url, "https://www.amazon.com/dp/B0DGQVFDVD")
        self.assertTrue(found[0].image.endswith("x.jpg"))

    def test_renewed_listings_carry_their_condition(self):
        page = PAGE.replace("Edifier W820NB", "Edifier W820NB").replace(
            '<h2><span>Brand</span></h2><span class="a-offscreen">$89.99', '<h2><span>Amazon Renewed</span></h2><span class="a-offscreen">$89.99')
        found = parse_search_candidates(page, "amazon.com")
        self.assertEqual([e.condition for e in found], [None, "Renewed"])

    def test_blocked_page_raises(self):
        with self.assertRaises(FetchError):
            parse_search_candidates("<html>tiny</html>", "amazon.co.uk")

    def test_relevance_filter_keeps_the_product_and_drops_neighbours(self):
        from hawksense.api import matches_query

        q = "jbl live nc670"
        self.assertTrue(matches_query(q, "JBL Live 670NC Wireless On-Ear Headphones, Adaptive NC - Black"))
        self.assertTrue(matches_query(q, "JBL LIVE670NCBLK Live 670NC On-Ear Headphones"))
        self.assertFalse(matches_query(q, "JBL Live 770NC - Wireless Over-Ear Headphones"))
        self.assertFalse(matches_query(q, "JBL MA710 7.2 Channel AV Receiver"))
        self.assertFalse(matches_query(q, None))

    def test_did_you_mean_suggests_neighbouring_models(self):
        from hawksense.api import suggest_phrases

        titles = ["JBL Live 770NC - Wireless Over-Ear Headphones", "JBL Live 770NC Wireless, Black",
                  "JBL Live 680NC Wireless On-Ear Headphones", "JBL MA710 7.2 Channel AV Receiver", None,
                  "JBL Live 670NC Wireless On-Ear Headphones"]
        self.assertEqual(suggest_phrases("jbl live nc670", titles), ["JBL Live 770NC", "JBL Live 680NC"])

    def test_did_you_mean_survives_typos(self):
        from hawksense.api import suggest_phrases

        titles = ["JBL Live 770NC - Wireless", "JBL Live 670NC Wireless On-Ear Headphones", "JBL MA710 AV Receiver",
                  "Sony WH-1000XM5"]
        self.assertEqual(suggest_phrases("jbl line 670c", titles)[0], "JBL Live 670NC")
        self.assertEqual(suggest_phrases("jbl lvie 670nc", titles)[0], "JBL Live 670NC")

    def test_search_offer_prices_the_cheapest_new_matching_listing(self):
        import unittest.mock

        from hawksense import amazon

        page = "<html>" + CARD.format(asin="B000000001", title="Sony WH-1000XM5", price="$19.99") \
            + CARD.format(asin="B000000002", title="JBL Live 670NC Renewed", price="$47.95").replace(
                "<h2><span>Brand</span></h2>", "<h2><span>Amazon Renewed</span></h2>") \
            + CARD.format(asin="B000000003", title="JBL Live 670NC Wireless", price="$129.95") + "x" * 6000
        self.assertEqual(amazon.search_query("https://www.amazon.com/s?k=jbl+live+670"), ("jbl live 670", "amazon.com"))
        self.assertIsNone(amazon.search_query("https://www.amazon.com/dp/B000000003"))
        with unittest.mock.patch("hawksense.amazon.fetch_html", return_value=page):
            ex = amazon.price_search_page("https://www.amazon.com/s?k=jbl+live+670")
            self.assertEqual((ex.price, ex.url), (129.95, "https://www.amazon.com/dp/B000000003"))
            with self.assertRaises(FetchError):  # nothing matches: an error, never a random product's price
                amazon.price_search_page("https://www.amazon.com/s?k=bose+qc45")


if __name__ == "__main__":
    unittest.main()
