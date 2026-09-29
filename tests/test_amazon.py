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


if __name__ == "__main__":
    unittest.main()
