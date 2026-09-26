"""Package forwarders ("shipping proxies"): buy from a store that won't ship to you,
have it delivered to your personal address at the forwarder's warehouse abroad,
and let the forwarder ship it on.

Each service has its own rules, modelled here per service and per warehouse:

* international shipping from a rate card: a price for the first weight step
  plus a price per extra step, charged on the greater of actual and volumetric
  weight, with a minimum chargeable weight,
* sales tax charged by the store in the warehouse's state (Delaware, Oregon and
  New Hampshire have none), taken from your address when it names a US state,
* per-package handling, insurance and "buy for me" service fees,
* who pays the import taxes on arrival: the forwarder (usually for a fixed fee)
  or the courier, who adds its clearance fee.

The rates and fees below are rough estimates for shipping to Israel at the time
of writing. They change often: check each service's price list and override any
value in config.toml::

    [forwarders.dealtas]
    handling_fee = 0
    tax_handling_fee = 20

    [forwarders.dealtas.warehouses.US]
    first = 12.5          # first 0.5 kg, in the warehouse currency
    additional = 5        # each extra 0.5 kg

A table for a key that is not built in defines a new service (it needs at least
one warehouse).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, fields, replace

EU_COUNTRIES = frozenset({"DE", "FR", "IT", "ES", "NL", "BE", "AT", "IE", "PL", "PT", "SE", "FI", "DK", "EU"})

# Approximate combined state + average local sales tax. Stores charge it when they
# ship to a forwarder's warehouse in that state; direct international orders are exempt.
US_SALES_TAX = {
    "AK": 0.0, "DE": 0.0, "MT": 0.0, "NH": 0.0, "OR": 0.0,
    "AL": 0.092, "AR": 0.094, "AZ": 0.084, "CA": 0.088, "CO": 0.078, "CT": 0.064, "DC": 0.06, "FL": 0.07,
    "GA": 0.074, "HI": 0.045, "IA": 0.069, "ID": 0.06, "IL": 0.089, "IN": 0.07, "KS": 0.087, "KY": 0.06,
    "LA": 0.095, "MA": 0.0625, "MD": 0.06, "ME": 0.055, "MI": 0.06, "MN": 0.075, "MO": 0.084, "MS": 0.071,
    "NC": 0.07, "ND": 0.07, "NE": 0.07, "NJ": 0.066, "NM": 0.076, "NV": 0.082, "NY": 0.085, "OH": 0.072,
    "OK": 0.09, "PA": 0.063, "RI": 0.07, "SC": 0.075, "SD": 0.061, "TN": 0.095, "TX": 0.082, "UT": 0.072,
    "VA": 0.058, "VT": 0.063, "WA": 0.094, "WI": 0.054, "WV": 0.065, "WY": 0.054,
}

# Used when an item has no weight set: typical boxed shipping weight per category (kg).
CATEGORY_WEIGHT_KG = {
    "electronics": 1.0, "computers": 3.0, "phones": 0.5, "cameras": 1.2, "appliances": 5.0,
    "clothing": 0.6, "shoes": 1.5, "toys": 1.0, "furniture": 15.0, "default": 1.0,
}


@dataclass(frozen=True)
class RateCard:
    """International shipping price from the warehouse to you."""
    currency: str
    first: float  # price of the first ``first_kg``
    additional: float  # price of each further ``step_kg``
    first_kg: float = 0.5
    step_kg: float = 0.5
    min_kg: float = 0.0  # minimum chargeable weight
    vol_divisor: float | None = 5000.0  # cm³ per kg for volumetric weight; None = weight only

    def chargeable_kg(self, weight_kg: float, dims_cm: tuple[float, float, float] | None = None) -> float:
        kg = weight_kg
        if dims_cm and self.vol_divisor:
            kg = max(kg, dims_cm[0] * dims_cm[1] * dims_cm[2] / self.vol_divisor)
        kg = max(kg, self.min_kg, self.first_kg)
        extra = math.ceil(round((kg - self.first_kg) / self.step_kg, 6)) if kg > self.first_kg else 0
        return self.first_kg + extra * self.step_kg

    def price(self, chargeable_kg: float) -> float:
        extra = max(0, round((chargeable_kg - self.first_kg) / self.step_kg))
        return self.first + extra * self.additional


@dataclass(frozen=True)
class Warehouse:
    code: str  # "US", "US-CA", "UK", ... unique within the service
    country: str  # where the warehouse is: stores in this country (or the EU market) can ship to it
    location: str
    rate: RateCard
    sales_tax: float = 0.0  # charged by stores shipping here, unless your address says otherwise
    transit: str = ""  # typical delivery time to you

    def serves(self, store_country: str) -> bool:
        return store_country == self.country or (store_country in EU_COUNTRIES and self.country in EU_COUNTRIES)


@dataclass(frozen=True)
class Forwarder:
    key: str
    name: str
    currency: str  # currency of the fees below
    warehouses: tuple[Warehouse, ...]
    handling_fee: float = 0.0  # per package
    insurance_rate: float = 0.0  # share of the goods value; 0 = not included
    insurance_min: float = 0.0
    service_fee_rate: float = 0.0  # "buy for me" services: share of the order value
    service_fee_min: float = 0.0
    collects_import_taxes: bool = False  # forwarder pays VAT/duty on arrival and bills you
    tax_handling_fee: float = 0.0  # its fee for doing so, when taxes are due
    needs_address: bool = True  # you get a personal address/suite at each warehouse
    notes: str = ""

    def warehouse(self, code: str) -> Warehouse | None:
        code = code.upper()
        return next((w for w in self.warehouses if w.code == code), None)


def _usd(first, additional, **kw) -> RateCard:
    return RateCard("USD", first, additional, **kw)


FORWARDERS: dict[str, Forwarder] = {f.key: f for f in [
    Forwarder(
        "dealtas", "Dealtas", "USD",
        (Warehouse("US", "US", "Delaware", _usd(13.0, 5.5), sales_tax=0.0, transit="7-14 days"),),
        collects_import_taxes=True, tax_handling_fee=5.0,
        notes="Israeli service with a tax-free Delaware address; delivers to your door with customs "
              "cleared and taxes billed through the service.",
    ),
    Forwarder(
        "redbox", "RedBox", "USD",
        (Warehouse("US", "US", "Delaware", _usd(12.0, 5.0), sales_tax=0.0, transit="7-14 days"),
         Warehouse("UK", "UK", "United Kingdom", RateCard("GBP", 11.0, 4.0), transit="7-14 days")),
        collects_import_taxes=True, tax_handling_fee=5.0,
        notes="Israeli service with US and UK addresses; handles customs clearance for you.",
    ),
    Forwarder(
        "zipy", "Zipy (buys for you)", "USD",
        (Warehouse("US", "US", "United States", _usd(10.0, 4.5), transit="10-20 days"),
         Warehouse("UK", "UK", "United Kingdom", _usd(12.0, 5.0), transit="10-20 days"),
         Warehouse("DE", "DE", "Germany", _usd(12.0, 5.0), transit="10-20 days"),
         Warehouse("CN", "CN", "China", _usd(6.0, 3.0), transit="14-30 days")),
        service_fee_rate=0.07, service_fee_min=4.0, collects_import_taxes=True, needs_address=False,
        notes="Israeli 'buy for me' service: it orders the item for you, so no address is needed. "
              "Price quoted up front including shipping and taxes; charges a service fee.",
    ),
    Forwarder(
        "myus", "MyUS", "USD",
        (Warehouse("US", "US", "Sarasota, Florida", _usd(24.0, 6.0), sales_tax=0.07, transit="3-6 days (express)"),),
        handling_fee=0.0, insurance_rate=0.0,
        notes="Express courier (DHL/FedEx); the courier collects Israeli taxes and adds a clearance fee. "
              "Membership plans change the handling fees.",
    ),
    Forwarder(
        "shipito", "Shipito", "USD",
        (Warehouse("US", "US", "Portland, Oregon", _usd(19.0, 5.0), sales_tax=0.0, transit="5-15 days"),
         Warehouse("US-CA", "US", "Torrance, California", _usd(19.0, 5.0), sales_tax=0.10, transit="5-15 days")),
        handling_fee=2.5,
        notes="Oregon warehouse is sales-tax free.",
    ),
    Forwarder(
        "stackry", "Stackry", "USD",
        (Warehouse("US", "US", "Salem, New Hampshire", _usd(18.0, 5.0), sales_tax=0.0, transit="4-10 days"),),
        notes="New Hampshire address: no sales tax; free consolidation.",
    ),
    Forwarder(
        "planetexpress", "Planet Express", "USD",
        (Warehouse("US", "US", "Torrance, California", _usd(17.0, 5.0), sales_tax=0.10, transit="4-10 days"),),
        handling_fee=2.0,
    ),
    Forwarder(
        "forward2me", "Forward2me", "GBP",
        (Warehouse("UK", "UK", "United Kingdom", RateCard("GBP", 15.0, 3.0), transit="3-7 days"),),
        notes="UK address. UK prices include 20% UK VAT, which is not refunded on forwarded orders.",
    ),
]}


def _rate_from(cfg: dict, base: RateCard | None) -> RateCard:
    names = {f.name for f in fields(RateCard)}
    overrides = {k: v for k, v in cfg.items() if k in names}
    if base is not None:
        return replace(base, **overrides)
    missing = {"currency", "first", "additional"} - overrides.keys()
    if missing:
        raise ValueError(f"warehouse rate needs {', '.join(sorted(missing))}")
    return RateCard(**overrides)


def _warehouse_from(code: str, cfg: dict, base: Warehouse | None) -> Warehouse:
    rate = _rate_from(cfg, base.rate if base else None)
    names = {"country", "location", "sales_tax", "transit"}
    overrides = {k: v for k, v in cfg.items() if k in names}
    if base is not None:
        return replace(base, rate=rate, **overrides)
    return Warehouse(code.upper(), str(cfg.get("country", code)).upper()[:2], str(cfg.get("location", code)), rate,
                     float(cfg.get("sales_tax", 0.0)), str(cfg.get("transit", "")))


def forwarders_from_config(cfg: dict | None) -> dict[str, Forwarder]:
    """Built-in services with the [forwarders.*] tables from config.toml applied."""
    out = dict(FORWARDERS)
    simple = {f.name for f in fields(Forwarder)} - {"key", "warehouses"}
    for key, table in (cfg or {}).items():
        if not isinstance(table, dict):
            continue
        base = out.get(key)
        wh_cfg = table.get("warehouses") or {}
        warehouses = list(base.warehouses) if base else []
        for code, wcfg in wh_cfg.items():
            existing = next((w for w in warehouses if w.code == code.upper()), None)
            try:
                wh = _warehouse_from(code, wcfg, existing)
            except (TypeError, ValueError) as exc:
                raise SystemExit(f"error: [forwarders.{key}.warehouses.{code}] {exc}") from None
            warehouses = [w for w in warehouses if w.code != wh.code] + [wh]
        overrides = {k: v for k, v in table.items() if k in simple}
        if base:
            out[key] = replace(base, warehouses=tuple(warehouses), **overrides)
        elif warehouses:
            out[key] = Forwarder(key, overrides.pop("name", key), overrides.pop("currency", "USD"),
                                 tuple(warehouses), **overrides)
        else:
            raise SystemExit(f"error: [forwarders.{key}] is not a built-in service - define at least one warehouse")
    return out


_STATE_RE = re.compile(r"\b([A-Z]{2})\b[\s,]+\d{5}(?:-\d{4})?\b")


def state_from_address(address: str) -> str | None:
    """'…, Wilmington, DE 19808' -> 'DE'."""
    for m in _STATE_RE.finditer(address or ""):
        if m.group(1) in US_SALES_TAX:
            return m.group(1)
    return None


def parse_dims(text: str | None) -> tuple[float, float, float] | None:
    """'30x20x10' (cm) -> (30.0, 20.0, 10.0)."""
    if not text:
        return None
    parts = re.split(r"\s*[x×*,]\s*", str(text).strip().lower())
    try:
        dims = tuple(float(p) for p in parts)
    except ValueError:
        raise ValueError(f"dimensions must look like 30x20x10 (cm), got {text!r}") from None
    if len(dims) != 3 or min(dims) <= 0:
        raise ValueError(f"dimensions must look like 30x20x10 (cm), got {text!r}")
    return dims


@dataclass
class Account:
    """Your registration with a forwarder at one of its warehouses."""
    id: int
    forwarder: str
    warehouse: str
    address: str = ""
    suite: str = ""
    sales_tax: float | None = None  # overrides the rate derived from the address / warehouse
    active: bool = True

    def sales_tax_for(self, wh: Warehouse) -> tuple[float, str]:
        """Effective sales tax rate and where it came from."""
        if self.sales_tax is not None:
            return self.sales_tax, "your setting"
        if wh.country == "US":
            state = state_from_address(self.address)
            if state:
                return US_SALES_TAX[state], f"{state} address"
        return wh.sales_tax, wh.location


@dataclass
class Route:
    forwarder: Forwarder
    warehouse: Warehouse
    account: Account | None = None  # None = a service you haven't set up (shown as a suggestion)

    @property
    def key(self) -> str:
        return f"{self.forwarder.key}:{self.warehouse.code}"

    @property
    def label(self) -> str:
        return f"via {self.forwarder.name} ({self.warehouse.location})"
