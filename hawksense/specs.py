"""Shipping weight and box size, read from product pages and cross-checked.

Forwarders charge by weight (or volumetric weight), so HawkSense tries to learn
an item's size from the store pages it already checks:

1. schema.org ``Product`` JSON-LD: ``weight``, ``width``/``height``/``depth`` and
   ``additionalProperty`` entries,
2. spec tables and lists ("Item Weight", "Package Dimensions", "משקל", "מידות" ...).

Every page is one observation. ``consensus`` compares them: two or more sources
that agree (within 15%) make a *verified* value; a single source is
*unverified*; sources that disagree are a *conflict* (the larger value is used,
so shipping isn't underestimated) and nothing found is *missing*. Conflicts and
missing values are reported to the user. A weight or size the user set by hand
always wins.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import statistics
from dataclasses import dataclass, field

AGREE = 0.15  # relative spread for sources to count as agreeing
CONFLICT = 0.30  # relative spread above which sources disagree

_WEIGHT_UNITS = {
    "kg": 1.0, "kgs": 1.0, "kilogram": 1.0, "kilograms": 1.0, "ק\"ג": 1.0, "קג": 1.0, "קילו": 1.0,
    "g": 0.001, "gr": 0.001, "gram": 0.001, "grams": 0.001, "גרם": 0.001,
    "lb": 0.45359237, "lbs": 0.45359237, "pound": 0.45359237, "pounds": 0.45359237,
    "oz": 0.028349523, "ounce": 0.028349523, "ounces": 0.028349523,
}
_LENGTH_UNITS = {"cm": 1.0, "ס\"מ": 1.0, "mm": 0.1, "מ\"מ": 0.1, "in": 2.54, '"': 2.54, "m": 100.0}
_UNIT_CODES = {"kgm": "kg", "grm": "g", "lbr": "lb", "onz": "oz", "cmt": "cm", "mmt": "mm", "inh": "in", "mtr": "m"}

_NUM = r"(\d+(?:[.,]\d+)?)"
_WEIGHT_RE = re.compile(_NUM + r"\s*(kilograms?|kgs?|grams?|gr|g|pounds?|lbs?|ounces?|oz|ק\"ג|קג|קילו|גרם)(?![a-z])",
                        re.I)
_DIMS_RE = re.compile(_NUM + r"\s*(?:cm|mm|in|\")?\s*[x×*]\s*" + _NUM + r"\s*(?:cm|mm|in|\")?\s*[x×*]\s*" + _NUM
                      + r"\s*(centimet(?:er|re)s?|cm|millimet(?:er|re)s?|mm|inch(?:es)?|in|\"|ס\"מ|מ\"מ)?", re.I)

# Whole-label patterns (after lower-casing and dropping "(kg)"-style hints) -> kind.
# Package/shipping values are what a forwarder weighs; "item" is the product alone.
_WEIGHT_LABELS = [
    (re.compile(r"(?:item |product )?(?:shipping|package|parcel|boxed|gross) weight|משקל (?:ה?אריזה|משלוח|ברוטו)"),
     "package"),
    (re.compile(r"(?:item |product |net |unit )?weight|משקל(?: ה?מוצר| נטו)?"), "item"),
]
_DIMS_LABELS = [
    (re.compile(r"(?:item )?(?:package|shipping|box|parcel|boxed) (?:dimensions|size)(?: l ?x ?w ?x ?h)?"
                r"|מידות (?:ה?אריזה|משלוח)"), "package"),
    (re.compile(r"(?:product |item |overall )?(?:dimensions|measurements|size)(?: l ?x ?w ?x ?h| w ?x ?h ?x ?d)?"
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


def extract_specs(page: str) -> Specs:
    """Weight and size found on a product page (empty ``Specs`` if none)."""
    ld = _from_json_ld(page)
    table = _from_tables(page)
    out = Specs(method="+".join(m for m, s in (("json-ld", ld), ("spec-table", table)) if s))
    for src in (ld, table):  # structured data first; package values beat item values
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

    @property
    def dims_text(self) -> str | None:
        return "x".join(f"{d:g}" for d in self.dims_cm) if self.dims_cm else None

    @property
    def alert(self) -> bool:
        return self.status in ("conflict", "missing")


PACKAGING_FACTOR, PACKAGING_KG = 1.1, 0.1  # boxed weight estimated from the product's own weight


def _agree(values: list[float]) -> tuple[str, float]:
    lo, hi, mid = min(values), max(values), statistics.median(values)
    spread = (hi - lo) / mid if mid else 0.0
    if len(values) == 1:
        return "unverified", values[0]
    if spread <= AGREE:
        return "verified", mid
    if spread <= CONFLICT:  # close enough: take the median but call it unverified
        return "unverified", mid
    return "conflict", hi


def _backed_by(package_value: float, item_values: list[float], low: float) -> bool:
    """An item value slightly below (or equal to) the boxed value corroborates it."""
    return any(low * package_value <= v <= package_value * (1 + AGREE) for v in item_values)


def consensus(observations: list[Observation]) -> Consensus:
    msgs: list[str] = []
    # weight: prefer package weights; fall back to item weight plus packaging
    pkg = [(o.source, o.weight_kg) for o in observations if o.weight_kg and o.weight_kind == "package"]
    item = [(o.source, o.weight_kg) for o in observations if o.weight_kg and o.weight_kind != "package"]
    use, estimated = (pkg, False) if pkg else (item, True)
    weight, w_status = None, "missing"
    if use:
        w_status, weight = _agree([v for _, v in use])
        if w_status == "unverified" and not estimated and _backed_by(weight, [v for _, v in item], 0.6):
            w_status = "verified"  # another page's product weight fits inside this boxed weight
        if estimated:
            weight = round(weight * PACKAGING_FACTOR + PACKAGING_KG, 3)
            msgs.append("only the product weight was found - added ~10% + 0.1 kg for the box")
        if w_status == "conflict":
            msgs.append("stores disagree on the weight: " + ", ".join(f"{s} {v:g} kg" for s, v in use)
                        + f" - using {max(v for _, v in use):g} kg")
    else:
        msgs.append("no weight found on any store page")

    pkg_d = [(o.source, o.dims_cm) for o in observations if o.dims_cm and o.dims_kind == "package"]
    item_d = [(o.source, o.dims_cm) for o in observations if o.dims_cm and o.dims_kind != "package"]
    use_d = pkg_d or item_d
    dims, d_status = None, "missing"
    if use_d:
        volumes = [d[0] * d[1] * d[2] for _, d in use_d]
        d_status, vol = _agree(volumes)
        if d_status == "unverified" and pkg_d and _backed_by(vol, [d[0] * d[1] * d[2] for _, d in item_d], 0.3):
            d_status = "verified"
        dims = min((d for _, d in use_d), key=lambda d: abs(d[0] * d[1] * d[2] - vol))
        if d_status == "conflict":
            dims = max((d for _, d in use_d), key=lambda d: d[0] * d[1] * d[2])
            msgs.append("stores disagree on the size: " + ", ".join(
                f"{s} {'x'.join(f'{x:g}' for x in d)} cm" for s, d in use_d))
        if not pkg_d:
            msgs.append("box size unknown - using the product's own dimensions")
    else:
        msgs.append("no dimensions found")

    if w_status == "missing":
        status = "missing"
    elif "conflict" in (w_status, d_status):
        status = "conflict"
    elif w_status == "verified" and d_status in ("verified", "missing"):
        status = "verified"
    else:
        status = "unverified"
    return Consensus(status, weight, dims, w_status, d_status, msgs)
