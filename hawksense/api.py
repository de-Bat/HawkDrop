"""JSON views of tracker data, shared by the web server (and handy for scripts)."""

from __future__ import annotations

import difflib
import re

from datetime import date, datetime, timedelta, timezone

from hawksense import __version__
from hawksense.calendar_events import EVENTS, upcoming_events
from hawksense.db import Item
from hawksense.fetch import Extraction
from hawksense.forecast import Advice, Candidate
from hawksense.forwarders import Account, Forwarder
from hawksense.landed import LandedCost
from hawksense.stores import STORES
from hawksense.tracker import CheckResult, Quote, Tracker

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


def matches_query(query: str, title: str | None) -> bool:
    """Does a listing title cover every word of the query? Model numbers match across word breaks and order
    ("nc670" finds "Live 670NC"), so accessories and neighbouring models drop out."""
    text = re.sub(r"[^a-z0-9]+", "", (title or "").lower())
    for tok in re.findall(r"[a-z0-9]+", query.lower()):
        parts = re.findall(r"[a-z]+|\d+", tok)  # "nc670" -> nc, 670
        if not all(p in text for p in parts):
            return False
    return True


_CUT_WORDS = {"wireless", "bluetooth", "over-ear", "on-ear", "in-ear", "headphones", "headphone", "earbuds", "noise",
              "true", "with", "for", "cancelling", "canceling", "black", "white", "blue", "silver", "gray", "grey"}


def _short_name(title: str) -> str:
    head = re.split(r"\s[-–|]\s|,|\||\(", title)[0].split()
    out = []
    for i, w in enumerate(head):
        if i >= 2 and w.lower() in _CUT_WORDS:
            break
        out.append(w)
    return " ".join(out[:6])


def _token_score(tok: str, text: str, words: list[str]) -> float:
    """How well one query word is found in a title: 1 for an exact hit (model numbers match across word breaks),
    a discounted similarity for a typo ("line" ~ "live"), else 0. Words with digits count double: the model
    number is what tells "670NC" from "770NC"."""
    parts = re.findall(r"[a-z]+|\d+", tok)
    weight = 2.0 if re.search(r"\d", tok) else 1.0
    digits = [p for p in parts if p.isdigit()]
    if all(p in text for p in digits) and all(p in text for p in parts if len(p) > 1 or p.isdigit()) and parts:
        return weight
    best = max((difflib.SequenceMatcher(None, tok, w).ratio() for w in words), default=0.0)
    return weight * 0.9 * best if best >= 0.7 and not digits else 0.0


def suggest_phrases(query: str, titles: list[str | None], limit: int = 5) -> list[str]:
    """"Did you mean" phrases: the product names of near-miss listings (they share most of the query but not
    all of it, e.g. a neighbouring model, or the model you mistyped), best match first."""
    toks = re.findall(r"[a-z0-9]+", query.lower())
    if not toks:
        return []
    top = sum(2.0 if re.search(r"\d", t) else 1.0 for t in toks)
    found: dict[str, list] = {}
    for title in titles:
        if not title or matches_query(query, title):
            continue
        low = title.lower()
        text, words = re.sub(r"[^a-z0-9]+", "", low), re.findall(r"[a-z0-9]+", low)
        score = sum(_token_score(t, text, words) for t in toks)
        name = _short_name(title)
        if score >= top / 2 and name and name.lower() != query.lower():
            entry = found.setdefault(re.sub(r"[^a-z0-9]+", "", name.lower()), [name, 0, score])
            entry[1] += 1
            entry[2] = max(entry[2], score)
    ranked = sorted(found.values(), key=lambda e: (-e[2], -e[1], len(e[0])))
    return [e[0] for e in ranked[:limit]]


def _site_label(site: str) -> str:
    brand, _, tld = site.partition(".")
    name = "eBay" if brand == "ebay" else brand.capitalize()
    return name if tld == "com" else f"{name} ({tld})"


def search_candidate(ex: Extraction, site: str = "ebay.com", label: str | None = None) -> dict:
    return {"title": ex.title, "price": _r(ex.price), "currency": ex.currency, "url": ex.url,
            "image": ex.image, "shipping": _r(ex.shipping), "in_stock": ex.in_stock,
            "condition": ex.condition, "store": label or _site_label(site)}


def forwarder(f: Forwarder) -> dict:
    return {
        "key": f.key, "name": f.name, "currency": f.currency, "needs_address": f.needs_address,
        "handling_fee": f.handling_fee, "insurance_rate": f.insurance_rate, "service_fee_rate": f.service_fee_rate,
        "service_fee_min": f.service_fee_min, "collects_import_taxes": f.collects_import_taxes,
        "tax_handling_fee": f.tax_handling_fee, "notes": f.notes, "verified": list(f.verified),
        "warehouses": [{"code": w.code, "country": w.country, "location": w.location, "sales_tax": w.sales_tax,
                        "transit": w.transit, "rate": {"currency": w.rate.currency, "first": w.rate.first,
                                                       "first_kg": w.rate.first_kg, "additional": w.rate.additional,
                                                       "step_kg": w.rate.step_kg, "min_price": w.rate.min_price}}
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
            "route": lc.route, "route_label": lc.route_label, "set_up": lc.set_up, "hold": lc.hold,
            "lines": [[label, _r(v)] for label, v in lc.lines]}


def quote(q: Quote, all_routes: list[LandedCost] | None = None) -> dict:
    return {
        "offer_id": q.offer.id, "store_key": q.store.key, "store": q.store.name, "country": q.store.country,
        "url": q.point.listing_url or q.offer.url, "price": q.point.price, "currency": q.point.currency, "in_stock": q.point.in_stock,
        "seen": q.point.ts.isoformat(), "source": q.point.source, "title": q.offer.title,
        "condition": q.point.condition, "availability": q.point.availability,
        "landed": landed(q.landed),
        # every capable forwarder, including ones you haven't set up yet (marked "set_up": false)
        "routes": [landed(lc) for lc in (all_routes if all_routes is not None else q.routes)],
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


def specs(t: Tracker, item: Item) -> dict:
    found, rows = t.specs(item)
    return {"status": found.status if rows else None, "messages": found.messages if rows else [],
            "alert": bool(rows) and found.alert and item.weight_source != "manual",
            "confirm": found.to_confirm(item.weight_source == "manual", item.dims_source == "manual")
            if rows and not found.alert else [],
            "weight_kg": found.weight_kg, "dims": found.dims_text,
            "weight_source": item.weight_source, "dims_source": item.dims_source,
            "observations": [{"source": r["source"], "url": r["url"], "weight_kg": r["weight_kg"], "dims": r["dims"],
                              "weight_kind": r["weight_kind"], "dims_kind": r["dims_kind"], "seen": r["ts"]}
                             for r in rows]}


def rules_summary(t: Tracker) -> dict:
    from hawksense import rules

    code = t.dest.code
    values = [{"path": p, "value": v, "from": layer}
              for p, (v, layer) in sorted(rules.explain(t.db, t.config, code).items())
              if p.startswith((f"destination.{code}.", "forwarders."))]
    return {"destination": code, "values": values, "last_check": rules.last_check(t.db),
            "pending": t.db.rule_changes("pending"), "history": t.db.rule_changes(limit=25)}


def settings_view(t: Tracker) -> dict:
    from hawksense import settings as app_settings
    from hawksense.config import Config

    return app_settings.view(t.db, getattr(t, "file_config", None) or Config(), startup=getattr(t, "startup", None))


def notifications(t: Tracker) -> dict:
    from hawksense.notify import EVENTS

    n = t.notifier
    return {"items": t.db.notifications(50), "unread": len(t.db.notifications(500, unread_only=True)),
            "events": [{"key": e.key, "title": e.title, "description": e.description} for e in EVENTS.values()],
            "channels": [{"key": k, "name": c.name, "configured": c.configured} for k, c in n.channels.items()],
            "subscriptions": n.subscriptions(), "settings": n.settings()}


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
                       "has_regex": bool(o.price_regex), "has_prices": o.id in quoted, "title": o.title})
    keys = {e for o in t.db.offers(item) for e in t.store_for(o).events}
    windows = []
    if series:
        for key in sorted(keys):
            for s, e in EVENTS[key].occurrences(series[0][0], today):
                windows.append({"name": EVENTS[key].name, "start": s.isoformat(), "end": e.isoformat()})
    # every capable forwarder for each quote, not just the ones you've already set up
    quotes_json = [quote(q, t.landed_options(item, q.offer, q.point, q.store, explore=True)) for q in quotes]
    return {
        "id": item.id, "name": item.name, "category": item.category, "target_price": item.target_price,
        "weight_kg": item.weight_kg, "dims": item.dims, "created_at": item.created_at, "muted": item.muted,
        "image_url": item.image_url, "description": item.description,
        "specs": specs(t, item),
        "best": quotes_json[0] if quotes_json else None,
        "quotes": quotes_json,
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
        "rules": rules_summary(t),
        "notifications": notifications(t),
        "settings": settings_view(t),
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
