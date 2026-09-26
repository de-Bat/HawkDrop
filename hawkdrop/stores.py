"""Built-in store profiles.

A profile tells HawkDrop where a store ships from, its currency, its shipping
policy to Israel and which sales events it takes part in. Shipping figures are
estimates: override them per offer with ``--shipping`` or in config.toml.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from urllib.parse import urlparse

IL_EVENTS = ("black_friday", "singles_day", "rosh_hashana", "passover", "back_to_school")
AMAZON_EVENTS = ("black_friday", "prime_day", "prime_big_deal", "year_end", "back_to_school")
CN_EVENTS = ("singles_day", "black_friday", "aliexpress_anniversary", "mid_year_618")
US_EVENTS = ("black_friday", "year_end", "back_to_school")
EU_EVENTS = ("black_friday", "year_end")


@dataclass(frozen=True)
class StoreProfile:
    key: str
    name: str
    domains: tuple[str, ...]
    country: str  # ISO code of where the store ships from ("IL", "US", "CN", ...)
    currency: str
    events: tuple[str, ...] = ()
    shipping_flat: float | None = None  # in store currency; None = unknown
    shipping_free_over: float | None = None  # order value above which shipping is free
    collects_import_vat: bool = False  # store charges destination VAT/duty at checkout
    notes: str = ""
    regions: tuple[str, ...] = field(default=())

    def shipping_for(self, price: float) -> float | None:
        if self.shipping_free_over is not None and price >= self.shipping_free_over:
            return 0.0
        return self.shipping_flat


STORES: dict[str, StoreProfile] = {s.key: s for s in [
    # ---- Israel -------------------------------------------------------------
    StoreProfile("ksp", "KSP", ("ksp.co.il",), "IL", "ILS", IL_EVENTS, regions=("IL",)),
    StoreProfile("ivory", "Ivory", ("ivory.co.il",), "IL", "ILS", IL_EVENTS, regions=("IL",)),
    StoreProfile("bug", "Bug", ("bug.co.il",), "IL", "ILS", IL_EVENTS, regions=("IL",)),
    StoreProfile("lastprice", "LastPrice", ("lastprice.co.il",), "IL", "ILS", IL_EVENTS, regions=("IL",)),
    StoreProfile("payngo", "Machsanei Hashmal / Payngo", ("payngo.co.il",), "IL", "ILS", IL_EVENTS,
                 regions=("IL",)),
    StoreProfile("ace", "ACE", ("ace.co.il",), "IL", "ILS", IL_EVENTS, regions=("IL",)),
    StoreProfile("zap", "Zap (price comparison)", ("zap.co.il",), "IL", "ILS", IL_EVENTS, regions=("IL",),
                 notes="Aggregator: the price is the cheapest listed shop."),
    # ---- International ------------------------------------------------------
    StoreProfile("amazon_us", "Amazon.com", ("amazon.com",), "US", "USD", AMAZON_EVENTS,
                 shipping_flat=12.0, shipping_free_over=49.0, collects_import_vat=True,
                 regions=("AMAZON", "US", "GLOBAL"),
                 notes="Free shipping to Israel on eligible orders over $49; import fees deposit at checkout."),
    StoreProfile("amazon_uk", "Amazon.co.uk", ("amazon.co.uk",), "UK", "GBP", AMAZON_EVENTS,
                 shipping_flat=10.0, collects_import_vat=True, regions=("AMAZON", "UK", "GLOBAL")),
    StoreProfile("amazon_de", "Amazon.de", ("amazon.de",), "DE", "EUR", AMAZON_EVENTS,
                 shipping_flat=12.0, collects_import_vat=True, regions=("AMAZON", "EU", "GLOBAL")),
    StoreProfile("aliexpress", "AliExpress", ("aliexpress.com", "aliexpress.us", "he.aliexpress.com"), "CN", "USD",
                 CN_EVENTS, shipping_flat=0.0, collects_import_vat=True, regions=("CN", "GLOBAL"),
                 notes="Most items ship free; VAT is collected at checkout for low-value orders."),
    StoreProfile("ebay", "eBay", ("ebay.com",), "US", "USD", US_EVENTS, regions=("US", "GLOBAL"),
                 notes="Shipping varies per listing - set it with --shipping."),
    StoreProfile("bhphoto", "B&H Photo", ("bhphotovideo.com",), "US", "USD", US_EVENTS, regions=("US",)),
    StoreProfile("newegg", "Newegg", ("newegg.com",), "US", "USD", US_EVENTS, regions=("US",)),
]}

_TLD_COUNTRY = {
    ".co.il": ("IL", "ILS"), ".il": ("IL", "ILS"), ".co.uk": ("UK", "GBP"), ".uk": ("UK", "GBP"),
    ".de": ("DE", "EUR"), ".fr": ("FR", "EUR"), ".it": ("IT", "EUR"), ".es": ("ES", "EUR"),
    ".nl": ("NL", "EUR"), ".cn": ("CN", "CNY"), ".jp": ("JP", "JPY"),
}

_COUNTRY_REGIONS = {"IL": ("IL",), "US": ("US", "GLOBAL"), "UK": ("UK", "GLOBAL"), "CN": ("CN", "GLOBAL")}
_COUNTRY_EVENTS = {"IL": IL_EVENTS, "US": US_EVENTS, "UK": EU_EVENTS, "CN": CN_EVENTS}


def _host(url: str) -> str:
    host = urlparse(url if "://" in url else "https://" + url).hostname or ""
    return host.lower().removeprefix("www.")


def resolve_store(url_or_key: str) -> StoreProfile:
    """Find a profile by store key or by the domain of a product URL.

    Unknown domains get a generic profile guessed from the TLD.
    """
    if url_or_key in STORES:
        return STORES[url_or_key]
    host = _host(url_or_key)
    for store in STORES.values():
        if any(host == d or host.endswith("." + d) for d in store.domains):
            return store
    country, currency = "US", "USD"
    for tld, cc in _TLD_COUNTRY.items():
        if host.endswith(tld):
            country, currency = cc
            break
    regions = _COUNTRY_REGIONS.get(country, ("EU", "GLOBAL"))
    events = _COUNTRY_EVENTS.get(country, EU_EVENTS)
    key = host or url_or_key
    return StoreProfile(key, key, (host,), country, currency, events, regions=regions,
                        notes="Unknown store - country and currency guessed from the domain.")


def with_overrides(store: StoreProfile, overrides: dict) -> StoreProfile:
    """Apply a [stores.<key>] table from config.toml."""
    allowed = {"name", "country", "currency", "shipping_flat", "shipping_free_over", "collects_import_vat"}
    return replace(store, **{k: v for k, v in overrides.items() if k in allowed})
