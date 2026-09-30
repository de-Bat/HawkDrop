"""Glue between storage, fetching, landed cost and the advisor."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from urllib.parse import urlparse
from datetime import date, datetime, timedelta, timezone

from hawksense import amazon, ebay
from hawksense.currency import FX
from hawksense.db import Database, Item, Offer, PricePoint
from hawksense.ebay import EbaySource, is_ebay
from hawksense.fetch import Extraction, FetchError, OutOfStock, fetch_html, fetch_price
from hawksense.forecast import Advice, Settings, advise
from hawksense.forwarders import FORWARDERS, Account, Forwarder, Route, parse_dims
from hawksense.landed import Destination, LandedCost, forwarded_cost, landed_cost
from hawksense.notify import Notifier
from hawksense.specs import Consensus, Observation, consensus, extract_specs
from hawksense.stores import StoreProfile, resolve_store, search_url, stores_with_search, with_overrides

STALE_AFTER_DAYS = 14

_BARE_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}(/|$)", re.I)


def clean_offer_ref(ref: str) -> str:
    """A product link (http/https only) or a store key/name; raises ValueError for anything else.

    Links are shown as clickable in the app, so ``javascript:``, ``data:`` and similar are refused.
    """
    ref = (ref or "").strip()
    if not ref or len(ref) > 2000 or any(ord(c) < 32 for c in ref):
        raise ValueError("enter a product link (https://...) or a store name")
    parsed = urlparse(ref)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        return ref
    if _BARE_DOMAIN.match(ref):  # "shop.co.il/item/1" -> https://shop.co.il/item/1
        return "https://" + ref
    if parsed.scheme or ref.startswith("//") or "/" in ref or ":" in ref:
        raise ValueError("product links must start with http:// or https://")
    return ref  # a store key ("ksp") or a name for prices logged by hand  # a store's last price is trusted this long when building the daily series


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
        self.config = None  # the Config this tracker was built from (from_config)
        self.notifier = Notifier(db)

    @classmethod
    def from_config(cls, db: Database, fx: FX, cfg, dest_code: str | None = None) -> "Tracker":
        """``cfg`` is a ``hawksense.config.Config``; taxes and forwarders include fetched and manual rules."""
        from hawksense import rules

        dest, forwarders = rules.build(db, cfg, dest_code)
        t = cls(db, fx, dest, cls.settings_from(cfg.advisor), cfg.stores, forwarders,
                EbaySource(cfg.ebay, db, dest.code))
        t.config = cfg
        t.notifier = Notifier(db, cfg.notify)
        return t

    def check_and_notify(self, items: list[Item] | None = None) -> dict[int, list[CheckResult]]:
        """Fetch prices for the items (default: all), then raise any notifications."""
        items = self.db.list_items() if items is None else items
        out = {}
        for item in items:
            out[item.id] = self.check(item)
            try:
                self.notifier.record_check(self, item, out[item.id])
            except Exception:  # bookkeeping for one item must not stop the others
                pass
        self.notifier.evaluate(self, items)
        return out

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
        url_or_store = clean_offer_ref(url_or_store)
        is_url = "://" in url_or_store
        store = resolve_store(url_or_store)
        return self.db.add_offer(item, store.key, url_or_store if is_url else "", shipping,
                                 shipping_currency, price_regex, local_shipping)

    def search_all_stores(self, item: Item, query: str | None = None) -> list[Offer]:
        """Add a trackable search offer at every store that supports one, for stores not already tracked.

        Best effort, like eBay's own search offers: a store may change its markup or block the request,
        in which case its check simply fails (log the price by hand).
        """
        query = (query or item.name).strip()
        if not query:
            return []
        have = {self.store_for(o).key for o in self.db.offers(item)}
        added = []
        for store in stores_with_search():
            if store.key in have:
                continue
            url = search_url(with_overrides(store, self.store_overrides.get(store.key, {})), query)
            if url:
                added.append(self.add_offer(item, url))
                have.add(store.key)
        for site in ebay.SITES:
            key = resolve_store(site).key
            if key in have:
                continue
            added.append(self.add_offer(item, ebay.search_url(query, site, None)))
            have.add(key)
        return added

    # ---- forwarders ---------------------------------------------------------------------
    def accounts(self) -> list[Account]:
        if self._accounts is None:
            self._accounts = self.db.accounts()
        return self._accounts

    def save_account(self, forwarder: str, warehouse: str, address: str = "", suite: str = "",
                     sales_tax: float | None = None) -> Account:
        fwd = self.forwarders.get(forwarder)
        if fwd is None:
            raise ValueError(f"unknown forwarder {forwarder!r} (see `hawksense forwarders`)")
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
                elif amazon.search_query(offer.url) and not offer.price_regex:
                    ex = amazon.price_search_page(offer.url)
                else:
                    ex = fetch_price(offer.url, offer.price_regex)
            except OutOfStock as exc:
                # nothing to buy right now: keep the last price but mark it out of stock, so the store stays listed
                last = self.db.prices(offer)[-1:] or None
                if last:
                    p = last[0]
                    self.db.add_price(offer, p.price, p.currency, p.shipping, False, source="availability",
                                      listing_url=p.listing_url, condition=p.condition,
                                      availability=exc.availability)
                if exc.details.get("title"):
                    self.db.set_offer_title(offer, exc.details["title"])
                results.append(CheckResult(offer, store, error=f"{store.name}: {exc}"))
                continue
            except FetchError as exc:
                results.append(CheckResult(offer, store, error=str(exc)))
                continue
            except Exception as exc:  # one broken store (eBay without keys, odd markup...) must not sink the rest
                results.append(CheckResult(offer, store, error=f"{store.name}: {type(exc).__name__}: {exc}"))
                continue
            ex.currency = (ex.currency or store.currency).upper()
            listing = ex.url if ex.url and ex.url != offer.url else None
            self.db.add_price(offer, ex.price, ex.currency, ex.shipping, ex.in_stock, source=ex.method,
                              listing_url=listing, condition=ex.condition, availability=ex.availability)
            if ex.title and ex.title != offer.title:
                self.db.set_offer_title(offer, ex.title)
            self._save_specs(item, store.name, ex.url or offer.url, ex.specs)
            if ex.image and not item.image_url:
                self.db.update_item(item, image_url=ex.image)
                item.image_url = ex.image
            if ex.description and not item.description:
                self.db.update_item(item, description=ex.description)
                item.description = ex.description
            results.append(CheckResult(offer, store, ex))
        if any(r.extraction for r in results):
            self.update_specs(item)
        return results

    # ---- weight / size --------------------------------------------------------------------
    def _save_specs(self, item: Item, source: str, url: str, specs) -> None:
        """Record what one page said about the item's size - also when it said nothing."""
        dims = "x".join(f"{d:g}" for d in specs.dims_cm) if specs and specs.dims_cm else None
        self.db.save_spec_observation(item, source, url, specs.weight_kg if specs else None, dims,
                                      specs.weight_kind if specs else "item", specs.dims_kind if specs else "item")

    def specs(self, item: Item) -> tuple[Consensus, list[dict]]:
        rows = self.db.spec_observations(item)
        obs = []
        for r in rows:
            try:
                dims = parse_dims(r["dims"])
            except ValueError:
                dims = None
            obs.append(Observation(r["source"], r["url"], r["weight_kg"], dims, r["weight_kind"], r["dims_kind"]))
        return consensus(obs), rows

    def update_specs(self, item: Item) -> Consensus:
        """Adopt the store pages' consensus unless you set the weight/size yourself."""
        found, _ = self.specs(item)
        item = self.db.get_item(item.id)
        if item.weight_source != "manual":
            if found.weight_kg:
                self.db.update_item(item, weight_kg=found.weight_kg, source="auto")
            elif item.weight_source == "auto":  # the pages no longer support it
                self.db.clear_item_field(item, "weight_kg")
        if item.dims_source != "manual":
            if found.dims_cm:
                self.db.update_item(item, dims=found.dims_text, source="auto")
            elif item.dims_source == "auto":
                self.db.clear_item_field(item, "dims")
        self.db.set_specs_status(item, found.status)
        return found

    def specs_hold(self, item: Item) -> str | None:
        """Why forwarder prices for this item wait for you: its weight/size is unknown or the pages split."""
        if item.weight_source == "manual" or item.specs_status not in ("missing", "conflict"):
            return None
        what = "weight not found on any store page" if item.specs_status == "missing" \
            else "store pages disagree on the weight/size"
        return (f"{what} - forwarder prices are on hold until you set it "
                f"(hawksense track {item.name!r} --weight KG --dims LxWxH)")

    def confirm_specs(self, item: Item) -> Item:
        """You checked the weight/size in use: keep them as if you had set them yourself."""
        item = self.db.get_item(item.id)
        self.db.update_item(item, weight_kg=item.weight_kg, dims=item.dims, source="manual")
        return self.db.get_item(item.id)

    def fetch_specs(self, item: Item, url: str) -> Consensus:
        """Read weight/size from an extra page (e.g. the manufacturer's) to cross-check the stores."""
        specs = extract_specs(fetch_html(url))
        self._save_specs(item, resolve_store(url).name, url, specs)
        return self.update_specs(item)

    def record_price(self, item: Item, url_or_store: str, price: float, currency: str | None = None,
                     shipping: float | None = None, in_stock: bool = True, when: date | None = None,
                     client_id: str | None = None) -> Offer:
        url_or_store = clean_offer_ref(url_or_store)
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
                                    "(`hawksense forwarder add`)")
        if hold := self.specs_hold(item):
            for o in options:  # no guessed weight: these wait until you set it
                o.hold, o.shipping_known = hold, False
        if direct is not None:
            options.append(direct)
        # routes on hold and suggestions never win; a known shipping cost beats a guessed one
        return sorted(options, key=lambda lc: (lc.hold is not None, not lc.set_up, not lc.shipping_known, lc.total))

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
        return sorted(out, key=lambda q: (q.landed.hold is not None, not q.point.in_stock, q.landed.total))

    def daily_series(self, item: Item, today: date | None = None) -> list[tuple[date, float]]:
        """Cheapest in-stock landed price per day, carrying each store's last price forward."""
        today = today or date.today()
        per_offer: list[list[tuple[date, float | None]]] = []
        for offer in self.db.offers(item, active_only=False):
            store = self.store_for(offer)
            pts = []
            for p in self.db.prices(offer):
                lc = self.landed(item, offer, p, store) if p.in_stock else None
                total = lc.total if lc and lc.hold is None else None
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
        if quotes and (hold := self.specs_hold(item)):
            if all(q.landed.hold for q in quotes):  # nothing can be priced until you set the weight
                advice.action, advice.confidence = "NO_DATA", 0.0
            advice.reasons.insert(0, hold[0].upper() + hold[1:])
        return advice, quotes
