"""Tax and forwarder rules: where the numbers come from, and keeping them current.

Rules are layered, later layers winning:

1. **built-in** values (``hawksense.landed.DESTINATIONS``, ``hawksense.forwarders.FORWARDERS``),
2. **fetched** updates from the rules feed and your page sources (automatic checks),
3. your **config.toml** (``[destination]``, ``[forwarders.*]``),
4. **manual** changes made with ``hawksense rules set`` or in the web app.

Every value has a dotted path, e.g. ``destination.IL.vat_exempt_usd`` or
``forwarders.dealtas.warehouses.US.first``.

Automatic checks read:

* the **rules feed**: a JSON file with the same shape as ``rules/rules.json`` in
  this repository (the default feed), maintained as tax rules and price lists
  change; point ``[rules] feed_url`` at your own copy, or set it to "" to turn it off,
* **page sources** you configure: a customs or forwarder web page plus either a
  regex (for one number) or ``format = "table"`` (a weight/price table)::

      [rules.sources.il_exemption]
      url = "https://example.gov/personal-import"
      target = "destination.IL.vat_exempt_usd"
      regex = 'up to \\$\\s*(\\d+)'

      [rules.sources.dealtas_us]
      url = "https://example.com/prices"
      target = "forwarders.dealtas.warehouses.US"
      format = "table"

A fetched value that fails validation is dropped; one that moves more than 50%
from the current value is held for review (``hawksense rules pending``) and you
are notified. Everything is logged (``hawksense rules history``).
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from hawksense import netguard
from hawksense.fetch import FetchError, fetch_html, parse_number
from hawksense.forwarders import FORWARDERS, Forwarder, forwarders_from_config
from hawksense.landed import DESTINATIONS, Destination, destination_from_config

DEFAULT_FEED = "https://raw.githubusercontent.com/de-Bat/HawkSense/main/rules/rules.json"
FEED_VERSION = 1
REVIEW_CHANGE = 0.5  # fetched values moving more than this (relative) wait for your review
LAYERS = ("builtin", "fetched", "config", "manual")

_DEST_FIELDS = {
    "vat_rate": (0.0, 0.5), "vat_exempt_usd": (0.0, 10000.0), "duty_exempt_usd": (0.0, 10000.0),
    "clearance_fee": (0.0, 1000.0), "threshold_includes_shipping": bool, "name": str, "currency": str,
}
_FWD_FIELDS = {
    "handling_fee": (0.0, 1000.0), "tax_handling_fee": (0.0, 1000.0), "insurance_min": (0.0, 1000.0),
    "service_fee_min": (0.0, 1000.0), "insurance_rate": (0.0, 0.5), "service_fee_rate": (0.0, 0.5),
    "collects_import_taxes": bool, "needs_address": bool, "name": str, "currency": str, "notes": str,
}
_WH_FIELDS = {
    "first": (0.0, 1000.0), "additional": (0.0, 1000.0), "first_kg": (0.01, 50.0), "step_kg": (0.01, 50.0),
    "min_kg": (0.0, 100.0), "vol_divisor": (0.0, 20000.0), "min_price": (0.0, 500.0), "sales_tax": (0.0, 0.3),
    "country": str,
    "location": str, "currency": str, "transit": str,
}


# ---- dotted paths ------------------------------------------------------------------------

def flatten(tree: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def unflatten(flat: dict) -> dict:
    tree: dict = {}
    for path, value in flat.items():
        node = tree
        *parents, leaf = path.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = value
    return tree


def merge(*trees: dict) -> dict:
    out: dict = {}
    for tree in trees:
        for key, value in (tree or {}).items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = merge(out[key], value)
            else:
                out[key] = value
    return out


def normalize_path(path: str, forwarders: dict | None = None) -> str:
    """Accept shorthands: 'IL.vat_rate', 'dealtas.tax_handling_fee', 'dealtas.US.first'."""
    parts = path.strip().split(".")
    if parts[0] in ("destination", "forwarders"):
        pass
    elif parts[0].upper() in DESTINATIONS and len(parts) >= 2:
        parts = ["destination", parts[0].upper(), *parts[1:]]
    elif parts[0] in (forwarders or FORWARDERS) or len(parts) >= 2:
        if len(parts) == 3 and parts[1] != "warehouses":
            parts = [parts[0], "warehouses", *parts[1:]]
        parts = ["forwarders", *parts]
    if parts[0] == "destination" and len(parts) > 1:
        parts[1] = parts[1].upper()
    if parts[0] == "forwarders" and len(parts) > 3 and parts[2] == "warehouses":
        parts[3] = parts[3].upper()
    return ".".join(parts)


def validate(path: str, value) -> str | None:
    """Why a value can't be used at this path, or None if it's fine."""
    parts = path.split(".")
    spec = None
    if parts[0] == "destination" and len(parts) == 3:
        spec = _DEST_FIELDS.get(parts[2])
    elif parts[0] == "destination" and len(parts) == 4 and parts[2] == "duty_rates":
        spec = (0.0, 1.0)
    elif parts[0] == "forwarders" and len(parts) == 3:
        spec = _FWD_FIELDS.get(parts[2])
    elif parts[0] == "forwarders" and len(parts) == 5 and parts[2] == "warehouses":
        spec = _WH_FIELDS.get(parts[4])
    if spec is None:
        return f"unknown rule {path!r}"
    if spec is bool:
        return None if isinstance(value, bool) else f"{path} must be true or false"
    if spec is str:
        return None if isinstance(value, str) and len(value) < 200 else f"{path} must be text"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{path} must be a number"
    lo, hi = spec
    return None if lo <= value <= hi else f"{path} must be between {lo:g} and {hi:g}"


def parse_value(text: str):
    low = text.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return float(text.rstrip("%")) / 100 if text.strip().endswith("%") else float(text)
    except ValueError:
        return text


# ---- built-in values and the effective rules ------------------------------------------------

def _dest_tree(d: Destination) -> dict:
    tree = {k: v for k, v in asdict(d).items() if k in _DEST_FIELDS}
    tree["duty_rates"] = dict(d.duty_rates)
    return tree


def _fwd_tree(f: Forwarder) -> dict:
    tree = {k: v for k, v in asdict(f).items() if k in _FWD_FIELDS}
    tree["warehouses"] = {}
    for w in f.warehouses:
        wt = {"country": w.country, "location": w.location, "sales_tax": w.sales_tax, "transit": w.transit}
        wt.update({k: v for k, v in asdict(w.rate).items() if k in _WH_FIELDS and v is not None})
        tree["warehouses"][w.code] = wt
    return tree


def builtin_rules() -> dict:
    return {"destination": {c: _dest_tree(d) for c, d in DESTINATIONS.items()},
            "forwarders": {k: _fwd_tree(f) for k, f in FORWARDERS.items()}}


def _config_layer(cfg, dest_code: str | None = None) -> dict:
    tree: dict = {}
    if cfg is None:
        return tree
    dest = dict(cfg.destination or {})
    code = str(dest.pop("code", "IL")).upper()
    if dest and (dest_code is None or dest_code.upper() == code):
        tree["destination"] = {code: dest}
    if cfg.forwarders:
        tree["forwarders"] = cfg.forwarders
    return tree


def layers(db, cfg, dest_code: str | None = None) -> dict[str, dict]:
    return {"builtin": builtin_rules(), "fetched": db.rule_layer("fetched"),
            "config": _config_layer(cfg, dest_code), "manual": db.rule_layer("manual")}


def build(db, cfg, dest_code: str | None = None) -> tuple[Destination, dict[str, Forwarder]]:
    """The destination tax profile and forwarders with every rules layer applied."""
    code = (dest_code or str((getattr(cfg, "destination", None) or {}).get("code", "IL"))).upper()
    ls = layers(db, cfg, code)
    over = merge(ls["fetched"], ls["config"], ls["manual"])
    dest_over = (over.get("destination") or {}).get(code, {})
    dest = destination_from_config({"code": code, **dest_over})
    return dest, forwarders_from_config(over.get("forwarders"), strict=False)


def explain(db, cfg, dest_code: str | None = None) -> dict[str, tuple[object, str]]:
    """path -> (effective value, layer it comes from)."""
    out: dict[str, tuple[object, str]] = {}
    for name, tree in layers(db, cfg, dest_code).items():
        for path, value in flatten(tree).items():
            out[path] = (value, name)
    return out


# ---- manual changes ----------------------------------------------------------------------------

def set_manual(db, changes: dict, forwarders: dict | None = None) -> list[str]:
    """Apply {path: value} (None removes your override). Returns the normalized paths changed."""
    manual = flatten(db.rule_layer("manual"))
    normalized = {}
    for path, value in changes.items():
        path = normalize_path(path, forwarders)
        if value is not None and (err := validate(path, value)):
            raise ValueError(err)
        normalized[path] = value
    for path, value in normalized.items():
        old = manual.get(path)
        if value is None:
            manual.pop(path, None)
        else:
            manual[path] = value
        db.log_rule_change("manual", "you", path, old, value, "applied" if value is not None else "removed")
    db.save_rule_layer("manual", unflatten(manual))
    return list(normalized)


# ---- automatic checks ------------------------------------------------------------------------------

@dataclass
class Change:
    path: str
    old: object
    new: object
    source: str
    id: int | None = None
    note: str = ""


@dataclass
class Report:
    applied: list[Change] = field(default_factory=list)
    pending: list[Change] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def _fetch_json(url: str) -> dict:
    try:
        raw, charset = netguard.fetch(url, {"User-Agent": "HawkSense rules check", "Accept": "application/json"})
        return json.loads(raw.decode(charset))
    except Exception as exc:
        raise FetchError(f"rules feed {url}: {exc}") from None


def parse_rate_table(page: str) -> dict | None:
    """Weight/price rows ('0.5 kg | $12.00') -> {first, first_kg, step_kg, additional}."""
    pairs = {}
    for row in re.findall(r"<tr\b.*?</tr>", page, re.S | re.I):
        cells = [re.sub(r"<[^>]+>|&nbsp;", " ", c).strip()
                 for c in re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", row, re.S | re.I)]
        if len(cells) < 2:
            continue
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|lbs?|g)\b", cells[0], re.I)
        price = next((parse_number(c) for c in cells[1:] if re.search(r"\d", c)), None)
        if not m or not price:
            continue
        kg = float(m.group(1).replace(",", ".")) * {"kg": 1, "g": 0.001}.get(m.group(2).lower(), 0.45359237)
        pairs[round(kg, 3)] = price
    if len(pairs) < 3:
        return None
    ws = sorted(pairs)
    steps = [round(b - a, 3) for a, b in zip(ws, ws[1:])]
    step = statistics.mode(steps)
    extra = [(pairs[b] - pairs[a]) for a, b in zip(ws, ws[1:]) if round(b - a, 3) == step]
    return {"first": pairs[ws[0]], "first_kg": ws[0], "step_kg": step, "additional": round(statistics.median(extra), 2)}


def _incoming(rcfg: dict, report: Report, fetch_json=_fetch_json, fetch_page=fetch_html) -> dict[str, tuple]:
    """Collect path -> (value, source) from the feed and every page source."""
    incoming: dict[str, tuple] = {}
    feed_url = rcfg.get("feed_url", DEFAULT_FEED)
    if feed_url:
        report.sources.append(feed_url)
        try:
            feed = fetch_json(feed_url)
            if int(feed.get("version", 0)) != FEED_VERSION:
                raise FetchError(f"rules feed version {feed.get('version')!r} not supported - update HawkSense")
            for section in ("destination", "forwarders"):
                for path, value in flatten({section: feed.get(section) or {}}).items():
                    incoming[path] = (value, "feed")
        except (FetchError, ValueError, TypeError, AttributeError) as exc:
            report.errors.append(str(exc))
    for name, src in (rcfg.get("sources") or {}).items():
        url, target = src.get("url"), src.get("target", "")
        if not url or not target:
            report.errors.append(f"rules source {name}: needs url and target")
            continue
        report.sources.append(url)
        try:
            page = fetch_page(url)
            if src.get("format") == "table":
                table = parse_rate_table(page)
                if not table:
                    raise FetchError("no weight/price table found")
                for k, v in table.items():
                    incoming[normalize_path(f"{target}.{k}")] = (v, name)
            else:
                m = re.search(src.get("regex", ""), page, re.S | re.I) if src.get("regex") else None
                if not m or (value := parse_number(m.group(1))) is None:
                    raise FetchError("pattern not found on the page")
                incoming[normalize_path(target)] = (value * float(src.get("scale", 1)), name)
        except (FetchError, re.error, IndexError) as exc:
            report.errors.append(f"rules source {name}: {exc}")
    return incoming


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) < 1e-9
    return a == b


def check_updates(db, cfg, fetch_json=_fetch_json, fetch_page=fetch_html) -> Report:
    """Fetch the feed and page sources, apply sane changes, hold suspicious ones for review."""
    report = Report()
    rcfg = getattr(cfg, "rules", None) or {}
    incoming = _incoming(rcfg, report, fetch_json, fetch_page)
    fetched = flatten(db.rule_layer("fetched"))
    baseline = merge(builtin_rules(), db.rule_layer("fetched"))
    base_flat = flatten(baseline)
    decided = {(c["path"], json.dumps(c["new"])) for c in db.rule_changes(limit=1000)
               if c["status"] in ("rejected", "pending")}
    for path, (value, source) in sorted(incoming.items()):
        if isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if err := validate(path, value):
            report.errors.append(f"{source}: {err}")
            continue
        old = base_flat.get(path)
        if _same(old, value) or (path, json.dumps(value)) in decided:
            continue
        change = Change(path, old, value, source)
        numeric = isinstance(old, (int, float)) and not isinstance(old, bool)
        if numeric and old and abs(value - old) / abs(old) > REVIEW_CHANGE:
            change.note = f"changes by {abs(value - old) / abs(old):.0%} - please confirm"
            change.id = db.log_rule_change("fetched", source, path, old, value, "pending", change.note)
            report.pending.append(change)
        else:
            fetched[path] = value
            change.id = db.log_rule_change("fetched", source, path, old, value, "applied")
            report.applied.append(change)
    if report.applied:
        try:  # a feed may add a service; make sure it's complete enough to price with
            forwarders_from_config(unflatten(fetched).get("forwarders"))
        except SystemExit as exc:
            bad = [c for c in report.applied if c.path.split(".")[1] not in FORWARDERS
                   and c.path.startswith("forwarders.")]
            for c in bad:
                fetched.pop(c.path, None)
                db.set_rule_change_status(c.id, "invalid")
                report.applied.remove(c)
            report.errors.append(f"incomplete forwarder in the feed: {exc}")
        db.save_rule_layer("fetched", unflatten(fetched))
    db.set_kv("rules_last_check", json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(), "applied": len(report.applied),
        "pending": len(report.pending), "errors": report.errors}))
    return report


def decide(db, change_id: int, accept: bool) -> dict:
    """Accept or reject a pending fetched change."""
    change = next((c for c in db.rule_changes("pending", 1000) if c["id"] == change_id), None)
    if change is None:
        raise ValueError(f"no pending rule change #{change_id}")
    if accept:
        fetched = flatten(db.rule_layer("fetched"))
        fetched[change["path"]] = change["new"]
        db.save_rule_layer("fetched", unflatten(fetched))
    db.set_rule_change_status(change_id, "accepted" if accept else "rejected")
    return change


def last_check(db) -> dict | None:
    raw = db.get_kv("rules_last_check")
    return json.loads(raw) if raw else None


def describe(path: str) -> str:
    """'destination.IL.vat_exempt_usd' -> 'IL vat exempt usd'."""
    parts = path.split(".")
    if parts[0] == "destination":
        parts = parts[1:]
    elif parts[0] == "forwarders":
        parts = [p for p in parts[1:] if p != "warehouses"]
    return " ".join(p.replace("_", " ") for p in parts)
