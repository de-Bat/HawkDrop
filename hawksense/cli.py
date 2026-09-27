"""Command-line interface: ``hawksense <command>`` (or ``python -m hawksense``)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from hawksense import __version__
from hawksense import settings as app_settings
from hawksense.calendar_events import upcoming_events
from hawksense.config import db_path, load_config, server_settings
from hawksense.currency import FX
from hawksense.db import Database, Item
from hawksense.demo import seed_demo
from hawksense.ebay import SITES as EBAY_SITES, search_url
from hawksense.forecast import Advice
from hawksense.forwarders import Forwarder, parse_dims, state_from_address
from hawksense.landed import LandedCost
from hawksense.stores import STORES
from hawksense.tracker import Quote, Tracker

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
        raise SystemExit(f"error: no tracked item matches {ref!r} (see `hawksense list`)")
    return item


def _print_specs(tracker: Tracker, item: Item, only_problems: bool = False):
    found, rows = tracker.specs(item)
    item = tracker.db.get_item(item.id)
    if not rows:
        return
    manual = item.weight_source == "manual"
    size = f"{item.weight_kg:g} kg" if item.weight_kg else "weight unknown"
    if item.dims:
        size += f", {item.dims} cm"
    sources = sum(1 for r in rows if r["weight_kg"] or r["dims"])
    if manual:
        if not only_problems:
            print(f"  size: {size} (set by you)")
        return
    label = {"verified": f"verified by {sources} store pages", "unverified": "from one store page, not verified",
             "conflict": "store pages disagree", "missing": "not found on any store page"}[found.status]
    if found.alert:
        print(f"  ⚠ size: {size} - {label}")
        for m in found.messages:
            print(f"      {m}")
        print(f"      set it yourself: hawksense track {item.name!r} --weight KG --dims LxWxH")
    elif not only_problems:
        print(f"  size: {size} ({label})")


def _print_check(tracker: Tracker, item: Item, results=None):
    for r in results if results is not None else tracker.check(item):
        if r.error:
            print(f"  ✗ {r.store.name:<22} {r.error}")
        else:
            ex = r.extraction
            stock = "" if ex.in_stock else " (out of stock)"
            ship = f" + {money(ex.shipping, ex.currency)} shipping" if ex.shipping else ""
            print(f"  ✓ {r.store.name:<22} {money(ex.price, ex.currency)}{ship}{stock}  [{ex.method}]")
            if ex.url and ex.url != r.offer.url:
                print(f"    {ex.title or 'listing'}: {ex.url}")


def _dims_arg(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        dims = parse_dims(value)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None
    return "x".join(f"{d:g}" for d in dims)


def cmd_track(t: Tracker, a):
    item = t.db.get_item(a.name)
    dims = _dims_arg(a.dims)
    if item:
        t.db.update_item(item, a.category, a.target, a.weight, dims)
    else:
        item = t.db.add_item(a.name, a.category or "default", a.target)
        t.db.update_item(item, weight_kg=a.weight, dims=dims)
        print(f"Tracking #{item.id} {item.name} [{item.category}]")
    for url in a.url or []:
        offer = t.add_offer(item, url)
        print(f"  + {t.store_for(offer).name}: {url}")
    if a.url and not a.no_check:
        _print_check(t, item)
        _print_specs(t, item)


def cmd_add_offer(t: Tracker, a):
    item = _item(t, a.item)
    url = a.url
    if a.ebay_search:
        if url:
            raise SystemExit("error: give either a URL or --ebay-search, not both")
        url = search_url(a.ebay_search, a.ebay_site, None if a.condition == "any" else a.condition)
    elif not url:
        raise SystemExit("error: give a product URL, a store key, or --ebay-search QUERY")
    offer = t.add_offer(item, url, a.shipping, a.shipping_currency, a.regex, a.local_shipping)
    store = t.store_for(offer)
    print(f"Added {store.name} ({store.country}, {store.currency}) to {item.name}")
    if a.ebay_search:
        cond = "" if a.condition == "any" else f"{a.condition} "
        print(f"  tracking the cheapest {cond}buy-it-now listing for {a.ebay_search!r}")
        if not t.ebay.api:
            print("  tip: add eBay API keys for reliable prices and shipping to your country (see README)")
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
    before = {n["id"] for n in t.db.notifications(200)}
    results = t.check_and_notify(items)
    for item in items:
        print(f"{item.name}")
        _print_check(t, item, results[item.id])
        _print_specs(t, item)
        if item.target_price is not None:
            quotes = t.quotes(item)
            if quotes and quotes[0].landed.total <= item.target_price:
                print(f"  ★ TARGET HIT: {quotes[0].store.name} "
                      f"{money(quotes[0].landed.total, t.dest.currency)} <= {money(item.target_price)}")
    new = [n for n in t.db.notifications(200) if n["id"] not in before]
    for n in reversed(new):
        sent = [c for c, status in n["deliveries"].items() if status == "sent"]
        print(f"🔔 {n['title']}" + (f"  (sent: {', '.join(sent)})" if sent else ""))


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
        print("Nothing tracked yet. Try `hawksense track \"Item name\" --url <product url>` or `hawksense demo`.")
        return
    print(table(rows, ["#", "item", "category", "stores", "best landed", "at", "target"]))


def _via(t: Tracker, lc: LandedCost) -> str:
    if lc.route == "direct":
        return "direct"
    key, _, code = lc.route.partition(":")
    fwd = t.forwarders.get(key)
    return f"{fwd.name if fwd else key} {code}"


def _quote_rows(t: Tracker, quotes: list[Quote]) -> list[list]:
    cur = t.dest.currency
    rows = []
    for q in quotes:
        lc = q.landed
        rows.append([
            q.store.name, f"{q.store.country}", _via(t, lc), money(q.point.price, q.point.currency),
            money(lc.item, cur), money(lc.shipping, cur) + ("" if lc.shipping_known else "?"),
            money(lc.duty + lc.vat + lc.sales_tax, cur), money(lc.fees, cur), money(lc.total, cur),
            "yes" if q.point.in_stock else "NO", q.point.ts.date().isoformat(),
        ])
    return rows


def _route_detail(t: Tracker, lc: LandedCost, best: bool) -> list[str]:
    cur = t.dest.currency
    mark = "★" if best else ("·" if lc.set_up else "?")
    head = f"    {mark} {_via(t, lc):<22} {money(lc.total, cur):>12}"
    if not lc.set_up:
        head += "   (not set up: hawksense forwarder add " + lc.route.split(":")[0] + ")"
    out = [head]
    parts = [("item", lc.item)]
    parts += lc.lines if lc.lines else [("shipping", lc.shipping)]
    parts += [("sales tax", lc.sales_tax), ("customs duty", lc.duty), ("import VAT", lc.vat)]
    if not lc.lines:
        parts.append(("fees", lc.fees))
    out.append("        " + " + ".join(f"{label} {money(v, cur)}" for label, v in parts if v))
    if lc.notes:
        out.append("        " + "; ".join(lc.notes))
    return out


def cmd_compare(t: Tracker, a):
    item = _item(t, a.item)
    quotes = t.quotes(item, explore=a.explore)
    if not quotes:
        raise SystemExit("No prices yet - run `hawksense check` or `hawksense price`.")
    print(f"{item.name} - landed cost delivered to {t.dest.name} ({t.dest.currency}, FX: {t.fx.source})\n")
    print(table(_quote_rows(t, quotes),
                ["store", "from", "via", "price", "item", "shipping", "taxes", "fees", "TOTAL", "stock", "seen"]))
    print()
    for q in quotes:
        if a.routes or a.explore:
            print(f"  {q.store.name}:")
            for i, lc in enumerate(q.routes):
                print("\n".join(_route_detail(t, lc, i == 0)))
        elif q.landed.notes:
            print(f"  {q.store.name} ({_via(t, q.landed)}): " + "; ".join(q.landed.notes))
    best = quotes[0].landed
    if best.route != "direct":
        key, _, code = best.route.partition(":")
        acc = next((x for x in t.accounts() if x.forwarder == key and x.warehouse == code), None)
        if acc and acc.address:
            print(f"\n  Ship the {quotes[0].store.name} order to your {_via(t, best)} address:\n    {acc.address}"
                  + (f"  (suite {acc.suite})" if acc.suite else ""))
    if not t.accounts() and any(t.forwarder_routes(q.store, explore=True) for q in quotes):
        print("\n  tip: stores abroad may be cheaper through a package forwarder - see `hawksense compare "
              f"{a.item!r} --explore` and `hawksense forwarders`")


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
    print("\nAny other store works too - HawkSense guesses country/currency from the URL.")


def _rate_text(fwd: Forwarder) -> str:
    return "; ".join(f"{w.code}: {money(w.rate.first, w.rate.currency)} first {w.rate.first_kg:g} kg "
                     f"+ {money(w.rate.additional, w.rate.currency)}/{w.rate.step_kg:g} kg" for w in fwd.warehouses)


def cmd_forwarders(t: Tracker, a):
    accounts = t.accounts()
    mine = {(x.forwarder, x.warehouse) for x in accounts}
    rows = []
    for f in t.forwarders.values():
        whs = ", ".join(f"{w.code}{'✓' if (f.key, w.code) in mine else ''} ({w.location}"
                        + (f", sales tax {w.sales_tax:.0%}" if w.sales_tax else "") + ")" for w in f.warehouses)
        fees = []
        if f.handling_fee:
            fees.append(f"handling {money(f.handling_fee, f.currency)}")
        if f.service_fee_rate:
            fees.append(f"service {f.service_fee_rate:.0%} (min {money(f.service_fee_min, f.currency)})")
        if f.insurance_rate:
            fees.append(f"insurance {f.insurance_rate:.1%}")
        taxes = "paid via service" if f.collects_import_taxes else "courier + clearance fee"
        if f.collects_import_taxes and f.tax_handling_fee:
            taxes += f" ({money(f.tax_handling_fee, f.currency)})"
        rows.append([f.key, f.name, whs, ", ".join(fees) or "-", taxes])
    print(table(rows, ["key", "name", "warehouses (✓ = yours)", "fees", "import taxes"]))
    print("\nShipping rates (estimates - check each service's price list, override in config.toml):")
    for f in t.forwarders.values():
        print(f"  {f.name:<22} {_rate_text(f)}")
    if accounts:
        print("\nYour addresses:")
        for x in accounts:
            fwd = t.forwarders.get(x.forwarder)
            wh = fwd.warehouse(x.warehouse) if fwd else None
            tax = f"sales tax {x.sales_tax_for(wh)[0]:.1%} ({x.sales_tax_for(wh)[1]})" if wh else "unknown service"
            print(f"  {fwd.name if fwd else x.forwarder} {x.warehouse}: {x.address or '(no address needed)'}"
                  + (f" [suite {x.suite}]" if x.suite else "") + f" - {tax}")
    else:
        print("\nSet one up with `hawksense forwarder add <key>` - you'll be asked for the address(es) it gave you.")


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def cmd_forwarder_add(t: Tracker, a):
    fwd = t.forwarders.get(a.key)
    if fwd is None:
        raise SystemExit(f"error: unknown forwarder {a.key!r}. Known: {', '.join(t.forwarders)}")
    if a.warehouse:
        wanted = [fwd.warehouse(a.warehouse)]
        if wanted[0] is None:
            raise SystemExit(f"error: {fwd.name} warehouses: {', '.join(w.code for w in fwd.warehouses)}")
    else:
        wanted = list(fwd.warehouses)
    interactive = sys.stdin.isatty() and a.address is None
    if fwd.needs_address and a.address is None and not interactive:
        raise SystemExit(f"error: {fwd.name} needs the address it gave you - e.g.\n  hawksense forwarder add {fwd.key} "
                         f"--warehouse {wanted[0].code} --address \"Your Name, 123 Street #SUITE, City, ST 12345\"")
    if fwd.notes:
        print(fwd.notes)
    saved = 0
    for wh in wanted:
        address, suite = a.address or "", a.suite or ""
        if fwd.needs_address and interactive:
            address = _ask(f"Your {fwd.name} address in {wh.location} ({wh.code}), as shown in your account "
                           "- leave empty to skip:\n  > ")
            if not address:
                continue
            suite = _ask("  Suite / customer number (optional): ")
        try:
            acc = t.save_account(fwd.key, wh.code, address, suite, a.sales_tax)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
        saved += 1
        rate, source = acc.sales_tax_for(wh)
        detail = f"sales tax {rate:.1%} ({source})" if wh.country == "US" else wh.location
        state = state_from_address(address)
        if wh.country == "US" and address and not state and a.sales_tax is None:
            detail += " - no US state found in the address, using the warehouse default"
        print(f"  ✓ {fwd.name} {wh.code}: {detail}")
    if not saved:
        print("Nothing saved.")
    else:
        print("Prices from matching stores now include this route - see `hawksense compare <item> --routes`.")


def cmd_forwarder_remove(t: Tracker, a):
    n = t.remove_account(a.key, a.warehouse)
    print(f"Removed {n} address{'es' if n != 1 else ''}" if n else "No matching forwarder address.")


def cmd_specs(t: Tracker, a):
    item = _item(t, a.item)
    for url in a.source or []:
        try:
            t.fetch_specs(item, url)
        except Exception as exc:  # a bad extra page shouldn't hide the rest
            print(f"  ✗ {url}: {exc}")
    found, rows = t.specs(item)
    item = t.db.get_item(item.id)
    print(f"{item.name} - weight and box size\n")
    if rows:
        print(table([[r["source"], f"{r['weight_kg']:g} kg ({r['weight_kind']})" if r["weight_kg"] else "-",
                      f"{r['dims']} cm ({r['dims_kind']})" if r["dims"] else "-", r["ts"][:10]] for r in rows],
                    ["page", "weight", "size", "seen"]))
        print(f"\nConsensus: {found.status}"
              + (f" - {found.weight_kg:g} kg" if found.weight_kg else "")
              + (f", {found.dims_text} cm" if found.dims_cm else ""))
        for m in found.messages:
            print(f"  • {m}")
    else:
        print("No store pages checked yet - run `hawksense check` (or add --source URL).")
    who = {"manual": "set by you", "auto": "from store pages", None: "not set"}
    print(f"\nUsed for shipping: weight {item.weight_kg or '-'} kg ({who.get(item.weight_source)}), "
          f"size {item.dims or '-'} cm ({who.get(item.dims_source)})")


def cmd_rules_show(t: Tracker, a):
    from hawksense import rules

    rows = []
    for path, (value, layer) in sorted(rules.explain(t.db, t.config or load_config(), t.dest.code).items()):
        if not a.all and layer == "builtin":
            continue
        if a.filter and a.filter.lower() not in path.lower():
            continue
        rows.append([path, value, layer])
    if rows:
        print(table(rows, ["rule", "value", "from"]))
    else:
        print("All rules are the built-in values." + ("" if a.all else " (--all lists them)"))
    last = rules.last_check(t.db)
    if last:
        print(f"\nLast automatic check: {last['ts'][:16].replace('T', ' ')} UTC - {last['applied']} applied, "
              f"{last['pending']} to review" + (f", {len(last['errors'])} problems" if last["errors"] else ""))
    pending = t.db.rule_changes("pending")
    if pending:
        print(f"{len(pending)} change(s) waiting for review: `hawksense rules pending`")


def cmd_rules_check(t: Tracker, a):
    from hawksense import rules

    report = rules.check_updates(t.db, t.config or load_config())
    for src in report.sources:
        print(f"  checked {src}")
    for c in report.applied:
        print(f"  ✓ {c.path}: {c.old} → {c.new} ({c.source})")
    for c in report.pending:
        print(f"  ? #{c.id} {c.path}: {c.old} → {c.new} ({c.source}) - {c.note}")
    for e in report.errors:
        print(f"  ✗ {e}")
    if not (report.applied or report.pending or report.errors):
        print("  rules are up to date")
    t.notifier.rules_report(report)


def cmd_rules_set(t: Tracker, a):
    from hawksense import rules

    try:
        paths = rules.set_manual(t.db, {a.path: rules.parse_value(a.value)}, t.forwarders)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None
    print(f"Set {paths[0]} = {a.value} (your manual rules override fetched and config values)")


def cmd_rules_unset(t: Tracker, a):
    from hawksense import rules

    paths = rules.set_manual(t.db, {a.path: None}, t.forwarders)
    print(f"Removed your value for {paths[0]}")


def cmd_rules_pending(t: Tracker, a):
    pending = t.db.rule_changes("pending")
    if not pending:
        print("Nothing to review.")
        return
    print(table([[c["id"], c["path"], c["old"], c["new"], c["source"], c["note"]] for c in pending],
                ["#", "rule", "now", "fetched", "from", "why"]))
    print("\nhawksense rules accept <#>  /  hawksense rules reject <#>")


def cmd_rules_decide(t: Tracker, a):
    from hawksense import rules

    try:
        c = rules.decide(t.db, a.id, a.rules_command == "accept")
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None
    print(f"{'Accepted' if a.rules_command == 'accept' else 'Rejected'} #{c['id']}: {c['path']} = {c['new']}")


def cmd_rules_history(t: Tracker, a):
    rows = [[c["id"], c["ts"][:16].replace("T", " "), c["path"], c["old"], c["new"], c["source"], c["status"]]
            for c in t.db.rule_changes(limit=a.limit)]
    print(table(rows, ["#", "when (UTC)", "rule", "old", "new", "from", "status"]) if rows else "No rule changes yet.")


def cmd_notify_status(t: Tracker, a):
    from hawksense.notify import EVENTS

    n = t.notifier
    print("Channels:")
    for key, ch in n.channels.items():
        state = "always on" if key == "inbox" else ("configured" if ch.configured else
                                                     "not set up (app Settings, or [notify.{key}] in config.toml)")
        print(f"  {key:<9} {ch.name:<12} {state}")
    subs = n.subscriptions()
    print()
    print(table([[e.key, e.description, ", ".join(subs[e.key]) or "off"] for e in EVENTS.values()],
                ["event", "when", "sent to"]))
    print("\nSettings: " + ", ".join(f"{k} = {v:g}" for k, v in n.settings().items()))
    print("Change with: hawksense notify subscribe <event> <channel>...   (e.g. buy_now inbox telegram)")


def cmd_notify_subscribe(t: Tracker, a):
    from hawksense.notify import EVENTS

    events = list(EVENTS) if a.event == "all" else [a.event]
    try:
        for e in events:
            t.notifier.subscribe(e, a.channels)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None
    unconfigured = [c for c in a.channels if not t.notifier.channels[c].configured]
    print(f"{', '.join(events)} → {', '.join(a.channels) or 'off'}")
    if unconfigured:
        print(f"  note: {', '.join(unconfigured)} not configured yet - see [notify.*] in the README")


def cmd_notify_set(t: Tracker, a):
    try:
        t.notifier.update_settings({a.key: a.value})
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None
    print(f"{a.key} = {a.value:g}")


def cmd_notify_test(t: Tracker, a):
    from hawksense.notify import NotifyError

    try:
        print(f"{a.channel}: {t.notifier.test(a.channel)}")
    except (NotifyError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from None


def cmd_notify_inbox(t: Tracker, a):
    items = t.db.notifications(a.limit, unread_only=not a.all)
    if not items:
        print("No new notifications." + ("" if a.all else " (--all shows read ones)"))
    for n in items:
        print(f"{'•' if not n['read'] else ' '} {n['ts'][:16].replace('T', ' ')}  {n['title']}")
        if n["body"]:
            print("    " + n["body"].replace("\n", "\n    "))
    if not a.keep:
        t.db.mark_read([n["id"] for n in items])


def cmd_notify_run(t: Tracker, a):
    new = t.notifier.evaluate(t)
    print(f"{len(new)} new notification(s)")


def cmd_settings_show(t: Tracker, a):
    view = app_settings.view(t.db, load_config())
    for sec in view["sections"]:
        state = "" if sec["configured"] is None else ("  ✓ set up" if sec["configured"] else "  (not set up)")
        print(f"{sec['title']}{state}")
        for f in sec["fields"]:
            if f["kind"] == "secret":
                value = "••••••" if f["is_set"] else "-"
            elif f["kind"] == "percent" and f["value"] is not None:
                value = f"{f['value'] * 100:g}%"
            else:
                value = "-" if f["value"] in (None, "") else f["value"]
            src = {"app": "", "default": " (default)", "config": " (config.toml)",
                   "environment": f" (env {f['env']})"}[f["source"]]
            print(f"  {f['path']:<32} {value}{src}")
    print("\nChange with `hawksense settings set <setting> <value>` or in the app (Settings).")


def cmd_settings_set(t: Tracker, a):
    field = app_settings.FIELDS.get(a.path)
    value = a.value
    if a.settings_command == "unset":
        value = None
    elif field and field.kind == "percent":
        value = float(value.rstrip("%")) / 100 if value.endswith("%") else float(value)
    elif field and field.kind == "bool" or a.path.split(".")[-1] in ("ships_abroad", "collects_import_vat"):
        value = value.lower() in ("true", "yes", "on", "1")
    try:
        app_settings.update(t.db, {a.path: value})
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None
    shown = "••••••" if field and field.kind == "secret" and value else value
    print(f"{a.path} = {shown}" if value is not None else f"{a.path}: back to config.toml / default")


def cmd_remove(t: Tracker, a):
    item = _item(t, a.item)
    t.db.delete_item(item)
    print(f"Removed {item.name}")


def cmd_serve(t: Tracker, a):
    from hawksense.server import ServerContext, serve

    cfg = load_config()
    settings = server_settings(vars(a), cfg.server)
    t.db.close()  # the server opens its own connections
    ctx = ServerContext(t.db.path, cfg, _dest_code(a), bool(a.offline) or None, settings.token,
                        settings.base_path, settings.check_every, settings.rules_every)
    serve(ctx, settings.host, settings.port, settings.cert, settings.key, settings.check_every, settings.rules_every)


def cmd_healthcheck(t: Tracker, a):
    """Exit 0 if the local server answers /api/health (used by the Docker HEALTHCHECK)."""
    import ssl
    import urllib.request

    settings = server_settings({}, load_config().server)
    scheme = "https" if settings.cert else "http"
    url = f"{scheme}://127.0.0.1:{settings.port}{settings.base_path}/api/health"
    ctx = ssl._create_unverified_context() if settings.cert else None  # our own cert, on localhost
    try:
        with urllib.request.urlopen(url, timeout=4, context=ctx) as res:
            ok = json.load(res).get("ok")
    except Exception as exc:
        print(f"unhealthy: {exc}")
        raise SystemExit(1) from None
    print("ok" if ok else "unhealthy: database check failed")
    raise SystemExit(0 if ok else 1)


def cmd_backup(t: Tracker, a):
    target = Path(a.path)
    if target.is_dir() or a.path.endswith("/") or not target.suffix:  # a directory, existing or not
        target = target / f"hawksense-{date.today().isoformat()}.db"
    t.db.backup(target)
    print(f"Backed up {len(t.db.list_items())} items to {target}")
    if not (os.environ.get("HAWKSENSE_SECRET_KEY") or os.environ.get("HAWKSENSE_SECRET_KEY_FILE")):
        key = Path(t.db.path).resolve().parent / "secret.key"
        if key.exists():
            print(f"Passwords and keys saved in the app are encrypted with {key} - keep a copy of it too,\n"
                  "or enter them again after restoring.")


def cmd_demo(t: Tracker, a):
    item = seed_demo(t.db)
    print(f"Seeded demo item #{item.id}: {item.name}\n")
    cmd_compare(t, argparse.Namespace(item=str(item.id), routes=False, explore=False))
    print()
    cmd_advise(t, argparse.Namespace(item=str(item.id), json=False))


# ---- argument parsing --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hawksense", description="Smart price tracker with landed cost and "
                                "sales-day aware buying advice.")
    p.add_argument("--version", action="version", version=f"hawksense {__version__}")
    p.add_argument("--db", help="database path (default: $HAWKSENSE_HOME/hawksense.db)")
    p.add_argument("--dest", help="destination country: IL (default), US, EU, UK")
    p.add_argument("--offline", action="store_true", help="don't fetch live exchange rates")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("track", help="start tracking an item (optionally with product URLs)")
    s.add_argument("name")
    s.add_argument("--url", action="append", help="product page URL (repeatable)")
    s.add_argument("--category", help="electronics, computers, phones, appliances, clothing, shoes, toys ...")
    s.add_argument("--target", type=float, help="alert when landed price drops to this (destination currency)")
    s.add_argument("--no-check", action="store_true", help="don't fetch prices right away")
    s.add_argument("--weight", type=float, metavar="KG", help="shipping weight in kg (for forwarder rates)")
    s.add_argument("--dims", metavar="LxWxH", help="boxed size in cm, e.g. 30x20x10 (volumetric weight)")
    s.set_defaults(func=cmd_track)

    s = sub.add_parser("add-offer", help="add a store/URL (or an eBay search) to an item")
    s.add_argument("item")
    s.add_argument("url", nargs="?", help="product URL, or a store key for manual-only tracking (see `stores`)")
    s.add_argument("--shipping", type=float, help="shipping cost to you (overrides store policy)")
    s.add_argument("--shipping-currency", help="currency of --shipping and --local-shipping (default: price's)")
    s.add_argument("--local-shipping", type=float, help="store's shipping to a forwarder's warehouse")
    s.add_argument("--regex", help="custom regex whose first group is the price")
    s.add_argument("--ebay-search", metavar="QUERY", help="track the cheapest eBay listing for this search")
    s.add_argument("--condition", choices=["new", "used", "any"], default="new", help="for --ebay-search")
    s.add_argument("--ebay-site", choices=list(EBAY_SITES), default="ebay.com", help="for --ebay-search")
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

    s = sub.add_parser("compare", help="landed-cost comparison across stores (and forwarders)")
    s.add_argument("item")
    s.add_argument("--routes", action="store_true", help="itemise every way to get it: direct and each forwarder")
    s.add_argument("--explore", action="store_true", help="also price forwarders you haven't set up")
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

    s = sub.add_parser("specs", help="weight and box size found on the store pages, and whether they agree")
    s.add_argument("item")
    s.add_argument("--source", action="append", metavar="URL", help="also read this page (e.g. the manufacturer's)")
    s.set_defaults(func=cmd_specs)

    s = sub.add_parser("rules", help="tax and forwarder rules: show, check for updates, change")
    rsub = s.add_subparsers(dest="rules_command", required=True)
    r = rsub.add_parser("show", help="rules that differ from the built-in values, and where they come from")
    r.add_argument("filter", nargs="?", help="only rules containing this text, e.g. IL or dealtas")
    r.add_argument("--all", action="store_true", help="include built-in values")
    r.set_defaults(func=cmd_rules_show)
    r = rsub.add_parser("check", help="check the rules feed and your page sources now")
    r.set_defaults(func=cmd_rules_check)
    r = rsub.add_parser("set", help="set a rule yourself, e.g. `IL.vat_exempt_usd 150` or `dealtas.US.first 12`")
    r.add_argument("path")
    r.add_argument("value")
    r.set_defaults(func=cmd_rules_set)
    r = rsub.add_parser("unset", help="remove your own value for a rule")
    r.add_argument("path")
    r.set_defaults(func=cmd_rules_unset)
    r = rsub.add_parser("pending", help="fetched changes waiting for your review")
    r.set_defaults(func=cmd_rules_pending)
    for name in ("accept", "reject"):
        r = rsub.add_parser(name, help=f"{name} a pending change")
        r.add_argument("id", type=int)
        r.set_defaults(func=cmd_rules_decide)
    r = rsub.add_parser("history", help="every rule change, fetched and manual")
    r.add_argument("--limit", type=int, default=50)
    r.set_defaults(func=cmd_rules_history)

    s = sub.add_parser("notify", help="notifications: channels, which events go where, inbox")
    nsub = s.add_subparsers(dest="notify_command")
    s.set_defaults(func=cmd_notify_status)
    n = nsub.add_parser("status", help="channels and subscriptions")
    n.set_defaults(func=cmd_notify_status)
    n = nsub.add_parser("subscribe", help="send an event to these channels (none = off), e.g. buy_now inbox telegram")
    n.add_argument("event", help="event key, or 'all'")
    n.add_argument("channels", nargs="*")
    n.set_defaults(func=cmd_notify_subscribe)
    n = nsub.add_parser("set", help="price_drop_pct, sale_soon_days or check_failed_after")
    n.add_argument("key")
    n.add_argument("value", type=float)
    n.set_defaults(func=cmd_notify_set)
    n = nsub.add_parser("test", help="send a test message")
    n.add_argument("channel")
    n.set_defaults(func=cmd_notify_test)
    n = nsub.add_parser("inbox", help="show new notifications (and mark them read)")
    n.add_argument("--all", action="store_true")
    n.add_argument("--keep", action="store_true", help="don't mark them read")
    n.add_argument("--limit", type=int, default=30)
    n.set_defaults(func=cmd_notify_inbox)
    n = nsub.add_parser("run", help="look for new events now (check does this automatically)")
    n.set_defaults(func=cmd_notify_run)

    s = sub.add_parser("settings", help="settings you can also change in the app")
    ssub = s.add_subparsers(dest="settings_command")
    s.set_defaults(func=cmd_settings_show)
    x = ssub.add_parser("set", help="e.g. `notify.telegram.chat_id 123` or `stores.amazon_us.shipping_flat 15`")
    x.add_argument("path")
    x.add_argument("value")
    x.set_defaults(func=cmd_settings_set)
    x = ssub.add_parser("unset", help="remove the app's value (back to config.toml / default)")
    x.add_argument("path")
    x.set_defaults(func=cmd_settings_set)

    s = sub.add_parser("forwarders", help="package forwarders (shipping proxies), their rules and your addresses")
    s.set_defaults(func=cmd_forwarders)

    s = sub.add_parser("forwarder", help="set up or remove a package forwarder")
    fsub = s.add_subparsers(dest="forwarder_command", required=True)
    f = fsub.add_parser("add", help="register a forwarder (asks for the address(es) it gave you)")
    f.add_argument("key", help="e.g. dealtas, redbox, zipy (see `hawksense forwarders`)")
    f.add_argument("--warehouse", help="only this warehouse, e.g. US or UK (default: ask for each)")
    f.add_argument("--address", help="your address at that warehouse (skips the questions)")
    f.add_argument("--suite", help="your suite / customer number")
    f.add_argument("--sales-tax", type=float, metavar="RATE", help="override the sales tax, e.g. 0 or 0.07")
    f.set_defaults(func=cmd_forwarder_add)
    f = fsub.add_parser("remove", help="forget a forwarder (or one of its warehouses)")
    f.add_argument("key")
    f.add_argument("--warehouse")
    f.set_defaults(func=cmd_forwarder_remove)

    s = sub.add_parser("remove", help="stop tracking an item")
    s.add_argument("item")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("serve", help="run the web app / PWA (open it on your phone)")
    s.add_argument("--host", help="default 127.0.0.1; use 0.0.0.0 to reach it from other devices")
    s.add_argument("--port", type=int, help="default 8765")
    s.add_argument("--token", help="require this access token (recommended with --host 0.0.0.0)")
    s.add_argument("--token-file", help="read the access token from a file (e.g. a Docker secret)")
    s.add_argument("--cert", help="TLS certificate (PEM) - needed for offline mode on iOS")
    s.add_argument("--key", help="TLS private key (PEM)")
    s.add_argument("--check-every", type=float, metavar="HOURS", help="fetch all prices every N hours")
    s.add_argument("--base-path", help="URL prefix when a reverse proxy doesn't strip it, e.g. /hawksense")
    s.add_argument("--rules-every", type=float, metavar="DAYS", help="check for rule updates every N days "
                   "(default 7, 0 = never)")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("healthcheck", help="check that the local server is up (for Docker/monitoring)")
    s.set_defaults(func=cmd_healthcheck)

    s = sub.add_parser("backup", help="consistent copy of the database (safe while the server runs)")
    s.add_argument("path", help="target file, or a directory for hawksense-YYYY-MM-DD.db")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("demo", help="load a demo item with 14 months of synthetic history")
    s.set_defaults(func=cmd_demo)
    return p


def _dest_code(args) -> str | None:
    return args.dest or os.environ.get("HAWKSENSE_DEST") or None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Database(db_path(args.db))
    try:
        cfg = app_settings.effective(db, load_config())
        # --dest for this command > the app's setting > HAWKSENSE_DEST > config.toml
        dest = args.dest or app_settings.dest_code(db) or os.environ.get("HAWKSENSE_DEST") or None
        tracker = Tracker.from_config(db, FX(db, offline=args.offline or None), cfg, dest)
        args.func(tracker, args)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
