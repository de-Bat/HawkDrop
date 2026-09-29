"""Shipping weight and box size, read from product pages and cross-checked.

Forwarders charge by weight (or volumetric weight), so HawkSense tries to learn
an item's size from the store pages it already checks:

1. schema.org ``Product`` JSON-LD: ``weight``, ``width``/``height``/``depth`` and
   ``additionalProperty`` entries,
2. spec tables and lists ("Item Weight", "Package Dimensions", "משקל", "מידות" ...).

Every page is one observation. ``consensus`` groups the pages whose values agree
(weights within 15%, box volumes within 30%):

- one group: its value is used - *verified* with two or more pages, *unverified* with one;
- one group bigger than the rest (*majority*): its value is used;
- the biggest groups tie (*conflict*) or nothing was found (*missing*): no value is used,
  the user is alerted, and forwarder prices wait until they set it.

A value that is used while other pages disagree or don't list it is flagged for the
user to confirm, with what each page said. A weight or size the user set (or
confirmed) always wins.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import statistics
from dataclasses import dataclass, field

AGREE = 0.15  # relative spread for sources to count as agreeing
CONFLICT = 0.30  # box volumes this close still agree (small differences on three sides add up)

_WEIGHT_UNITS = {
    "kg": 1.0, "kgs": 1.0, "kilogram": 1.0, "kilograms": 1.0, "kilogramm": 1.0, "gramm": 0.001, "ק\"ג": 1.0, "קג": 1.0, "קילו": 1.0,
    "g": 0.001, "gr": 0.001, "gram": 0.001, "grams": 0.001, "גרם": 0.001,
    "lb": 0.45359237, "lbs": 0.45359237, "pound": 0.45359237, "pounds": 0.45359237,
    "oz": 0.028349523, "ounce": 0.028349523, "ounces": 0.028349523,
}
_LENGTH_UNITS = {"cm": 1.0, "ס\"מ": 1.0, "mm": 0.1, "מ\"מ": 0.1, "in": 2.54, '"': 2.54, "m": 100.0}
_UNIT_CODES = {"kgm": "kg", "grm": "g", "lbr": "lb", "onz": "oz", "cmt": "cm", "mmt": "mm", "inh": "in", "mtr": "m"}

_NUM = r"(\d+(?:[.,]\d+)?)"
_WEIGHT_RE = re.compile(_NUM + r"\s*(kilogramm?|kilograms?|kgs?|gramm?|grams?|gr|g|pounds?|lbs?|ounces?|oz|ק\"ג|קג|קילו|גרם)(?![a-z])",
                        re.I)
# Amazon writes sizes as '5 x 3 x 2 inches', '3.9"D x 2.4"W x 2"H' or '5 by 3 by 2 inches'
_SEP = r"\s*(?:[dwhl](?![a-z]))?\s*(?:[x×*]|by)\s*"
_UNIT = r"(?:centimet(?:er|re)s?|cm|millimet(?:er|re)s?|mm|inch(?:es)?|in(?![a-z])|\"|״|”|″|ס\"מ|מ\"מ)?"
_DIMS_RE = re.compile(_NUM + r"\s*" + _UNIT + _SEP + _NUM + r"\s*" + _UNIT + _SEP + _NUM + r"\s*(" + _UNIT[3:], re.I)

# Whole-label patterns (after lower-casing and dropping "(kg)"-style hints) -> kind.
# Package/shipping values are what a forwarder weighs; "item" is the product alone.
_WEIGHT_LABELS = [
    (re.compile(r"(?:item |product )?(?:shipping|package|parcel|boxed|gross) weight|versandgewicht|verpackungsgewicht|משקל (?:ה?אריזה|משלוח|ברוטו)"),
     "package"),
    (re.compile(r"(?:item |product |net |unit )?weight|משקל(?: ה?מוצר| נטו)?|artikelgewicht|produktgewicht|gewicht"),
     "item"),
]
_DIMS_LABELS = [
    (re.compile(r"(?:item )?(?:package|shipping|box|parcel|boxed) (?:dimensions|size)(?: l ?x ?w ?x ?h)?"
                r"|verpackungsabmessungen|packungsabmessungen|מידות (?:ה?אריזה|משלוח)"), "package"),
    (re.compile(r"(?:product |item |overall )?(?:dimensions|measurements|size)|produktabmessungen|artikelabmessungen|abmessungen(?: l ?x ?w ?x ?h| w ?x ?h ?x ?d)?"
                r"|מידות(?: ה?מוצר)?"), "item"),
]


def _num(text: str) -> float:
    return float(text.replace(",", "."))


@dataclass
class Specs:
    weight_kg: float | None = None
    dims_cm: tuple[float, float, float] | None = None
    weight_kind: str = "item"  # "package" (boxed/shipping) or "item" (the product alone)
    dims_kind: str = "item"
    method: str = ""

    def __bool__(self) -> bool:
        return self.weight_kg is not None or self.dims_cm is not None


def parse_weight(text: str) -> float | None:
    """'1.2 pounds' -> 0.544, '350 g' -> 0.35, '1,5 ק"ג' -> 1.5."""
    m = _WEIGHT_RE.search(str(text))
    if not m:
        return None
    kg = _num(m.group(1)) * _WEIGHT_UNITS[m.group(2).lower()]
    return round(kg, 3) if 0.005 <= kg <= 150 else None


def parse_dimensions(text: str) -> tuple[float, float, float] | None:
    """'10.2 x 7.3 x 3 inches' -> (25.9, 18.5, 7.6) cm; no unit means cm."""
    m = _DIMS_RE.search(str(text))
    if not m:
        return None
    unit = (m.group(4) or "cm").lower()
    if unit in ("״", "”", "″"):
        unit = '"'
    if unit.startswith("centimet"):
        unit = "cm"
    elif unit.startswith("millimet"):
        unit = "mm"
    elif unit.startswith("inch"):
        unit = "in"
    factor = _LENGTH_UNITS.get(unit, 1.0)  # "in" and '"' -> 2.54
    dims = tuple(round(_num(m.group(i)) * factor, 1) for i in (1, 2, 3))
    return dims if all(0.3 <= d <= 400 for d in dims) else None


def _unit_value(node) -> str:
    """schema.org QuantitativeValue -> '1.2 kg' (unitCode KGM/GRM/LBR/ONZ/CMT/MMT/INH)."""
    if isinstance(node, dict):
        value = node.get("value")
        unit = str(node.get("unitText") or node.get("unitCode") or "").lower()
        return f"{value} {_UNIT_CODES.get(unit, unit)}".strip()
    return str(node)


def _from_json_ld(page: str) -> Specs:
    from hawksense.fetch import _is_type, _walk, ld_json_blocks  # shared JSON-LD helpers

    specs = Specs(method="json-ld")
    for block in ld_json_blocks(page):
        try:
            data = json.loads(html_lib.unescape(block.strip()))
        except json.JSONDecodeError:
            continue
        for node in _walk(data):
            if not (_is_type(node, "Product") or _is_type(node, "ProductModel")):
                continue
            if specs.weight_kg is None and node.get("weight") is not None:
                specs.weight_kg = parse_weight(_unit_value(node["weight"]))
            sides = [node.get(k) for k in ("depth", "width", "height")]
            if specs.dims_cm is None and all(s is not None for s in sides):
                dims = tuple(_side_cm(s) for s in sides)
                if all(0.3 <= d <= 400 for d in dims):
                    specs.dims_cm = dims
            for prop in node.get("additionalProperty") or []:
                if isinstance(prop, dict):
                    _apply_label(specs, str(prop.get("name", "")), _unit_value(prop.get("value", "")) + " "
                                 + str(prop.get("unitText") or ""))
    return specs


def _side_cm(node) -> float:
    """One QuantitativeValue side (width/height/depth) in cm."""
    m = re.match(r"\s*" + _NUM + r"\s*([a-z\"]*)", _unit_value(node), re.I)
    if not m:
        return 0.0
    unit = m.group(2).lower() or "cm"
    return round(_num(m.group(1)) * _LENGTH_UNITS.get("in" if unit.startswith("inch") else unit, 1.0), 1)


def _apply_label(specs: Specs, label: str, value: str) -> None:
    hint = re.search(r"\(([^)]*)\)", label)  # "Weight (kg)": 1.2
    label = re.sub(r"\s+", " ", re.sub(r"\([^)]*\)|[:\u200e\u200f]", " ", label)).strip().lower()
    label = re.sub(r"\s+(?:כ-?|בערך|approx\.?|approximately)$", "", label)  # "מידות כ-" (approx.)
    if hint and not re.search(r"[a-z\u0590-\u05ff\"]", value, re.I):
        value = f"{value} {hint.group(1)}"
    for pattern, kind in _DIMS_LABELS:
        if pattern.fullmatch(label):
            dims = parse_dimensions(value)
            if dims and (specs.dims_cm is None or (kind == "package" and specs.dims_kind != "package")):
                specs.dims_cm, specs.dims_kind = dims, kind
            # Amazon: "Package Dimensions: 10 x 7 x 3 inches; 8.8 ounces"
            weight = parse_weight(value) if dims else None
            if weight and (specs.weight_kg is None or (kind == "package" and specs.weight_kind != "package")):
                specs.weight_kg, specs.weight_kind = weight, kind
            if dims:
                return
            break
    for pattern, kind in _WEIGHT_LABELS:
        if pattern.fullmatch(label):
            weight = parse_weight(value)
            if weight and (specs.weight_kg is None or (kind == "package" and specs.weight_kind != "package")):
                specs.weight_kg, specs.weight_kind = weight, kind
            return


# Linear-time scanning (pages come from anywhere): each opening tag only looks a bounded
# distance ahead for its closing tag, instead of a ".*?" that can run to the end of the page.
_BLOCK_OPEN = re.compile(r"<(script|style|noscript)\b", re.I)
_ROW_OPEN = re.compile(r"<(tr|li|div|p)\b[^>]{0,500}>", re.I)
_DT_OPEN = re.compile(r"<dt\b[^>]{0,200}>", re.I)
_DD_OPEN = re.compile(r"\s{0,50}<dd\b[^>]{0,200}>", re.I)
ROW_MAX = 600  # longer "rows" are layout, not a spec line
_CELL_SPLIT = re.compile(r"</(?:th|td|dt|dd|span|b|strong|label)>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(_TAG_RE.sub(" ", fragment))).replace("‎", "").strip()


def _strip_blocks(page: str) -> str:
    """Drop <script>, <style> and <noscript> blocks."""
    lower, out, pos = page.lower(), [], 0
    while m := _BLOCK_OPEN.search(lower, pos):
        out.append(page[pos:m.start()])
        end = lower.find(f"</{m.group(1)}", m.end())
        if end < 0:
            return " ".join(out)  # unclosed: the rest is script
        close = lower.find(">", end)
        pos = close + 1 if close >= 0 else len(page)
    out.append(page[pos:])
    return " ".join(out)


def _dl_pairs(body: str, lower: str):
    for m in _DT_OPEN.finditer(lower):
        end = lower.find("</dt>", m.end(), m.end() + ROW_MAX)
        if end < 0:
            continue
        dd = _DD_OPEN.match(lower, end + 5)
        if not dd:
            continue
        close = lower.find("</dd>", dd.end(), dd.end() + ROW_MAX)
        if close >= 0:
            yield body[m.end():end], body[dd.end():close]


def _rows(body: str, lower: str):
    for m in _ROW_OPEN.finditer(lower):
        end = lower.find(f"</{m.group(1)}>", m.end(), m.end() + ROW_MAX + 10)
        if end >= 0:
            yield body[m.end():end]


def _from_tables(page: str) -> Specs:
    specs = Specs(method="spec-table")
    body = _strip_blocks(page)
    lower = body.lower()
    for label, value in _dl_pairs(body, lower):
        _apply_label(specs, _text(label), _text(value))
    for row in _rows(body, lower):
        if len(row) > ROW_MAX:
            continue
        cells = [c for c in (_text(x) for x in _CELL_SPLIT.split(row)) if c]
        if len(cells) >= 2:
            label, value = cells[0].rstrip(":").strip(), " ".join(cells[1:])
        elif cells and ":" in cells[0]:
            label, _, value = cells[0].partition(":")
        else:
            continue
        if len(label) <= 40:
            _apply_label(specs, label, value)
    return specs


def specs_from_pairs(pairs: list[tuple[str, str]], method: str = "attributes") -> Specs:
    """Specs from name/value attributes (e.g. eBay item specifics)."""
    specs = Specs(method=method)
    for name, value in pairs:
        _apply_label(specs, str(name), str(value))
    return specs


# Newegg's page data: the item's shipping box, in pounds and inches (the item comes first)
_NEWEGG_BOX = re.compile(r'"Weight":(\d{1,4}(?:\.\d+)?),"Length":(\d{1,4}(?:\.\d+)?),"Width":(\d{1,4}(?:\.\d+)?),'
                         r'"Height":(\d{1,4}(?:\.\d+)?),"ShippingCharge"')


def _from_store_data(page: str) -> Specs:
    specs = Specs(method="store-data")
    if m := _NEWEGG_BOX.search(page):
        weight = parse_weight(f"{m.group(1)} lb")
        dims = parse_dimensions(f"{m.group(2)} x {m.group(3)} x {m.group(4)} in")
        if weight:
            specs.weight_kg, specs.weight_kind = weight, "package"
        if dims:
            specs.dims_cm, specs.dims_kind = dims, "package"
    return specs


def extract_specs(page: str) -> Specs:
    """Weight and size found on a product page (empty ``Specs`` if none)."""
    ld = _from_json_ld(page)
    table = _from_tables(page)
    store = _from_store_data(page)
    out = Specs(method="+".join(m for m, s in (("json-ld", ld), ("spec-table", table), ("store-data", store)) if s))
    for src in (ld, table, store):  # structured data first; package values beat item values
        if src.weight_kg is not None and (out.weight_kg is None or
                                          (src.weight_kind == "package" and out.weight_kind != "package")):
            out.weight_kg, out.weight_kind = src.weight_kg, src.weight_kind
        if src.dims_cm is not None and (out.dims_cm is None or
                                        (src.dims_kind == "package" and out.dims_kind != "package")):
            out.dims_cm, out.dims_kind = src.dims_cm, src.dims_kind
    return out


# ---- consensus -----------------------------------------------------------------------------

@dataclass
class Observation:
    source: str  # store name or URL
    url: str
    weight_kg: float | None
    dims_cm: tuple[float, float, float] | None
    weight_kind: str = "item"
    dims_kind: str = "item"


@dataclass
class Consensus:
    status: str  # verified | unverified | conflict | missing
    weight_kg: float | None = None
    dims_cm: tuple[float, float, float] | None = None
    weight_status: str = "missing"
    dims_status: str = "missing"
    messages: list[str] = field(default_factory=list)
    # values that are used although some pages don't list them: {"weight"|"dims": what to confirm, and why}
    confirm: dict[str, str] = field(default_factory=dict)

    @property
    def dims_text(self) -> str | None:
        return "x".join(f"{d:g}" for d in self.dims_cm) if self.dims_cm else None

    @property
    def alert(self) -> bool:
        """Nothing usable, or the pages split evenly: no value is used and forwarder prices wait for you."""
        return self.status in ("conflict", "missing")

    def to_confirm(self, weight_manual: bool = False, dims_manual: bool = False) -> list[str]:
        """What to ask the user to confirm, leaving out what they already set themselves."""
        return [msg for key, msg in self.confirm.items()
                if not (key == "weight" and weight_manual) and not (key == "dims" and dims_manual)]


PACKAGING_FACTOR, PACKAGING_KG = 1.1, 0.1  # boxed weight estimated from the product's own weight
DIMS_AGREE = CONFLICT

Entry = tuple[str, float, str]  # (page, value compared, how it's shown)


def _names(names: list[str]) -> str:
    """['A'] -> 'A', ['A', 'B', 'C'] -> 'A, B and C'."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _lacks(missing: list[str], noun: str) -> str:
    return f"{_names(missing)} {'does' if len(missing) == 1 else 'do'}n't list {noun}"


def _backed_by(package_value: float, item_values: list[float], low: float) -> bool:
    """An item value slightly below (or equal to) the boxed value corroborates it."""
    return any(low * package_value <= v <= package_value * (1 + AGREE) for v in item_values)


def _said(group: list[Entry]) -> str:
    return _names([f"{src} ({label})" for src, _, label in group])


def _groups(entries: list[Entry], tolerance: float) -> list[list[Entry]]:
    """Pages whose values agree (within ``tolerance`` of the group's smallest), biggest group first."""
    groups: list[list[Entry]] = []
    for e in sorted(entries, key=lambda e: e[1]):
        if groups and e[1] <= groups[-1][0][1] * (1 + tolerance):
            groups[-1].append(e)
        else:
            groups.append([e])
    return sorted(groups, key=len, reverse=True)  # stable: equal sizes stay smallest-value first


def _pick(entries: list[Entry], tolerance: float) -> tuple[str, float | None, list[list[Entry]]]:
    """-> (status, value, groups). A tie between the biggest groups gives no value."""
    if not entries:
        return "missing", None, []
    groups = _groups(entries, tolerance)
    if len(groups) > 1 and len(groups[0]) == len(groups[1]):
        return "conflict", None, groups
    value = statistics.median(v for _, v, _ in groups[0])
    if len(groups) > 1:
        return "majority", value, groups
    return ("verified" if len(groups[0]) > 1 else "unverified"), value, groups


def _describe(what: str, noun: str, shown: str, groups: list[list[Entry]], missing: list[str]) -> str:
    """'Weight 1.05 kg: Amazon (1 kg boxed) and B&H (1.1 kg boxed) agree, but KSP (2.5 kg boxed) differs;
    Bug doesn't list a weight.'"""
    top, others = groups[0], [e for g in groups[1:] for e in g]
    text = (f"{what} {shown} comes only from {_said(top)}" if len(top) == 1
            else f"{what} {shown}: {_said(top)} agree")
    if others:
        text += f", but {_said(others)} {'differs' if len(others) == 1 else 'differ'}"
    if missing:
        text += f"; {_lacks(missing, noun)}"
    return text + "."


def _split(what: str, noun: str, groups: list[list[Entry]], missing: list[str]) -> str:
    """'No weight is used: the pages split evenly - Amazon (1 kg) vs B&H (2.5 kg).'"""
    text = f"No {what} is used: the store pages split evenly - " + " vs ".join(_said(g) for g in groups)
    if missing:
        text += f"; {_lacks(missing, noun)}"
    return text + ". Please set it yourself."


def consensus(observations: list[Observation]) -> Consensus:
    msgs: list[str] = []
    confirm: dict[str, str] = {}
    pages = list(dict.fromkeys(o.source for o in observations))

    # weight: prefer package weights; fall back to item weight plus packaging
    pkg = [(o.source, o.weight_kg, f"{o.weight_kg:g} kg boxed") for o in observations
           if o.weight_kg and o.weight_kind == "package"]
    item = [(o.source, o.weight_kg, f"{o.weight_kg:g} kg") for o in observations
            if o.weight_kg and o.weight_kind != "package"]
    use, estimated = (pkg, False) if pkg else (item, True)
    w_status, weight, groups = _pick(use, AGREE)
    if w_status == "unverified" and not estimated and _backed_by(weight, [v for _, v, _ in item], 0.6):
        w_status = "verified"  # another page's product weight fits inside this boxed weight
    no_weight = [p for p in pages if p not in {o.source for o in observations if o.weight_kg}]
    if weight is not None:
        if estimated:
            weight = round(weight * PACKAGING_FACTOR + PACKAGING_KG, 3)
            msgs.append("only the product weight was found - added ~10% + 0.1 kg for the box")
        if w_status == "majority" or no_weight:
            shown = f"{weight:g} kg" + (" (the product's weight plus an allowance for the box)" if estimated else "")
            confirm["weight"] = _describe("Weight", "a weight", shown, groups, no_weight)
    elif w_status == "conflict":
        msgs.append(_split("weight", "a weight", groups, no_weight))
    else:
        msgs.append("no weight found on any store page" + (f" (checked: {_names(pages)})" if pages else ""))

    def cm(d):
        return "x".join(f"{x:g}" for x in d) + " cm"

    pkg_d = [(o.source, o.dims_cm) for o in observations if o.dims_cm and o.dims_kind == "package"]
    item_d = [(o.source, o.dims_cm) for o in observations if o.dims_cm and o.dims_kind != "package"]
    use_d = pkg_d or item_d
    by_source = dict(use_d)
    entries = [(src, d[0] * d[1] * d[2], cm(d) + (" boxed" if pkg_d else "")) for src, d in use_d]
    d_status, vol, groups = _pick(entries, DIMS_AGREE)
    if d_status == "unverified" and pkg_d and _backed_by(vol, [d[0] * d[1] * d[2] for _, d in item_d], 0.3):
        d_status = "verified"
    no_dims = [p for p in pages if p not in {o.source for o in observations if o.dims_cm}]
    dims = None
    if vol is not None:
        dims = min((by_source[src] for src, _, _ in groups[0]),
                   key=lambda d: abs(d[0] * d[1] * d[2] - vol))
        if not pkg_d:
            msgs.append("box size unknown - using the product's own dimensions")
        if d_status == "majority" or no_dims:
            confirm["dims"] = _describe("Size", "a size", cm(dims), groups, no_dims)
    elif d_status == "conflict":
        msgs.append(_split("size", "a size", groups, no_dims))
    else:
        msgs.append("no dimensions found" + (f" (checked: {_names(pages)})" if pages else ""))

    if w_status == "missing":
        status = "missing"
    elif "conflict" in (w_status, d_status):
        status = "conflict"
    elif "majority" in (w_status, d_status):
        status = "majority"
    elif w_status == "verified" and d_status in ("verified", "missing"):
        status = "verified"
    else:
        status = "unverified"
    return Consensus(status, weight, dims, w_status, d_status, msgs, confirm)
