import tempfile
import unittest
from pathlib import Path

from hawksense.db import Database
from hawksense.landed import DESTINATIONS
from hawksense.stores import STORES, search_url, stores_with_search
from hawksense.tracker import Tracker
from tests.test_landed import StubFX


class StoreSearchUrlTest(unittest.TestCase):
    def test_amazon_and_aliexpress_search_urls(self):
        self.assertEqual(search_url(STORES["amazon_us"], "sony wh-1000xm5"),
                         "https://www.amazon.com/s?k=sony+wh-1000xm5")
        self.assertIn("SearchText=sony", search_url(STORES["aliexpress"], "sony wh-1000xm5"))

    def test_store_without_a_template_has_no_search_url(self):
        self.assertIsNone(search_url(STORES["ksp"], "anything"))

    def test_stores_with_search_are_a_subset(self):
        keys = {s.key for s in stores_with_search()}
        self.assertIn("amazon_us", keys)
        self.assertNotIn("ksp", keys)


class SearchAllStoresTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "s.db")
        self.t = Tracker(self.db, StubFX(), DESTINATIONS["IL"])
        self.item = self.db.add_item("Sony WH-1000XM5")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_adds_one_offer_per_searchable_store(self):
        added = self.t.search_all_stores(self.item)
        keys = {self.t.store_for(o).key for o in added}
        self.assertIn("amazon_us", keys)
        self.assertIn("ebay", keys)
        self.assertNotIn("ksp", keys)  # no known search page
        self.assertEqual(len(self.db.offers(self.item)), len(added))

    def test_does_not_duplicate_a_store_already_tracked(self):
        self.t.add_offer(self.item, "https://www.amazon.com/dp/X")
        added = self.t.search_all_stores(self.item)
        self.assertNotIn("amazon_us", {self.t.store_for(o).key for o in added})
        self.assertEqual(len(self.db.offers(self.item)), 1 + len(added))

    def test_running_it_twice_is_a_no_op(self):
        first = self.t.search_all_stores(self.item)
        second = self.t.search_all_stores(self.item)
        self.assertEqual(second, [])
        self.assertEqual(len(self.db.offers(self.item)), len(first))


if __name__ == "__main__":
    unittest.main()
