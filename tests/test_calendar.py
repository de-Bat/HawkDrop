import unittest
from datetime import date

from hawkdrop.calendar_events import EVENTS, black_friday, passover, rosh_hashana, upcoming_events


class HebrewCalendarTest(unittest.TestCase):
    def test_rosh_hashana(self):
        known = {2023: date(2023, 9, 16), 2024: date(2024, 10, 3), 2025: date(2025, 9, 23),
                 2026: date(2026, 9, 12), 2027: date(2027, 10, 2)}
        for year, expected in known.items():
            self.assertEqual(rosh_hashana(year), expected, year)

    def test_passover(self):
        known = {2023: date(2023, 4, 6), 2024: date(2024, 4, 23), 2025: date(2025, 4, 13),
                 2026: date(2026, 4, 2), 2027: date(2027, 4, 22)}
        for year, expected in known.items():
            self.assertEqual(passover(year), expected, year)


class EventsTest(unittest.TestCase):
    def test_black_friday(self):
        self.assertEqual(black_friday(2025), date(2025, 11, 28))
        self.assertEqual(black_friday(2026), date(2026, 11, 27))

    def test_upcoming_sorted_and_filtered(self):
        occ = upcoming_events(date(2026, 9, 26), 120, {"IL"})
        keys = [o.event.key for o in occ]
        self.assertIn("black_friday", keys)
        self.assertNotIn("prime_day", keys)
        self.assertEqual([o.start for o in occ], sorted(o.start for o in occ))

    def test_year_end_window_crosses_new_year(self):
        occ = EVENTS["year_end"].occurrences(date(2027, 1, 1), date(2027, 1, 1))
        self.assertEqual(occ, [(date(2026, 12, 26), date(2027, 1, 2))])


if __name__ == "__main__":
    unittest.main()
