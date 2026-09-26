"""Landed-cost calculation: what an offer really costs delivered to your door.

Domestic offers: shelf price + shipping (VAT is already included).
Imports: goods + shipping, plus customs duty and VAT when the shipment value is
above the destination's personal-import exemption thresholds, plus a courier
clearance fee whenever taxes are collected on arrival.

Tax rules change - every number here can be overridden in config.toml under
``[destination]``. Defaults reflect the rules known at the time of writing and
are estimates, not tax advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from hawkdrop.currency import FX
from hawkdrop.stores import StoreProfile


@dataclass(frozen=True)
class Destination:
    code: str
    name: str
    currency: str
    vat_rate: float
    vat_exempt_usd: float  # goods value (USD) up to which no VAT is charged on import
    duty_exempt_usd: float  # goods value (USD) up to which no customs duty is charged
    threshold_includes_shipping: bool = False
    clearance_fee: float = 0.0  # courier handling fee (destination currency) when taxes are due
    duty_rates: dict[str, float] = field(default_factory=dict)
    same_market: tuple[str, ...] = ()  # origin countries treated as domestic (e.g. EU single market)

    def duty_for(self, category: str) -> float:
        return self.duty_rates.get(category, self.duty_rates.get("default", 0.0))


DESTINATIONS: dict[str, Destination] = {d.code: d for d in [
    Destination(
        "IL", "Israel", "ILS", vat_rate=0.18, vat_exempt_usd=75.0, duty_exempt_usd=500.0,
        clearance_fee=35.0,
        duty_rates={"electronics": 0.0, "computers": 0.0, "phones": 0.0, "cameras": 0.0, "appliances": 0.0,
                    "toys": 0.0, "clothing": 0.12, "shoes": 0.12, "furniture": 0.08, "default": 0.06},
    ),
    Destination(
        "US", "United States", "USD", vat_rate=0.0, vat_exempt_usd=0.0, duty_exempt_usd=0.0,
        duty_rates={"default": 0.10}, clearance_fee=0.0,
    ),
    Destination(
        "EU", "European Union", "EUR", vat_rate=0.21, vat_exempt_usd=0.0, duty_exempt_usd=160.0,
        duty_rates={"electronics": 0.0, "computers": 0.0, "phones": 0.0, "clothing": 0.12, "default": 0.04},
        clearance_fee=10.0,
        same_market=("DE", "FR", "IT", "ES", "NL", "BE", "AT", "IE", "PL", "PT", "SE", "FI", "DK", "EU"),
    ),
    Destination(
        "UK", "United Kingdom", "GBP", vat_rate=0.20, vat_exempt_usd=0.0, duty_exempt_usd=170.0,
        duty_rates={"electronics": 0.0, "computers": 0.0, "clothing": 0.12, "default": 0.04},
        clearance_fee=8.0,
    ),
]}


def destination_from_config(cfg: dict) -> Destination:
    code = str(cfg.get("code", "IL")).upper()
    base = DESTINATIONS.get(code) or DESTINATIONS["IL"]
    allowed = {"name", "currency", "vat_rate", "vat_exempt_usd", "duty_exempt_usd",
               "threshold_includes_shipping", "clearance_fee"}
    overrides = {k: v for k, v in cfg.items() if k in allowed}
    if "duty_rates" in cfg:
        overrides["duty_rates"] = {**base.duty_rates, **cfg["duty_rates"]}
    return replace(base, **overrides)


@dataclass
class LandedCost:
    currency: str
    item: float
    shipping: float
    duty: float = 0.0
    vat: float = 0.0
    fees: float = 0.0
    domestic: bool = True
    shipping_known: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.item + self.shipping + self.duty + self.vat + self.fees

    @property
    def taxes(self) -> float:
        return self.duty + self.vat + self.fees


def landed_cost(
    price: float,
    currency: str,
    store: StoreProfile,
    dest: Destination,
    fx: FX,
    category: str = "default",
    shipping: float | None = None,
    shipping_currency: str | None = None,
) -> LandedCost:
    """Compute the delivered cost of one unit in the destination currency.

    ``shipping`` overrides the store's shipping policy (in ``shipping_currency``,
    defaulting to the price currency).
    """
    to_dest = lambda amount, cur: fx.convert(amount, cur, dest.currency)  # noqa: E731
    notes: list[str] = []

    shipping_known = True
    if shipping is not None:
        ship_dest = to_dest(shipping, shipping_currency or currency)
    else:
        policy = store.shipping_for(fx.convert(price, currency, store.currency))
        if policy is None:
            shipping_known = False
            ship_dest = 0.0
            notes.append("shipping cost unknown (assumed 0) - set it with --shipping")
        else:
            ship_dest = to_dest(policy, store.currency)

    item_dest = to_dest(price, currency)
    domestic = store.country == dest.code or store.country in dest.same_market
    lc = LandedCost(dest.currency, item_dest, ship_dest, domestic=domestic,
                    shipping_known=shipping_known, notes=notes)
    if domestic:
        return lc

    goods_usd = fx.convert(price, currency, "USD")
    ship_usd = fx.convert(ship_dest, dest.currency, "USD")
    threshold_value = goods_usd + (ship_usd if dest.threshold_includes_shipping else 0.0)
    cif = item_dest + ship_dest  # customs value: goods + freight

    if threshold_value > dest.duty_exempt_usd:
        rate = dest.duty_for(category)
        lc.duty = cif * rate
        if rate:
            notes.append(f"customs duty {rate:.0%} (value over ${dest.duty_exempt_usd:.0f})")
    if dest.vat_rate and threshold_value > dest.vat_exempt_usd:
        lc.vat = (cif + lc.duty) * dest.vat_rate
        notes.append(f"import VAT {dest.vat_rate:.0%} (value over ${dest.vat_exempt_usd:.0f})")
    elif dest.vat_rate:
        notes.append(f"under ${dest.vat_exempt_usd:.0f} personal-import exemption - no VAT")

    if lc.duty or lc.vat:
        if store.collects_import_vat:
            notes.append("taxes collected at checkout by the store")
        else:
            lc.fees = dest.clearance_fee
            if dest.clearance_fee:
                notes.append("courier clearance fee")
    return lc
