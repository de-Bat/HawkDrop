"""Command-line interface: ``hawkdrop <command>`` (or ``python -m hawkdrop``)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

from hawkdrop import __version__
from hawkdrop.calendar_events import upcoming_events
from hawkdrop.config import home, load_config
from hawkdrop.currency import FX
from hawkdrop.db import Database, Item
from hawkdrop.demo import seed_demo
from hawkdrop.forecast import Advice
from hawkdrop.landed import destination_from_config
from hawkdrop.stores import STORES
from hawkdrop.tracker import Quote, Tracker

SPARK = "▁▂▃▄▅▆▇█"


# ---- output helpers --------------------------------------------------------------

def table(rows: list[list], headers: list[str]) -> str:
    cells = [[str(c) for c in r] for r in [headers, *rows]]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def money(v: float, cur: str = "") -> str:
    decimals = 2 if 0 < abs(v) < 100 and v != int(v) else 0
    return f"{v:,.{decimals}f} {cur}".strip()


def sparkline(values: list[float], width: int = 60) -> str:
    if not values:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [min(values[int(i * step):int((i + 1) * step)] or [values[-1]]) for i in range(width)]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    return "".join(SPARK[int((v - lo) / span * (len(SPARK) - 1))] for v in values)


# ---- command implementations -------------------------------------------------------

def _item(tracker: Tracker, ref: str) -> Item:
    item = tracker.db.get_item(ref)
    if not item:
        raise SystemExit(f"error: no tracked item matches {ref!r} (see `hawkdrop list`)")
    return item


def _print_check(tracker: Tracker, item: Item):
    for r in tracker.check(item):
        if r.error:
            print(f"  ✗ {r.store.name:<22} {r.error}")
        else:
            ex = r.extraction
            stock = "" if ex.in_stock else " (out of stock)"
            print(f"  ✓ {r.store.name:<22} {money(ex.price, ex.currency)}{stock}  [{ex.method}]")


def cmd_track(t: Tracker, a):
    item = t.db.get_item(a.name)
    if item:
        t.db.update_item(item, a.category, a.target)
    else:
        item = t.db.add_item(a.name, a.category or "default", a.target)
        print(f"Tracking #{item.id} {item.name} [{item.category}]")
    for url in a.url or []:
        offer = t.add_offer(item, url)
        print(f"  + {t.store_for(offer).name}: {url}")
    if a.url and not a.no_check:
        _print_check(t, item)


def cmd_add_offer(t: Tracker, a):
    item = _item(t, a.item)
    offer = t.add_offer(item, a.url, a.shipping, a.shipping_currency, a.regex)
    store = t.store_for(offer)
    print(f"Added {store.name} ({store.country}, {store.currency}) to {item.name}")
    if store.notes:
        print(f"  note: {store.notes}")


def cmd_price(t: Tracker, a):
    item = _item(t, a.item)
    when = date.fromisoformat(a.date) if a.date else None
    offer = t.record_price(item, a.store, a.price, a.currency, a.shipping, not a.out_of_stock, when)
    store = t.store_for(offer)
    print(f"Recorded {money(a.price, (a.currency or store.currency).upper())} at {store.name} for {item.name}")


def cmd_check(t: Tracker, a):
    items = [_item(t, a.item)] if a.item else t.db.list_items()
    for item in items:
        print(f"{item.name}")
        _print_check(t, item)
        if item.target_price is not None:
            quotes = t.quotes(item)
            if quotes and quotes[0].landed.total <= item.target_price:
                print(f"  ★ TARGET HIT: {quotes[0].store.name} "
                      f"{money(quotes[0].landed.total, t.dest.currency)} <= {money(item.target_price)}")


def cmd_list(t: Tracker, a):
    rows = []
    for item in t.db.list_items():
        quotes = t.quotes(item)
        best = quotes[0] if quotes else None
        rows.append([item.id, item.name, item.category, len(t.db.offers(item)),
                     money(best.landed.total, t.dest.currency) if best else "-",
                     best.store.name if best else "-",
                     money(item.target_price) if item.target_price else "-"])
    if not rows:
        print("Nothing tracked yet. Try `hawkdrop track \"Item name\" --url <product url>` or `hawkdrop demo`.")
        return
    print(table(rows, ["#", "item", "category", "stores", "best landed", "at", "target"]))


def _quote_rows(t: Tracker, quotes: list[Quote]) -> list[list]:
    cur = t.dest.currency
    rows = []
    for q in quotes:
        lc = q.landed
        rows.append([
            q.store.name, f"{q.store.country}", money(q.point.price, q.point.currency),
            money(lc.item, cur), money(lc.shipping, cur) + ("" if lc.shipping_known else "?"),
            money(lc.duty + lc.vat, cur), money(lc.fees, cur), money(lc.total, cur),
            "yes" if q.point.in_stock else "NO", q.point.ts.date().isoformat(),
        ])
    return rows


def cmd_compare(t: Tracker, a):
    item = _item(t, a.item)
    quotes = t.quotes(item)
    if not quotes:
        raise SystemExit("No prices yet - run `hawkdrop check` or `hawkdrop price`.")
    print(f"{item.name} - landed cost delivered to {t.dest.name} ({t.dest.currency}, FX: {t.fx.source})\n")
    print(table(_quote_rows(t, quotes),
                ["store", "from", "price", "item", "shipping", "duty+VAT", "fees", "TOTAL", "stock", "seen"]))
    print()
    for q in quotes:
        if q.landed.notes:
            print(f"  {q.store.name}: " + "; ".join(q.landed.notes))


def cmd_history(t: Tracker, a):
    item = _item(t, a.item)
    series = t.daily_series(item)
    series = [(d, p) for d, p in series if d >= date.today() - timedelta(days=a.days)]
    if not series:
        raise SystemExit("No history yet.")
    prices = [p for _, p in series]
    cur = t.dest.currency
    print(f"{item.name} - best landed price, last {a.days} days ({cur})")
    print(f"  {sparkline(prices)}")
    print(f"  {series[0][0]} … {series[-1][0]}")
    lo = min(series, key=lambda x: x[1])
    print(f"  now {money(prices[-1], cur)}  |  low {money(lo[1], cur)} ({lo[0]})  |  high {money(max(prices), cur)}"
          f"  |  avg {money(sum(prices) / len(prices), cur)}")


def _advice_json(item: Item, adv: Advice, quotes: list[Quote], cur: str) -> dict:
    return {
        "item": item.name, "action": adv.action, "confidence": round(adv.confidence, 3),
        "confidence_label": adv.confidence_label, "currency": cur, "current": round(adv.current, 2),
        "expected": round(adv.expected, 2), "range": [round(adv.low, 2), round(adv.high, 2)],
        "best_store": quotes[0].store.name if quotes else None,
        "wait_for": adv.wait.label if adv.wait else None,
        "wait_until": adv.wait.date.isoformat() if adv.wait else None,
        "reasons": adv.reasons,
        "events": [{"event": c.label, "date": c.date.isoformat(), "days": c.days,
                    "buy_below": round(c.buy_below, 2), "p_hit": round(c.p_win, 3),
                    "p_hit_by_then": round(c.plan_p_win, 3), "net_saving": round(c.net_saving, 2),
                    "expected_price": round(c.expected, 2), "p10": round(c.p10, 2), "p90": round(c.p90, 2)}
                   for c in adv.candidates],
    }


def cmd_advise(t: Tracker, a):
    items = [_item(t, a.item)] if a.item else t.db.list_items()
    cur = t.dest.currency
    out = []
    for item in items:
        adv, quotes = t.advise(item)
        if a.json:
            out.append(_advice_json(item, adv, quotes, cur))
            continue
        print("=" * 72)
        print(f"{item.name}")
        print("=" * 72)
        if adv.action == "NO_DATA":
            print("  " + adv.reasons[0])
            continue
        best = quotes[0]
        verdict = "BUY NOW" if adv.action == "BUY_NOW" else f"WAIT until {adv.wait.date:%b %d} ({adv.wait.label})"
        print(f"  Recommendation : {verdict}")
        print(f"  Confidence     : {adv.confidence:.0%} ({adv.confidence_label})")
        print(f"  Best offer now : {best.store.name} - {money(best.landed.total, cur)} delivered")
        if adv.action == "WAIT":
            print(f"  Expected price : ~{money(adv.expected, cur)}  "
                  f"(80% range {money(adv.low, cur)} - {money(adv.high, cur)})")
        print("\n  Why:")
        for r in adv.reasons:
            print(f"   • {r}")
        if adv.candidates:
            print("\n  Upcoming sales days:")
            rows = [[c.label, c.date.isoformat(), c.days,
                     f"{c.stats.participation:.0%}/{c.stats.depth:.0%}" if c.stats else "-",
                     money(c.buy_below, cur), f"{c.p_win:.0%}", f"{c.plan_p_win:.0%}", money(c.net_saving, cur)]
                    for c in adv.candidates]
            print("  " + table(rows, ["event", "date", "days", "p(sale)/depth", "buy below", "p(hit)",
                                      "p(hit) by then", "net saving"]).replace("\n", "\n  "))
        print()
    if a.json:
        print(json.dumps(out if not a.item else out[0], indent=2, ensure_ascii=False))


def cmd_events(t: Tracker, a):
    regions = {r.upper() for r in a.region} if a.region else None
    rows = [[o.event.name, o.start.isoformat(), o.end.isoformat(), max(0, o.days_until(date.today())),
             ", ".join(o.event.regions), "~" if o.event.date_certainty == "estimated" else ""]
            for o in upcoming_events(date.today(), a.days, regions)]
    print(table(rows, ["event", "from", "to", "in days", "regions", "est."]))
    print("\n~ = date estimated from previous years")


def cmd_stores(t: Tracker, a):
    rows = []
    for s in STORES.values():
        ship = "?" if s.shipping_flat is None else money(s.shipping_flat, s.currency)
        if s.shipping_free_over is not None:
            ship += f" (free over {money(s.shipping_free_over, s.currency)})"
        rows.append([s.key, s.name, s.country, s.currency, ship, "yes" if s.collects_import_vat else ""])
    print(table(rows, ["key", "name", "ships from", "currency", "shipping", "collects VAT"]))
    print("\nAny other store works too - HawkDrop guesses country/currency from the URL.")


def cmd_remove(t: Tracker, a):
    item = _item(t, a.item)
    t.db.delete_item(item)
    print(f"Removed {item.name}")


def cmd_serve(t: Tracker, a):
    from hawkdrop.server import ServerContext, serve

    ctx = ServerContext(t.db.path, load_config(), a.dest, bool(a.offline), a.token)
    serve(ctx, a.host, a.port, a.cert, a.key, a.check_every)


def cmd_demo(t: Tracker, a):
    item = seed_demo(t.db)
    print(f"Seeded demo item #{item.id}: {item.name}\n")
    cmd_compare(t, argparse.Namespace(item=str(item.id)))
    print()
    cmd_advise(t, argparse.Namespace(item=str(item.id), json=False))


# ---- argument parsing --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hawkdrop", description="Smart price tracker with landed cost and "
                                "sales-day aware buying advice.")
    p.add_argument("--version", action="version", version=f"hawkdrop {__version__}")
    p.add_argument("--db", help="database path (default: $HAWKDROP_HOME/hawkdrop.db)")
    p.add_argument("--dest", help="destination country: IL (default), US, EU, UK")
    p.add_argument("--offline", action="store_true", help="don't fetch live exchange rates")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("track", help="start tracking an item (optionally with product URLs)")
    s.add_argument("name")
    s.add_argument("--url", action="append", help="product page URL (repeatable)")
    s.add_argument("--category", help="electronics, computers, phones, appliances, clothing, shoes, toys ...")
    s.add_argument("--target", type=float, help="alert when landed price drops to this (destination currency)")
    s.add_argument("--no-check", action="store_true", help="don't fetch prices right away")
    s.set_defaults(func=cmd_track)

    s = sub.add_parser("add-offer", help="add a store/URL to an item")
    s.add_argument("item")
    s.add_argument("url", help="product URL, or a store key for manual-only tracking (see `stores`)")
    s.add_argument("--shipping", type=float, help="shipping cost to you (overrides store policy)")
    s.add_argument("--shipping-currency", help="currency of --shipping (default: price currency)")
    s.add_argument("--regex", help="custom regex whose first group is the price")
    s.set_defaults(func=cmd_add_offer)

    s = sub.add_parser("price", help="record a price manually")
    s.add_argument("item")
    s.add_argument("store", help="store key or product URL")
    s.add_argument("price", type=float)
    s.add_argument("--currency")
    s.add_argument("--shipping", type=float)
    s.add_argument("--out-of-stock", action="store_true")
    s.add_argument("--date", help="YYYY-MM-DD (default: now)")
    s.set_defaults(func=cmd_price)

    s = sub.add_parser("check", help="fetch current prices (all items, or one) - run it from cron")
    s.add_argument("item", nargs="?")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("list", help="list tracked items")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("compare", help="landed-cost comparison across stores")
    s.add_argument("item")
    s.set_defaults(func=cmd_compare)

    s = sub.add_parser("history", help="price history sparkline")
    s.add_argument("item")
    s.add_argument("--days", type=int, default=180)
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("advise", help="buy now or wait? with confidence")
    s.add_argument("item", nargs="?")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_advise)

    s = sub.add_parser("events", help="upcoming sales days")
    s.add_argument("--days", type=int, default=120)
    s.add_argument("--region", action="append", help="IL, US, CN, AMAZON, EU, UK (repeatable)")
    s.set_defaults(func=cmd_events)

    s = sub.add_parser("stores", help="built-in store profiles")
    s.set_defaults(func=cmd_stores)

    s = sub.add_parser("remove", help="stop tracking an item")
    s.add_argument("item")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("serve", help="run the web app / PWA (open it on your phone)")
    s.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to reach it from other devices")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--token", help="require this access token (recommended with --host 0.0.0.0)")
    s.add_argument("--cert", help="TLS certificate (PEM) - needed for offline mode on iOS")
    s.add_argument("--key", help="TLS private key (PEM)")
    s.add_argument("--check-every", type=float, metavar="HOURS", help="fetch all prices every N hours")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("demo", help="load a demo item with 14 months of synthetic history")
    s.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    dest_cfg = dict(cfg.destination)
    if args.dest:
        dest_cfg = {"code": args.dest}
    db = Database(args.db or home() / "hawkdrop.db")
    try:
        tracker = Tracker(db, FX(db, offline=args.offline or None), destination_from_config(dest_cfg),
                          Tracker.settings_from(cfg.advisor), cfg.stores)
        args.func(tracker, args)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
