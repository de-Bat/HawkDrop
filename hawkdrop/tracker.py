"""Glue between storage, fetching, landed cost and the advisor."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta, timezone

from hawkdrop.currency import FX
from hawkdrop.db import Database, Item, Offer, PricePoint
from hawkdrop.fetch import Extraction, FetchError, fetch_price
from hawkdrop.forecast import Advice, Settings, advise
from hawkdrop.landed import Destination, LandedCost, landed_cost
from hawkdrop.stores import StoreProfile, resolve_store, with_overrides

STALE_AFTER_DAYS = 14  # a store's last price is trusted this long when building the daily series


@dataclass
class Quote:
    offer: Offer
    store: StoreProfile
    point: PricePoint
    landed: LandedCost


@dataclass
class CheckResult:
    offer: Offer
    store: StoreProfile
    extraction: Extraction | None = None
    error: str | None = None


class Tracker:
    def __init__(self, db: Database, fx: FX, dest: Destination, settings: Settings | None = None,
                 store_overrides: dict | None = None):
        self.db, self.fx, self.dest = db, fx, dest
        self.settings = settings or Settings()
        self.store_overrides = store_overrides or {}

    @classmethod
    def settings_from(cls, advisor_cfg: dict) -> Settings:
        names = {f.name for f in fields(Settings)}
        return Settings(**{k: v for k, v in advisor_cfg.items() if k in names})

    # ---- stores / offers ------------------------------------------------------------
    def store_for(self, offer_or_ref: Offer | str) -> StoreProfile:
        if isinstance(offer_or_ref, Offer):
            offer_or_ref = offer_or_ref.url or offer_or_ref.store
        store = resolve_store(offer_or_ref)
        return with_overrides(store, self.store_overrides.get(store.key, {}))

    def add_offer(self, item: Item, url_or_store: str, shipping: float | None = None,
                  shipping_currency: str | None = None, price_regex: str | None = None) -> Offer:
        is_url = "://" in url_or_store or "/" in url_or_store
        store = resolve_store(url_or_store)
        return self.db.add_offer(item, store.key, url_or_store if is_url else "", shipping,
                                 shipping_currency, price_regex)

    # ---- prices ------------------------------------------------------------------------
    def check(self, item: Item) -> list[CheckResult]:
        results = []
        for offer in self.db.offers(item):
            store = self.store_for(offer)
            if not offer.url:
                results.append(CheckResult(offer, store, error="no URL (manual prices only)"))
                continue
            try:
                ex = fetch_price(offer.url, offer.price_regex)
            except FetchError as exc:
                results.append(CheckResult(offer, store, error=str(exc)))
                continue
            ex.currency = (ex.currency or store.currency).upper()
            self.db.add_price(offer, ex.price, ex.currency, ex.shipping, ex.in_stock, source=ex.method)
            results.append(CheckResult(offer, store, ex))
        return results

    def record_price(self, item: Item, url_or_store: str, price: float, currency: str | None = None,
                     shipping: float | None = None, in_stock: bool = True, when: date | None = None,
                     client_id: str | None = None) -> Offer:
        store = resolve_store(url_or_store)
        offer = self.db.find_offer(item, store.key) or self.add_offer(item, url_or_store)
        ts = datetime.combine(when, datetime.min.time(), timezone.utc) + timedelta(hours=12) if when else None
        self.db.add_price(offer, price, (currency or store.currency), shipping, in_stock, "manual", ts, client_id)
        return offer

    # ---- landed cost -------------------------------------------------------------------
    def landed(self, item: Item, offer: Offer, point: PricePoint, store: StoreProfile | None = None) -> LandedCost:
        store = store or self.store_for(offer)
        if offer.shipping is not None:
            shipping, ship_cur = offer.shipping, offer.shipping_currency or point.currency
        elif point.shipping is not None:
            shipping, ship_cur = point.shipping, point.currency
        else:
            shipping, ship_cur = None, None
        return landed_cost(point.price, point.currency, store, self.dest, self.fx, item.category,
                           shipping, ship_cur)

    def quotes(self, item: Item) -> list[Quote]:
        """Latest price of each offer, cheapest landed total first (out of stock last)."""
        out = []
        for offer in self.db.offers(item):
            points = self.db.prices(offer)
            if not points:
                continue
            store = self.store_for(offer)
            out.append(Quote(offer, store, points[-1], self.landed(item, offer, points[-1], store)))
        return sorted(out, key=lambda q: (not q.point.in_stock, q.landed.total))

    def daily_series(self, item: Item, today: date | None = None) -> list[tuple[date, float]]:
        """Cheapest in-stock landed price per day, carrying each store's last price forward."""
        today = today or date.today()
        per_offer: list[list[tuple[date, float | None]]] = []
        for offer in self.db.offers(item, active_only=False):
            store = self.store_for(offer)
            pts = []
            for p in self.db.prices(offer):
                total = self.landed(item, offer, p, store).total if p.in_stock else None
                pts.append((p.ts.date(), total))
            if pts:
                per_offer.append(pts)
        if not per_offer:
            return []
        start = min(pts[0][0] for pts in per_offer)
        series = []
        idx = [0] * len(per_offer)
        day = start
        while day <= today:
            best = None
            for i, pts in enumerate(per_offer):
                while idx[i] + 1 < len(pts) and pts[idx[i] + 1][0] <= day:
                    idx[i] += 1
                d, total = pts[idx[i]]
                if d <= day and total is not None and (day - d).days <= STALE_AFTER_DAYS:
                    best = total if best is None else min(best, total)
            if best is not None:
                series.append((day, best))
            day += timedelta(days=1)
        return series

    # ---- advice --------------------------------------------------------------------------
    def advise(self, item: Item, today: date | None = None) -> tuple[Advice, list[Quote]]:
        today = today or date.today()
        quotes = self.quotes(item)
        event_keys = {e for q in quotes if q.point.in_stock for e in q.store.events}
        if not event_keys:
            event_keys = {e for q in quotes for e in q.store.events}
        advice = advise(self.daily_series(item, today), item.category, event_keys, today, self.settings,
                        item.target_price, n_offers=len(quotes), currency=self.dest.currency)
        return advice, quotes
