"""Sales-day calendar.

Computes the date windows of recurring shopping events, both global (Black
Friday, 11.11, Prime Day ...) and Israeli (pre-Rosh Hashana and pre-Passover
sales, which follow the Hebrew calendar and move every year).

Each event carries priors: how likely a tracked item is to be discounted during
the event (``participation``) and by how much (``depth``), per category. The
forecaster refines these priors with the item's own price history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

# --------------------------------------------------------------------------
# Hebrew calendar (algorithm from Reingold & Dershowitz, "Calendrical
# Calculations"). Only what we need: the Gregorian date of Rosh Hashana and
# of the first day of Passover.
# --------------------------------------------------------------------------

_HEBREW_EPOCH = -1373427  # R.D. fixed date of 1 Tishrei, year 1 AM


def _hebrew_elapsed_days(year: int) -> int:
    months_elapsed = (235 * year - 234) // 19
    parts_elapsed = 12084 + 13753 * months_elapsed
    day = 29 * months_elapsed + parts_elapsed // 25920
    if (3 * (day + 1)) % 7 < 3:
        day += 1
    return day


def _hebrew_year_length_correction(year: int) -> int:
    ny0 = _hebrew_elapsed_days(year - 1)
    ny1 = _hebrew_elapsed_days(year)
    ny2 = _hebrew_elapsed_days(year + 1)
    if ny2 - ny1 == 356:
        return 2
    if ny1 - ny0 == 382:
        return 1
    return 0


def rosh_hashana(gregorian_year: int) -> date:
    """First day of Rosh Hashana that falls in the given Gregorian year."""
    hebrew_year = gregorian_year + 3761
    fixed = _HEBREW_EPOCH + _hebrew_elapsed_days(hebrew_year) + _hebrew_year_length_correction(hebrew_year)
    return date.fromordinal(fixed)


def passover(gregorian_year: int) -> date:
    """First day of Passover (15 Nisan) in the given Gregorian year.

    15 Nisan is always exactly 163 days before the following Rosh Hashana.
    """
    return rosh_hashana(gregorian_year) - timedelta(days=163)


# --------------------------------------------------------------------------
# Gregorian helpers
# --------------------------------------------------------------------------


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th given weekday (Mon=0) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def black_friday(year: int) -> date:
    return nth_weekday(year, 11, 3, 4) + timedelta(days=1)  # day after Thanksgiving


# --------------------------------------------------------------------------
# Event definitions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SalesEvent:
    key: str
    name: str
    window: Callable[[int], tuple[date, date]]  # year -> (start, end), inclusive
    regions: tuple[str, ...]  # store regions where it is widely observed
    participation: float  # prior P(item discounted during event)
    depth: dict[str, float] = field(default_factory=dict)  # category -> mean discount
    date_certainty: str = "fixed"  # "fixed" or "estimated"

    def depth_for(self, category: str) -> float:
        return self.depth.get(category, self.depth.get("default", 0.10))

    def occurrences(self, start: date, end: date) -> list[tuple[date, date]]:
        """All windows of this event that overlap [start, end]."""
        out = []
        for year in range(start.year - 1, end.year + 1):
            s, e = self.window(year)
            if e >= start and s <= end:
                out.append((s, e))
        return out


def _range(month_a: int, day_a: int, month_b: int, day_b: int):
    return lambda y: (date(y, month_a, day_a), date(y, month_b, day_b))


_ELECTRONICS = {"electronics": 0.16, "computers": 0.12, "phones": 0.10, "appliances": 0.18,
                "clothing": 0.25, "shoes": 0.22, "toys": 0.20, "default": 0.15}

EVENTS: dict[str, SalesEvent] = {e.key: e for e in [
    SalesEvent(
        "black_friday", "Black Friday / Cyber Monday",
        lambda y: (black_friday(y) - timedelta(days=4), black_friday(y) + timedelta(days=3)),
        ("GLOBAL", "US", "IL", "EU", "UK", "CN"), 0.60, _ELECTRONICS,
    ),
    SalesEvent(
        "singles_day", "Singles' Day 11.11",
        _range(11, 9, 11, 12), ("CN", "IL"), 0.55,
        {"electronics": 0.15, "computers": 0.10, "phones": 0.12, "clothing": 0.20, "default": 0.13},
    ),
    SalesEvent(
        "prime_day", "Amazon Prime Day",
        _range(7, 8, 7, 17), ("AMAZON",), 0.45,
        {"electronics": 0.18, "computers": 0.12, "phones": 0.12, "default": 0.15},
        date_certainty="estimated",
    ),
    SalesEvent(
        "prime_big_deal", "Amazon Prime Big Deal Days (October)",
        lambda y: (nth_weekday(y, 10, 1, 2), nth_weekday(y, 10, 1, 2) + timedelta(days=1)),
        ("AMAZON",), 0.35,
        {"electronics": 0.14, "computers": 0.10, "default": 0.12},
        date_certainty="estimated",
    ),
    SalesEvent(
        "aliexpress_anniversary", "AliExpress Anniversary Sale",
        _range(3, 25, 3, 31), ("CN",), 0.50,
        {"electronics": 0.14, "default": 0.15}, date_certainty="estimated",
    ),
    SalesEvent(
        "mid_year_618", "618 Mid-Year Sale",
        _range(6, 16, 6, 20), ("CN",), 0.40,
        {"electronics": 0.12, "default": 0.12}, date_certainty="estimated",
    ),
    SalesEvent(
        "rosh_hashana", "Pre-Rosh Hashana sales (Israel)",
        lambda y: (rosh_hashana(y) - timedelta(days=14), rosh_hashana(y) - timedelta(days=1)),
        ("IL",), 0.45,
        {"electronics": 0.10, "appliances": 0.15, "computers": 0.08, "clothing": 0.15, "default": 0.10},
    ),
    SalesEvent(
        "passover", "Pre-Passover sales (Israel)",
        lambda y: (passover(y) - timedelta(days=14), passover(y) - timedelta(days=1)),
        ("IL",), 0.45,
        {"electronics": 0.10, "appliances": 0.15, "computers": 0.08, "clothing": 0.15, "default": 0.10},
    ),
    SalesEvent(
        "back_to_school", "Back to School",
        _range(8, 10, 8, 31), ("IL", "US"), 0.30,
        {"computers": 0.10, "electronics": 0.08, "default": 0.08},
    ),
    SalesEvent(
        "year_end", "Boxing Day / Year-End Sales",
        lambda y: (date(y, 12, 26), date(y + 1, 1, 2)), ("US", "UK", "EU", "AMAZON"), 0.40,
        {"electronics": 0.12, "clothing": 0.30, "default": 0.12},
    ),
]}


@dataclass
class EventOccurrence:
    event: SalesEvent
    start: date
    end: date

    def days_until(self, today: date) -> int:
        return (self.start - today).days

    def is_active(self, today: date) -> bool:
        return self.start <= today <= self.end


def upcoming_events(today: date, days: int, regions: set[str] | None = None) -> list[EventOccurrence]:
    """Events whose window overlaps [today, today+days], sorted by start date."""
    horizon = today + timedelta(days=days)
    out = []
    for event in EVENTS.values():
        if regions is not None and not (set(event.regions) & regions):
            continue
        for s, e in event.occurrences(today, horizon):
            out.append(EventOccurrence(event, s, e))
    return sorted(out, key=lambda o: o.start)
