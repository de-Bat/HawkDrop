"""Landed-cost calculation: what an offer really costs delivered to your door.

Domestic offers: shelf price + shipping (VAT is already included).
Imports: goods + shipping, plus customs duty and VAT when the shipment value is
above the destination's personal-import exemption thresholds, plus a courier
clearance fee whenever taxes are collected on arrival.
Forwarded orders (via a package forwarder's warehouse abroad): see
``forwarded_cost`` and ``hawksense.forwarders``.

Tax rules change - every number here can be overridden in config.toml under
``[destination]``. Defaults reflect the rules known at the time of writing and
are estimates, not tax advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from hawksense.currency import FX
from hawksense.forwarders import CATEGORY_WEIGHT_KG, Route
from hawksense.stores import StoreProfile


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
    sales_tax: float = 0.0  # charged by a store shipping to a forwarder's US address
    domestic: bool = True
    shipping_known: bool = True
    notes: list[str] = field(default_factory=list)
    route: str = "direct"  # "direct" or "<forwarder>:<warehouse>"
    route_label: str = "direct from the store"
    lines: list[tuple[str, float]] = field(default_factory=list)  # itemised shipping/fees for forwarded routes
    set_up: bool = True  # False for a forwarder you haven't registered with (a suggestion)

    @property
    def total(self) -> float:
        return self.item + self.shipping + self.duty + self.vat + self.fees + self.sales_tax

    @property
    def taxes(self) -> float:
        return self.duty + self.vat + self.fees


def _import_taxes(lc: LandedCost, goods: float, freight: float, goods_usd: float, freight_usd: float,
                  dest: Destination, category: str) -> bool:
    """Add customs duty and import VAT to ``lc``; returns True when any are due.

    ``goods`` and ``freight`` are in the destination currency; the customs value is
    goods + freight (CIF). Exemption thresholds are compared in USD.
    """
    notes = lc.notes
    threshold_value = goods_usd + (freight_usd if dest.threshold_includes_shipping else 0.0)
    cif = goods + freight
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
    return bool(lc.duty or lc.vat)


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
    if _import_taxes(lc, item_dest, ship_dest, goods_usd, ship_usd, dest, category):
        if store.collects_import_vat:
            notes.append("taxes collected at checkout by the store")
        else:
            lc.fees = dest.clearance_fee
            if dest.clearance_fee:
                notes.append("courier clearance fee")
    return lc


def forwarded_cost(
    price: float,
    currency: str,
    store: StoreProfile,
    route: Route,
    dest: Destination,
    fx: FX,
    category: str = "default",
    weight_kg: float | None = None,
    dims_cm: tuple[float, float, float] | None = None,
    local_shipping: float | None = None,
    local_shipping_currency: str | None = None,
) -> LandedCost:
    """Delivered cost when the store ships to a forwarder's warehouse and the forwarder ships to you.

    ``local_shipping`` is what the store charges to the warehouse (overrides the store's
    domestic shipping policy).
    """
    fwd, wh = route.forwarder, route.warehouse
    to_dest = lambda amount, cur: fx.convert(amount, cur, dest.currency)  # noqa: E731
    fee = lambda amount: to_dest(amount, fwd.currency)  # noqa: E731
    notes: list[str] = []
    lines: list[tuple[str, float]] = []
    item_dest = to_dest(price, currency)

    # 1. store -> warehouse
    if local_shipping is not None:
        local = to_dest(local_shipping, local_shipping_currency or currency)
    else:
        policy = store.local_shipping_for(fx.convert(price, currency, store.currency))
        if policy is None:
            local = 0.0
            notes.append(f"shipping to the {wh.location} warehouse assumed free - set it with --local-shipping")
        else:
            local = to_dest(policy, store.currency)
    if local:
        lines.append((f"store shipping to {wh.location}", local))

    # 2. sales tax at the warehouse address
    tax_rate, tax_source = route.account.sales_tax_for(wh) if route.account else (wh.sales_tax, wh.location)
    sales_tax = item_dest * tax_rate
    if sales_tax:
        notes.append(f"sales tax {tax_rate:.1%} ({tax_source})")

    # 3. warehouse -> you
    if weight_kg is None:
        weight_kg = CATEGORY_WEIGHT_KG.get(category, CATEGORY_WEIGHT_KG["default"])
        notes.append(f"weight unknown - assumed {weight_kg:g} kg (set it with --weight)")
    chargeable = wh.rate.chargeable_kg(weight_kg, dims_cm)
    intl = to_dest(wh.rate.price(chargeable), wh.rate.currency)
    lines.append((f"{fwd.name} shipping, {chargeable:g} kg", intl))
    if dims_cm and chargeable > wh.rate.chargeable_kg(weight_kg):
        notes.append(f"charged by volumetric weight ({chargeable:g} kg)")

    # 4. the service's own fees
    fees = 0.0
    goods_value = item_dest + sales_tax
    if fwd.handling_fee:
        fees += fee(fwd.handling_fee)
        lines.append(("handling", fee(fwd.handling_fee)))
    insurance = 0.0
    if fwd.insurance_rate:
        insurance = max(fee(fwd.insurance_min), goods_value * fwd.insurance_rate)
        fees += insurance
        lines.append((f"insurance {fwd.insurance_rate:.1%}", insurance))
    if fwd.service_fee_rate:
        service = max(fee(fwd.service_fee_min), (goods_value + local) * fwd.service_fee_rate)
        fees += service
        lines.append((f"service fee {fwd.service_fee_rate:.0%}", service))

    lc = LandedCost(dest.currency, item_dest, local + intl, fees=fees, sales_tax=sales_tax, domestic=False,
                    notes=notes, route=route.key, route_label=route.label, lines=lines,
                    set_up=route.account is not None)

    # 5. import taxes on arrival: goods (incl. the sales tax you paid) + freight + insurance
    goods_usd = fx.convert(goods_value, dest.currency, "USD")
    freight = intl + insurance
    if _import_taxes(lc, goods_value, freight, goods_usd, fx.convert(freight, dest.currency, "USD"), dest, category):
        if fwd.collects_import_taxes:
            notes.append(f"taxes paid through {fwd.name}")
            if fwd.tax_handling_fee:
                lc.fees += fee(fwd.tax_handling_fee)
                lines.append(("tax handling", fee(fwd.tax_handling_fee)))
        elif dest.clearance_fee:
            lc.fees += dest.clearance_fee
            lines.append(("courier clearance fee", dest.clearance_fee))
    if wh.transit:
        notes.append(f"delivery {wh.transit}")
    return lc
