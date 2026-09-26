"""Synthetic demo data: ~14 months of prices for one item in four stores,
including realistic dips around past sales events, so every command can be
tried without scraping anything."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from hawkdrop.calendar_events import EVENTS
from hawkdrop.db import Database, Item

DEMO_NAME = "Sony WH-1000XM5 (demo)"

# store key, url, currency, regular price, {event: discount}
DEMO_OFFERS = [
    ("ksp", "https://ksp.co.il/web/item/demo-wh1000xm5", "ILS", 1390.0,
     {"black_friday": 0.12, "rosh_hashana": 0.08, "passover": 0.07, "singles_day": 0.06}),
    ("ivory", "https://www.ivory.co.il/catalog.php?id=demo", "ILS", 1449.0,
     {"black_friday": 0.10, "rosh_hashana": 0.06}),
    ("amazon_us", "https://www.amazon.com/dp/DEMO000000", "USD", 328.0,
     {"black_friday": 0.20, "prime_day": 0.18, "prime_big_deal": 0.12, "year_end": 0.08}),
    ("aliexpress", "https://www.aliexpress.com/item/demo.html", "USD", 305.0,
     {"singles_day": 0.15, "aliexpress_anniversary": 0.12, "black_friday": 0.10}),
]


def _discount_on(day: date, events: dict[str, float]) -> float:
    best = 0.0
    for key, disc in events.items():
        for s, e in EVENTS[key].occurrences(day, day):
            if s <= day <= e:
                best = max(best, disc)
    return best


def seed_demo(db: Database, today: date | None = None, days: int = 430) -> Item:
    today = today or date.today()
    existing = db.get_item(DEMO_NAME)
    if existing:
        db.delete_item(existing)
    item = db.add_item(DEMO_NAME, "electronics", None)
    rng = random.Random(7)
    for store, url, currency, regular, events in DEMO_OFFERS:
        offer = db.add_offer(item, store, url)
        drift = rng.uniform(-0.00015, -0.00005)  # slow price erosion of an ageing model
        day = today - timedelta(days=days)
        while day <= today:
            age = (day - today).days
            base = regular * (1 + drift * age)
            noise = rng.gauss(0, 0.012)
            price = base * (1 - _discount_on(day, events)) * (1 + noise)
            price = round(price) if currency == "ILS" else round(price, 2)
            in_stock = rng.random() > 0.03
            ts = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=9)
            db.add_price(offer, price, currency, None, in_stock, "demo", ts)
            day += timedelta(days=rng.choice((1, 2, 3)))
    return item
