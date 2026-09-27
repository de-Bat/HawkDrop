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
import sys
from dataclasses import dataclass, fields, replace

from hawksense.rate_tables import DEALTAS_US, REDBOX_EUROPE, REDBOX_US, SHIPITO_US

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
    min_price: float = 0.0  # minimum charge per package
    vol_free_cm: tuple[float, float, float] | None = None  # parcels up to this size are charged by weight only
    max_kg: float | None = None  # heaviest chargeable weight the service accepts
    table: tuple[tuple[float, float], ...] | None = None  # published price list: (up to kg, price) rows;
    # when set it decides the price, and first/additional only apply beyond its last row

    def _small(self, dims_cm: tuple[float, float, float]) -> bool:
        return bool(self.vol_free_cm) and all(d <= f for d, f in zip(sorted(dims_cm), sorted(self.vol_free_cm)))

    def chargeable_kg(self, weight_kg: float, dims_cm: tuple[float, float, float] | None = None) -> float:
        kg = weight_kg
        if dims_cm and self.vol_divisor and not self._small(dims_cm):
            kg = max(kg, dims_cm[0] * dims_cm[1] * dims_cm[2] / self.vol_divisor)
        kg = max(kg, self.min_kg)
        if self.table:
            for up_to, _ in self.table:
                if kg <= up_to + 1e-9:
                    return up_to
            last = self.table[-1][0]
            return last + math.ceil(round((kg - last) / self.step_kg, 6)) * self.step_kg
        kg = max(kg, self.first_kg)
        extra = math.ceil(round((kg - self.first_kg) / self.step_kg, 6)) if kg > self.first_kg else 0
        return self.first_kg + extra * self.step_kg

    def price(self, chargeable_kg: float) -> float:
        if self.table:
            for up_to, price in self.table:
                if chargeable_kg <= up_to + 1e-9:
                    return max(self.min_price, price)
            last_kg, last_price = self.table[-1]
            steps = max(0, round((chargeable_kg - last_kg) / self.step_kg))
            return max(self.min_price, last_price + steps * self.additional)
        extra = max(0, round((chargeable_kg - self.first_kg) / self.step_kg))
        return max(self.min_price, self.first + extra * self.additional)


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
    verified: tuple[str, ...] = ()  # values checked against the service's published terms, with the source
    customs_freight_per_kg: float | None = None  # freight the service declares to customs per chargeable kg
    # (instead of the shipping price), in the service's currency

    def warehouse(self, code: str) -> Warehouse | None:
        code = code.upper()
        return next((w for w in self.warehouses if w.code == code), None)


def _usd(first, additional, **kw) -> RateCard:
    return RateCard("USD", first, additional, **kw)


# Checked 2026-09-27. RedBox, DealTas and Shipito were read from their websites (price tables in
# rate_tables.py); for the others the "verified" values come from web-search excerpts of their own
# pages or reviews (named in each entry), since their sites weren't reachable. Everything else is an
# estimate. Check the live price lists and override in config.toml or the app.
DEALTAS_SMALL_CM = (43.0, 30.0, 10.0)  # 17 x 12 x 4 in: charged by weight only

FORWARDERS: dict[str, Forwarder] = {f.key: f for f in [
    Forwarder(
        "dealtas", "Dealtas", "USD",
        (Warehouse("US", "US", "Boston, Massachusetts",
                   _usd(25.0, 5.5, table=DEALTAS_US, vol_free_cm=DEALTAS_SMALL_CM),
                   sales_tax=0.0625, transit="7-14 business days"),),
        collects_import_taxes=True, tax_handling_fee=0.0, customs_freight_per_kg=5.0,
        notes="Israeli service, warehouse in Boston (US stores charge MA sales tax). \"Special Air\" "
              "prices; Priority (UPS/FedEx, 3-5 days) costs more. One price with no fees: repacking, "
              "consolidation and insurance up to $100 included. Taxes are collected up front and "
              "customs cleared by DealTas. 14 days' free storage, then $6/week. Up to 25% off for "
              "frequent shippers.",
        verified=("full Special Air price table to 20 kg: $25 up to 0.5 kg, $32 up to 1 kg ... $260 for "
                  "20 kg (dealtas.com rates, 2026-09-27)",
                  "physical weight up to 43x30x10 cm, else the higher of physical and volumetric "
                  "(L x W x H / 5000); no weight limit (dealtas.com FAQ)",
                  "warehouse in Boston; taxes collected up front; declared shipping for customs is "
                  "$5 per chargeable kg; 14 days' free storage (dealtas.com FAQ)"),
    ),
    Forwarder(
        "redbox", "RedBox", "USD",
        (Warehouse("US", "US", "Edison, New Jersey",
                   _usd(15.0, 1.0, first_kg=0.25, step_kg=0.1, table=REDBOX_US, max_kg=20.0),
                   sales_tax=0.06625, transit="14-21 business days"),
         Warehouse("EU", "NL", "Nieuw-Vennep, Netherlands",
                   _usd(15.0, 0.5, first_kg=0.25, step_kg=0.1, table=REDBOX_EUROPE, max_kg=20.0),
                   transit="14-21 business days")),
        collects_import_taxes=True, tax_handling_fee=0.0,
        notes="Israeli service with warehouses in New Jersey (US stores charge NJ sales tax) and the "
              "Netherlands (serves EU stores). Prices are to a pickup point; home delivery of small "
              "parcels (up to 4 kg / 30x30x40 cm) is $6 more. Consolidation $3 per package, repacking "
              "$5, 21 days' free storage. Max 20 kg and 60x40x40 cm. Taxes are paid through RedBox.",
        verified=("full US and Europe price tables per 100 g, pickup-point prices "
                  "(redboxparcel.com price list, 2026-09-27)",
                  "physical or volumetric weight (L x W x H / 5000), whichever is higher; max 20 kg, "
                  "60x40x40 cm; home delivery +$6 for small parcels; consolidation $3/package; "
                  "US warehouse in Edison, NJ; Europe warehouse in the Netherlands (redboxparcel.com)"),
    ),
    Forwarder(
        "zipy", "Zipy (buys for you)", "USD",
        (Warehouse("US", "US", "United States", _usd(10.0, 4.5), transit="10-20 days"),
         Warehouse("UK", "UK", "United Kingdom", _usd(12.0, 5.0), transit="10-20 days"),
         Warehouse("DE", "DE", "Germany", _usd(12.0, 5.0), transit="10-20 days"),
         Warehouse("CN", "CN", "China", _usd(6.0, 3.0), transit="14-30 days")),
        service_fee_rate=0.07, service_fee_min=4.0, collects_import_taxes=True, needs_address=False,
        notes="Israeli 'buy for me' service in Hebrew (AliExpress, eBay, Amazon, Allegro): it orders the "
              "item for you, so no address is needed; offers a customs refund guarantee. Fees and rates "
              "are estimates.",
    ),
    Forwarder(
        "myus", "MyUS", "USD",
        (Warehouse("US", "US", "Sarasota, Florida", _usd(24.0, 6.0), sales_tax=0.07, transit="3-6 days (express)"),),
        handling_fee=0.0, insurance_rate=0.0,
        notes="Express courier (DHL/FedEx); the courier collects Israeli taxes and adds a clearance fee. "
              "Premium membership ($9.99/month) gives lower rates and free consolidation. Rates to Israel "
              "are estimates.",
        verified=("shipping rates start at $9.99; Premium membership $9.99/month after a 30-day trial, "
                  "free consolidation and 30 days' storage (myus.com pricing)",),
    ),
    Forwarder(
        "shipito", "Shipito", "USD",
        (Warehouse("US", "US", "Portland, Oregon", _usd(31.53, 10.5, table=SHIPITO_US),
                   sales_tax=0.0, transit="5-15 business days"),
         Warehouse("US-CA", "US", "Torrance, California", _usd(31.53, 10.5, table=SHIPITO_US),
                   sales_tax=0.10, transit="5-15 business days")),
        handling_fee=3.25,
        notes="Prices are the cheapest carrier Shipito quotes to Israel at each weight (its own "
              "Priority Parcel, USPS Priority Mail, DHL); the courier or post collects Israeli taxes. "
              "The sales-tax-free Oregon address needs Premium, which also cuts handling to $2.25 (set "
              "handling_fee = 2.25) and consolidation from $5.50 to $3.25 per package. Free storage "
              "7 days (45 with Premium); insurance from $3.",
        verified=("carrier quotes to Israel from shipito.com's calculator, 0.25-20 kg: $31.53 for 250 g, "
                  "$63.03 for 1 kg, $125.74 for 5 kg, $510.79 for 20 kg (2026-09-27)",
                  "processing fee $3.25 per package ($2.25 Premium); consolidation $5.50 ($3.25); "
                  "storage 7 days free (45 Premium); insurance from $3 (shipito.com pricing, 2026-09-27)",
                  "the Oregon tax-free warehouse is Premium-only (shipito.com FAQ)"),
    ),
    Forwarder(
        "stackry", "Stackry", "USD",
        (Warehouse("US", "US", "New Hampshire", _usd(18.0, 5.0), sales_tax=0.0, transit="4-10 days"),),
        handling_fee=1.5,
        notes="New Hampshire address: no sales tax. Receiving $1-2 per package depending on destination "
              "(1.5 used), consolidation $3. Shipping rates are estimates.",
        verified=("receiving fee $1-2 per package by destination, consolidation $3 per package "
                  "(parcelforward.net review of stackry.com pricing)",),
    ),
    Forwarder(
        "planetexpress", "Planet Express", "USD",
        (Warehouse("US", "US", "Torrance, California", _usd(17.0, 5.0), sales_tax=0.10, transit="4-10 days"),),
        handling_fee=2.0,
        notes="Consolidation $5 plus $2 per package. Shipping rates are estimates.",
        verified=("handling fee $2 per incoming package; consolidation $5 + $2 per package "
                  "(planetexpress.com pricing, via reviews)",),
    ),
    Forwarder(
        "forward2me", "Forward2me", "GBP",
        (Warehouse("UK", "UK", "United Kingdom", RateCard("GBP", 15.0, 3.0), transit="3-7 days"),),
        notes="UK address. UK prices include 20% UK VAT, which is not refunded on forwarded orders. "
              "Rates are estimates.",
    ),
]}


def _rate_from(cfg: dict, base: RateCard | None) -> RateCard:
    names = {f.name for f in fields(RateCard)}
    overrides = {k: v for k, v in cfg.items() if k in names}
    if overrides.get("table") is not None:  # [[kg, price], ...] from config.toml or the rules feed
        overrides["table"] = tuple((float(kg), float(price)) for kg, price in overrides["table"]) or None
    if isinstance(overrides.get("vol_free_cm"), list):
        overrides["vol_free_cm"] = tuple(float(x) for x in overrides["vol_free_cm"])
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


def forwarders_from_config(cfg: dict | None, strict: bool = True) -> dict[str, Forwarder]:
    """Built-in services with the [forwarders.*] tables from config.toml applied.

    ``strict=False`` skips entries that can't be used (e.g. an override for a warehouse a service
    no longer has) with a warning, instead of stopping.
    """
    out = dict(FORWARDERS)

    def problem(message: str):
        if strict:
            raise SystemExit(f"error: {message}")
        print(f"warning: ignoring {message}", file=sys.stderr)

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
                problem(f"[forwarders.{key}.warehouses.{code}] {exc}")
                continue
            warehouses = [w for w in warehouses if w.code != wh.code] + [wh]
        overrides = {k: v for k, v in table.items() if k in simple}
        if base:
            out[key] = replace(base, warehouses=tuple(warehouses), **overrides)
        elif warehouses:
            out[key] = Forwarder(key, overrides.pop("name", key), overrides.pop("currency", "USD"),
                                 tuple(warehouses), **overrides)
        else:
            problem(f"[forwarders.{key}] is not a built-in service - define at least one warehouse")
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
