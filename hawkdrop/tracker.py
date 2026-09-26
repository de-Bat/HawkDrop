"""Glue between storage, fetching, landed cost and the advisor."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone

from hawkdrop.currency import FX
from hawkdrop.db import Database, Item, Offer, PricePoint
from hawkdrop.ebay import EbaySource, is_ebay
from hawkdrop.fetch import Extraction, FetchError, fetch_price
from hawkdrop.forecast import Advice, Settings, advise
from hawkdrop.forwarders import FORWARDERS, Account, Forwarder, Route, forwarders_from_config, parse_dims
from hawkdrop.landed import Destination, LandedCost, forwarded_cost, landed_cost
from hawkdrop.stores import StoreProfile, resolve_store, with_overrides

STALE_AFTER_DAYS = 14  # a store's last price is trusted this long when building the daily series


@dataclass
class Quote:
    offer: Offer
    store: StoreProfile
    point: PricePoint
    landed: LandedCost  # the cheapest way to get it: direct or through one of your forwarders
    routes: list[LandedCost] = field(default_factory=list)  # every option, best first


@dataclass
class CheckResult:
    offer: Offer
    store: StoreProfile
    extraction: Extraction | None = None
    error: str | None = None


class Tracker:
    def __init__(self, db: Database, fx: FX, dest: Destination, settings: Settings | None = None,
                 store_overrides: dict | None = None, forwarders: dict[str, Forwarder] | None = None,
                 ebay: EbaySource | None = None):
        self.db, self.fx, self.dest = db, fx, dest
        self.settings = settings or Settings()
        self.store_overrides = store_overrides or {}
        self.forwarders = forwarders if forwarders is not None else dict(FORWARDERS)
        self.ebay = ebay or EbaySource(db=db, dest_code=dest.code)
        self._accounts: list[Account] | None = None

    @classmethod
    def from_config(cls, db: Database, fx: FX, dest: Destination, cfg) -> "Tracker":
        """``cfg`` is a ``hawkdrop.config.Config``."""
        return cls(db, fx, dest, cls.settings_from(cfg.advisor), cfg.stores, forwarders_from_config(cfg.forwarders),
                   EbaySource(cfg.ebay, db, dest.code))

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
                  shipping_currency: str | None = None, price_regex: str | None = None,
                  local_shipping: float | None = None) -> Offer:
        is_url = "://" in url_or_store or "/" in url_or_store
        store = resolve_store(url_or_store)
        return self.db.add_offer(item, store.key, url_or_store if is_url else "", shipping,
                                 shipping_currency, price_regex, local_shipping)

    # ---- forwarders ---------------------------------------------------------------------
    def accounts(self) -> list[Account]:
        if self._accounts is None:
            self._accounts = self.db.accounts()
        return self._accounts

    def save_account(self, forwarder: str, warehouse: str, address: str = "", suite: str = "",
                     sales_tax: float | None = None) -> Account:
        fwd = self.forwarders.get(forwarder)
        if fwd is None:
            raise ValueError(f"unknown forwarder {forwarder!r} (see `hawkdrop forwarders`)")
        wh = fwd.warehouse(warehouse)
        if wh is None:
            codes = ", ".join(w.code for w in fwd.warehouses)
            raise ValueError(f"{fwd.name} has no {warehouse!r} warehouse (choose from {codes})")
        if fwd.needs_address and not address.strip():
            raise ValueError(f"enter the address {fwd.name} gave you in {wh.location}")
        if sales_tax is not None and not 0 <= sales_tax < 1:
            raise ValueError("sales tax is a fraction, e.g. 0.07 for 7%")
        self._accounts = None
        return self.db.save_account(fwd.key, wh.code, address, suite, sales_tax)

    def remove_account(self, forwarder: str, warehouse: str | None = None) -> int:
        self._accounts = None
        return self.db.delete_accounts(forwarder, warehouse)

    def _is_domestic(self, store: StoreProfile) -> bool:
        return store.country == self.dest.code or store.country in self.dest.same_market

    def forwarder_routes(self, store: StoreProfile, explore: bool = False) -> list[Route]:
        """Your forwarders that can receive from this store (plus, with ``explore``, ones you could add)."""
        if self._is_domestic(store):
            return []
        routes = []
        for acc in self.accounts():
            fwd = self.forwarders.get(acc.forwarder)
            wh = fwd.warehouse(acc.warehouse) if fwd else None
            if wh and wh.serves(store.country):
                routes.append(Route(fwd, wh, acc))
        if explore:
            have = {r.key for r in routes}
            routes += [Route(f, w) for f in self.forwarders.values() for w in f.warehouses
                       if w.serves(store.country) and f"{f.key}:{w.code}" not in have]
        return routes

    # ---- prices ------------------------------------------------------------------------
    def check(self, item: Item) -> list[CheckResult]:
        results = []
        for offer in self.db.offers(item):
            store = self.store_for(offer)
            if not offer.url:
                results.append(CheckResult(offer, store, error="no URL (manual prices only)"))
                continue
            try:
                if is_ebay(offer.url):
                    ex = self.ebay.fetch(offer.url, offer.price_regex)
                else:
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
    def landed_options(self, item: Item, offer: Offer, point: PricePoint, store: StoreProfile | None = None,
                       explore: bool = False) -> list[LandedCost]:
        """Every way to get this offer delivered, best first: direct, and through each forwarder."""
        store = store or self.store_for(offer)
        if offer.shipping is not None:
            shipping, ship_cur = offer.shipping, offer.shipping_currency or point.currency
        elif point.shipping is not None:
            shipping, ship_cur = point.shipping, point.currency
        else:
            shipping, ship_cur = None, None
        direct = landed_cost(point.price, point.currency, store, self.dest, self.fx, item.category,
                             shipping, ship_cur)
        try:
            dims = parse_dims(item.dims)
        except ValueError:
            dims = None
        routes = self.forwarder_routes(store, explore)
        options = [forwarded_cost(point.price, point.currency, store, r, self.dest, self.fx, item.category,
                                  item.weight_kg, dims, offer.local_shipping,
                                  offer.shipping_currency or point.currency)
                   for r in routes]
        if store.ships_abroad is False and not direct.domestic:
            if any(o.set_up for o in options):
                direct = None  # it can't be bought directly - only your forwarders count
            else:
                direct.shipping_known = False
                direct.notes.append(f"{store.name} doesn't ship to {self.dest.name} - set up a forwarder "
                                    "(`hawkdrop forwarder add`)")
        if direct is not None:
            options.append(direct)
        # suggestions never win; a route with a known shipping cost beats a guessed one
        return sorted(options, key=lambda lc: (not lc.set_up, not lc.shipping_known, lc.total))

    def landed(self, item: Item, offer: Offer, point: PricePoint, store: StoreProfile | None = None) -> LandedCost:
        return self.landed_options(item, offer, point, store)[0]

    def quotes(self, item: Item, explore: bool = False) -> list[Quote]:
        """Latest price of each offer, cheapest landed total first (out of stock last)."""
        out = []
        for offer in self.db.offers(item):
            points = self.db.prices(offer)
            if not points:
                continue
            store = self.store_for(offer)
            routes = self.landed_options(item, offer, points[-1], store, explore)
            out.append(Quote(offer, store, points[-1], routes[0], routes))
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
