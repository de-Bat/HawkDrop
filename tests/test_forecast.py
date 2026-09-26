import os
import tempfile
import unittest
from datetime import date, timedelta

from hawksense.currency import FX
from hawksense.db import Database
from hawksense.demo import seed_demo
from hawksense.forecast import advise
from hawksense.landed import DESTINATIONS
from hawksense.tracker import Tracker

ALL_EVENTS = {"black_friday", "singles_day", "rosh_hashana", "passover", "prime_day", "year_end"}


def flat(today, days, price=1000.0):
    return [(today - timedelta(days=i), price * (1 + 0.005 * ((i * 7) % 3 - 1))) for i in range(days, -1, -1)]


class AdviseTest(unittest.TestCase):
    def test_no_data(self):
        self.assertEqual(advise([], "electronics", ALL_EVENTS, date(2026, 9, 26)).action, "NO_DATA")

    def test_no_events_ahead_means_buy(self):
        today = date(2026, 9, 26)
        adv = advise(flat(today, 120), "electronics", {"passover"}, today)
        self.assertEqual(adv.action, "BUY_NOW")

    def test_black_friday_ahead_means_wait(self):
        today = date(2026, 10, 20)
        adv = advise(flat(today, 120), "electronics", {"black_friday", "singles_day"}, today)
        self.assertEqual(adv.action, "WAIT")
        self.assertIsNotNone(adv.wait)
        self.assertLess(adv.expected, adv.current)
        self.assertGreaterEqual(adv.confidence, 0.5)

    def test_target_price_reached_means_buy(self):
        today = date(2026, 10, 20)
        adv = advise(flat(today, 120), "electronics", {"black_friday"}, today, target_price=1100)
        self.assertEqual(adv.action, "BUY_NOW")

    def test_confidence_grows_with_history(self):
        today = date(2026, 10, 20)
        short = advise(flat(today, 5), "electronics", {"black_friday", "singles_day"}, today)
        long = advise(flat(today, 300), "electronics", {"black_friday", "singles_day"}, today)
        self.assertLess(short.confidence, long.confidence)


class DemoScenarioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _advise(self, today):
        item = seed_demo(self.db, today)
        tracker = Tracker(self.db, FX(self.db, offline=True), DESTINATIONS["IL"])
        return tracker.advise(item, today)

    def test_before_black_friday_waits(self):
        adv, quotes = self._advise(date(2026, 9, 26))
        self.assertEqual(adv.action, "WAIT")
        self.assertEqual(len(quotes), 4)

    def test_during_black_friday_buys(self):
        adv, _ = self._advise(date(2026, 11, 26))
        self.assertEqual(adv.action, "BUY_NOW")
        self.assertTrue(adv.active_events)

    def test_series_uses_cheapest_landed_offer(self):
        today = date(2026, 9, 26)
        item = seed_demo(self.db, today)
        tracker = Tracker(self.db, FX(self.db, offline=True), DESTINATIONS["IL"])
        series = dict(tracker.daily_series(item, today))
        cheapest = min(q.landed.total for q in tracker.quotes(item) if q.point.in_stock)
        self.assertLessEqual(series[today], cheapest + 1e-6)


if __name__ == "__main__":
    unittest.main()
