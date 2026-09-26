"""JSON views of tracker data, shared by the web server (and handy for scripts)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from hawkdrop import __version__
from hawkdrop.calendar_events import EVENTS, upcoming_events
from hawkdrop.db import Item
from hawkdrop.forecast import Advice, Candidate
from hawkdrop.forwarders import Account, Forwarder
from hawkdrop.landed import LandedCost
from hawkdrop.stores import STORES
from hawkdrop.tracker import CheckResult, Quote, Tracker

CATEGORIES = ["electronics", "computers", "phones", "cameras", "appliances", "clothing", "shoes", "toys",
              "furniture", "default"]


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


def meta(t: Tracker) -> dict:
    d = t.dest
    return {
        "version": __version__,
        "destination": {"code": d.code, "name": d.name, "currency": d.currency, "vat_rate": d.vat_rate,
                        "vat_exempt_usd": d.vat_exempt_usd, "duty_exempt_usd": d.duty_exempt_usd},
        "fx_source": t.fx.source,
        "categories": CATEGORIES,
    }


def stores() -> list[dict]:
    return [{"key": s.key, "name": s.name, "country": s.country, "currency": s.currency,
             "shipping_flat": s.shipping_flat, "shipping_free_over": s.shipping_free_over,
             "collects_import_vat": s.collects_import_vat, "ships_abroad": s.ships_abroad, "notes": s.notes}
            for s in STORES.values()]


def forwarder(f: Forwarder) -> dict:
    return {
        "key": f.key, "name": f.name, "currency": f.currency, "needs_address": f.needs_address,
        "handling_fee": f.handling_fee, "insurance_rate": f.insurance_rate, "service_fee_rate": f.service_fee_rate,
        "service_fee_min": f.service_fee_min, "collects_import_taxes": f.collects_import_taxes,
        "tax_handling_fee": f.tax_handling_fee, "notes": f.notes,
        "warehouses": [{"code": w.code, "country": w.country, "location": w.location, "sales_tax": w.sales_tax,
                        "transit": w.transit, "rate": {"currency": w.rate.currency, "first": w.rate.first,
                                                       "first_kg": w.rate.first_kg, "additional": w.rate.additional,
                                                       "step_kg": w.rate.step_kg}}
                       for w in f.warehouses],
    }


def account(t: Tracker, a: Account) -> dict:
    fwd = t.forwarders.get(a.forwarder)
    wh = fwd.warehouse(a.warehouse) if fwd else None
    rate, source = a.sales_tax_for(wh) if wh else (a.sales_tax, "")
    return {"forwarder": a.forwarder, "forwarder_name": fwd.name if fwd else a.forwarder, "warehouse": a.warehouse,
            "location": wh.location if wh else "", "address": a.address, "suite": a.suite,
            "sales_tax": a.sales_tax, "effective_sales_tax": rate, "sales_tax_source": source}


def forwarders(t: Tracker) -> dict:
    return {"services": [forwarder(f) for f in t.forwarders.values()],
            "accounts": [account(t, a) for a in t.accounts()]}


def events(today: date, days: int = 365) -> list[dict]:
    return [{"key": o.event.key, "name": o.event.name, "start": o.start.isoformat(), "end": o.end.isoformat(),
             "regions": list(o.event.regions), "estimated": o.event.date_certainty == "estimated",
             "active": o.is_active(today)} for o in upcoming_events(today, days)]


def landed(lc: LandedCost) -> dict:
    return {"item": _r(lc.item), "shipping": _r(lc.shipping), "duty": _r(lc.duty), "vat": _r(lc.vat),
            "fees": _r(lc.fees), "sales_tax": _r(lc.sales_tax), "total": _r(lc.total),
            "shipping_known": lc.shipping_known, "domestic": lc.domestic, "notes": lc.notes,
            "route": lc.route, "route_label": lc.route_label, "set_up": lc.set_up,
            "lines": [[label, _r(v)] for label, v in lc.lines]}


def quote(q: Quote) -> dict:
    return {
        "offer_id": q.offer.id, "store_key": q.store.key, "store": q.store.name, "country": q.store.country,
        "url": q.offer.url, "price": q.point.price, "currency": q.point.currency, "in_stock": q.point.in_stock,
        "seen": q.point.ts.isoformat(), "source": q.point.source,
        "landed": landed(q.landed),
        "routes": [landed(lc) for lc in q.routes],
    }


def _candidate(c: Candidate) -> dict:
    return {"label": c.label, "date": c.date.isoformat(), "days": c.days,
            "estimated": bool(c.event and c.event.date_certainty == "estimated"),
            "participation": _r(c.stats.participation, 3) if c.stats else None,
            "depth": _r(c.stats.depth, 3) if c.stats else None,
            "observed": c.stats.observed if c.stats else 0, "hits": c.stats.hits if c.stats else 0,
            "buy_below": _r(c.buy_below), "p_hit": _r(c.p_win, 3), "p_hit_by_then": _r(c.plan_p_win, 3),
            "net_saving": _r(c.net_saving), "expected": _r(c.expected), "p10": _r(c.p10), "p90": _r(c.p90)}


def advice(a: Advice) -> dict:
    return {
        "action": a.action, "confidence": _r(a.confidence, 3), "confidence_label": a.confidence_label,
        "current": _r(a.current), "regular": _r(a.regular), "expected": _r(a.expected),
        "low": _r(a.low), "high": _r(a.high), "wait": _candidate(a.wait) if a.wait else None,
        "candidates": [_candidate(c) for c in a.candidates],
        "active_events": [{"name": o.event.name, "start": o.start.isoformat(), "end": o.end.isoformat()}
                          for o in a.active_events],
        "reasons": a.reasons, "data_quality": _r(a.data_quality, 3),
    }


def item_detail(t: Tracker, item: Item, today: date | None = None, history_days: int = 400) -> dict:
    today = today or date.today()
    adv, quotes = t.advise(item, today)
    series = [(d, p) for d, p in t.daily_series(item, today) if d >= today - timedelta(days=history_days)]
    offers = []
    quoted = {q.offer.id for q in quotes}
    for o in t.db.offers(item):
        s = t.store_for(o)
        offers.append({"id": o.id, "store_key": s.key, "store": s.name, "url": o.url, "shipping": o.shipping,
                       "shipping_currency": o.shipping_currency, "local_shipping": o.local_shipping,
                       "has_regex": bool(o.price_regex), "has_prices": o.id in quoted})
    keys = {e for o in t.db.offers(item) for e in t.store_for(o).events}
    windows = []
    if series:
        for key in sorted(keys):
            for s, e in EVENTS[key].occurrences(series[0][0], today):
                windows.append({"name": EVENTS[key].name, "start": s.isoformat(), "end": e.isoformat()})
    return {
        "id": item.id, "name": item.name, "category": item.category, "target_price": item.target_price,
        "weight_kg": item.weight_kg, "dims": item.dims, "created_at": item.created_at,
        "best": quote(quotes[0]) if quotes else None,
        "quotes": [quote(q) for q in quotes],
        "offers": offers,
        "advice": advice(adv),
        "history": [[d.isoformat(), _r(p)] for d, p in series],
        "event_windows": sorted(windows, key=lambda w: w["start"]),
    }


def snapshot(t: Tracker, today: date | None = None) -> dict:
    """Everything the web client needs to work offline, in one response."""
    today = today or date.today()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "meta": meta(t),
        "items": [item_detail(t, i, today) for i in t.db.list_items()],
        "events": events(today),
        "stores": stores(),
        "forwarders": forwarders(t),
    }


def check_results(results: list[CheckResult]) -> list[dict]:
    out = []
    for r in results:
        d = {"offer_id": r.offer.id, "store": r.store.name, "ok": r.error is None, "error": r.error}
        if r.extraction:
            ex = r.extraction
            d.update(price=ex.price, currency=ex.currency, in_stock=ex.in_stock, method=ex.method,
                     shipping=ex.shipping, listing=ex.url, title=ex.title)
        out.append(d)
    return out
