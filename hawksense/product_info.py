"""Product details read from a store page: title, description, picture, stock status and condition.

The price comes from ``hawksense.fetch.extract_price``; this fills in everything around it, trying the most
precise source first: the store's own markup (Amazon, eBay, Newegg, AliExpress), then schema.org JSON-LD,
then Open Graph / microdata tags. Each field is only filled when still empty, so a better source wins.

Pages come from anywhere, so every scan is bounded (a fixed window after a marker), never a ``.*?`` from
each opening tag.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from urllib.parse import urlparse

from hawksense.fetch import Extraction, _is_type, _image_of, _meta, _walk, ld_json_blocks

TITLE_MAX = 200
DESCRIPTION_MAX = 400
WINDOW = 12_000

_TAGS = re.compile(r"<[^>]+>")
_SCRIPTS = re.compile(r"<(script|style)\b[^>]{0,300}>.{0,20000}?</\1>", re.S | re.I)

# ---- normalising ----------------------------------------------------------------

_OUT = re.compile(
    r"out of stock|currently unavailable|temporarily unavailable|sold out|no longer available|discontinued"
    r"|this listing (?:has |was )?ended|listing sold|not available|unavailable"
    r"|nicht verf[üu]gbar|derzeit nicht|ausverkauft|[ée]puis[ée]|indisponible|agotado|no disponible|esaurito"
    r"|OutOfStock|SoldOut|Discontinued", re.I)
_IN = re.compile(
    r"in stock|only \d+ left|\d+ available|more than \d+ available|available|ships (?:in|within)|pre-?order"
    r"|auf lager|vorr[äa]tig|en stock|disponible|disponibile|InStock|LimitedAvailability|PreOrder|OnlineOnly", re.I)

_CONDITIONS = [  # (pattern, label); "new" is not a condition worth showing
    (re.compile(r"for parts|not working", re.I), "For parts"),
    (re.compile(r"renewed premium", re.I), "Renewed Premium"),
    (re.compile(r"renewed", re.I), "Renewed"),
    (re.compile(r"certified refurbished|manufacturer refurbished", re.I), "Certified refurbished"),
    (re.compile(r"seller refurbished", re.I), "Seller refurbished"),
    (re.compile(r"refurbished|refurb|generalüberholt|reconditionn[ée]|reacondicionado|ricondizionato", re.I),
     "Refurbished"),
    (re.compile(r"open[- ]box", re.I), "Open box"),
    (re.compile(r"used\s*[-–:]\s*(like new|very good|good|acceptable)", re.I), None),  # "Used - Like New"
    (re.compile(r"pre-?owned", re.I), "Pre-owned"),
    (re.compile(r"\bused\b|gebraucht|d['’]occasion|usado|usato", re.I), "Used"),
    (re.compile(r"damaged", re.I), "Damaged"),
]


def text_of(fragment: str) -> str:
    """Visible text of an HTML fragment, whitespace collapsed."""
    fragment = re.sub(r"<[^>]*$", "", _SCRIPTS.sub(" ", fragment))  # a tag cut off by the scan window
    return re.sub(r"\s+", " ", html_lib.unescape(_TAGS.sub(" ", fragment))).replace("‎", "").strip()


def condition_label(text: str | None) -> str | None:
    """'Used - Like New' / 'Pre-owned' / 'Renewed'...; None for new (or unknown)."""
    if not text:
        return None
    text = str(text).rsplit("/", 1)[-1]  # schema.org URLs: .../RefurbishedCondition
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)  # "RefurbishedCondition" -> "Refurbished Condition"
    if re.fullmatch(r"\s*(?:brand )?new(?: condition)?(?: with (?:box|tags))?\s*", text, re.I):
        return None
    for pattern, label in _CONDITIONS:
        if m := pattern.search(text):
            return label or f"Used - {m.group(1).capitalize()}"
    return None


def in_stock_from(text: str | None) -> bool | None:
    """True/False when the wording says so, None when it says neither."""
    if not text:
        return None
    if _OUT.search(text):
        return False
    return True if _IN.search(text) else None


def _clip(text: str | None, limit: int) -> str | None:
    text = re.sub(r"\s+", " ", html_lib.unescape(text or "")).strip()
    if not text:
        return None
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
    return text


_SITE_SUFFIX = re.compile(r"\s*(?:[-|:–]\s*)?(?:Newegg\.com|eBay|AliExpress|B&H Photo(?: Video)?|Best Buy)\s*$", re.I)
_AMAZON_TITLE = re.compile(r"^Amazon\.[a-z.]+\s*:\s*|\s*:\s*Amazon\.[a-z.]+\s*:.*$|\s*:\s*[^:]{1,40}$", re.I)


def clean_title(title: str | None, host: str = "") -> str | None:
    title = _clip(title, 400)
    if not title:
        return None
    title = _SITE_SUFFIX.sub("", title)
    if "amazon." in host:
        title = _AMAZON_TITLE.sub("", title)
    return _clip(title, TITLE_MAX)


# ---- sources ----------------------------------------------------------------------

def _after(page: str, marker: str, size: int = WINDOW) -> str | None:
    i = page.find(marker)
    return page[i + len(marker):i + len(marker) + size] if i >= 0 else None


def _from_json_ld(page: str) -> dict:
    out: dict = {}
    for block in ld_json_blocks(page):
        try:
            data = json.loads(html_lib.unescape(block.strip()))
        except json.JSONDecodeError:
            continue
        for node in _walk(data):
            if not (_is_type(node, "Product") or _is_type(node, "ProductGroup")):
                continue
            out.setdefault("title", node.get("name"))
            if isinstance(node.get("description"), str):
                out.setdefault("description", node["description"])
            if img := _image_of(node):
                out.setdefault("image", img)
            offers = node.get("offers")
            for offer in offers if isinstance(offers, list) else [offers]:
                if not isinstance(offer, dict):
                    continue
                if offer.get("itemCondition"):
                    out.setdefault("condition", condition_label(str(offer["itemCondition"])) or "")
                if offer.get("availability"):
                    out.setdefault("availability", str(offer["availability"]).rsplit("/", 1)[-1])
            if node.get("itemCondition"):
                out.setdefault("condition", condition_label(str(node["itemCondition"])) or "")
            if out:
                return out
    return out


def _from_meta(page: str) -> dict:
    out = {"title": _meta(page, "property", "og:title"),
           "description": _meta(page, "property", "og:description") or _meta(page, "name", "description"),
           "image": _meta(page, "property", "og:image")}
    avail = _meta(page, "property", "product:availability") or _meta(page, "property", "og:availability")
    if not avail and (m := re.search(r'itemprop=["\']availability["\'][^>]{0,200}?(?:href|content)=["\']([^"\']+)',
                                     page, re.I)):
        avail = m.group(1)
    if avail:
        out["availability"] = avail.rsplit("/", 1)[-1]
    cond = _meta(page, "property", "product:condition") or _meta(page, "property", "og:condition")
    if not cond and (m := re.search(r'itemprop=["\']itemCondition["\'][^>]{0,200}?(?:href|content)=["\']([^"\']+)',
                                    page, re.I)):
        cond = m.group(1)
    if cond:
        out["condition"] = condition_label(cond) or ""
    return {k: v for k, v in out.items() if v is not None}


# -- Amazon

_AMZ_BULLET = re.compile(r'<li\b[^>]{0,200}>\s*<span class="a-list-item[^"]*">(.{1,1500}?)</span>', re.S)
_AMZ_HIRES = re.compile(r'"hiRes":"(https://[^"]{10,300})"')
_AMZ_DYN = re.compile(r'id="landingImage"[^>]{0,3000}?data-a-dynamic-image="\{&quot;(https://[^&"]{10,300})&quot;', re.S)


def _amazon(page: str) -> dict:
    out: dict = {}
    if seg := _after(page, 'id="productTitle"', 2000):
        out["title"] = text_of(seg.split("</span>", 1)[0].split(">", 1)[-1])
    if seg := _after(page, 'id="feature-bullets"', 20_000):
        bullets = [text_of(b) for b in _AMZ_BULLET.findall(seg.split('id="productOverview', 1)[0])]
        bullets = [b for b in bullets if len(b) > 15 and not b.lower().startswith(("make sure this fits", "›"))]
        if bullets:
            out["description"] = " · ".join(bullets[:3])
    if "description" not in out and (seg := _after(page, 'id="productDescription"', 4000)):
        out["description"] = text_of(seg.split("</div>", 1)[0].split(">", 1)[-1])
    if m := _AMZ_HIRES.search(page) or _AMZ_DYN.search(page):
        out["image"] = html_lib.unescape(m.group(1))
    if 'id="outOfStock"' in page:
        out["availability"] = "Currently unavailable"
    elif seg := _after(page, '<div id="availability"', 4000):
        seg = seg.split(">", 1)[-1]
        span = re.search(r"<span\b[^>]{0,300}>(.{1,600}?)</span>", seg, re.S)  # the wording sits in the first span
        words = text_of(span.group(1) if span else seg.split("</div>", 1)[0])
        out["availability"] = re.split(r"(?<=[.!])\s|\s{2,}|Quantity:|\.[a-z#]", words)[0].strip(" .") or None
    title = out.get("title") or ""
    if re.search(r"\((?:renewed|refurbished)", title, re.I) or "condition Amazon Renewed" in page:
        out["condition"] = "Renewed Premium" if "Renewed Premium" in title else "Renewed"
    return {k: v for k, v in out.items() if v}


# -- eBay (item pages; the API path fills these from JSON already)

def _ebay(page: str) -> dict:
    out: dict = {}
    if seg := _after(page, 'class="x-item-title__mainTitle"', 1500):
        out["title"] = text_of(seg.split("</h1>", 1)[0].split(">", 1)[-1])
    for marker in ('class="x-item-condition-text"', 'data-testid="x-item-condition"',
                   'class="x-item-condition-value"'):
        if seg := _after(page, marker, 1500):
            words = text_of(seg.split(">", 1)[-1])[:120]
            out["condition"] = condition_label(words) or ""
            break
    if re.search(r"This listing (?:was|has) ended|This listing sold on|This item is out of stock", page):
        out["availability"] = "Listing ended"
    elif seg := _after(page, 'class="x-quantity__availability', 1500):
        out["availability"] = text_of(seg.split("</div>", 1)[0].split(">", 1)[-1])[:80] or None
    if m := re.search(r'<img[^>]{0,400}?src="(https://i\.ebayimg\.com/images/g/[^"]{5,200}/s-l\d+\.(?:jpg|webp|png))"',
                      page):
        out["image"] = re.sub(r"/s-l\d+\.", "/s-l1600.", m.group(1))
    return {k: v for k, v in out.items() if v is not None}


# -- Newegg

def _newegg(page: str) -> dict:
    out: dict = {}
    if seg := _after(page, '<h1 class="product-title"', 1000):
        out["title"] = text_of(seg.split("</h1>", 1)[0].split(">", 1)[-1])
    if seg := _after(page, '<div class="product-bullets">', 6000):
        bullets = [text_of(b) for b in re.findall(r"<li\b[^>]{0,100}>(.{1,1500}?)</li>", seg.split("</ul>", 1)[0], re.S)]
        if bullets:
            out["description"] = " · ".join(b for b in bullets[:3] if b)
    if re.search(r'"IsRefurbished":true', page):
        out["condition"] = "Refurbished"
    elif re.search(r'"IsOpenBoxed":true', page):
        out["condition"] = "Open box"
    elif re.search(r'"IsNew":true', page):
        out["condition"] = ""
    if m := re.search(r'"Instock":(true|false)', page):
        out["availability"] = "In stock" if m.group(1) == "true" else "Out of stock"
    return {k: v for k, v in out.items() if v is not None}


# -- AliExpress (the page data; often blocked for servers)

def _aliexpress(page: str) -> dict:
    out: dict = {}
    if m := re.search(r'"subject":"((?:[^"\\]|\\.){5,400})"', page):
        out["title"] = json.loads(f'"{m.group(1)}"')
    if m := re.search(r'"imagePathList":\["(https?:[^"]{5,300})"', page):
        out["image"] = m.group(1)
    if m := re.search(r'"totalAvailQuantity":(\d+)', page):
        out["availability"] = "Out of stock" if m.group(1) == "0" else f"{m.group(1)} available"
    return out


_STORES = {"amazon.": _amazon, "ebay.": _ebay, "newegg.": _newegg, "aliexpress.": _aliexpress}


def read_details(page: str, url: str = "") -> dict:
    """Every detail found, best source first (store markup, then JSON-LD, then meta tags)."""
    host = (urlparse(url).hostname or "").lower()
    sources = [fn(page) for key, fn in _STORES.items() if key in host]
    sources += [_from_json_ld(page), _from_meta(page)]
    out: dict = {}
    for src in sources:
        for key, value in src.items():
            if key not in out and value is not None:
                out[key] = value
    out["title"] = clean_title(out.get("title"), host)
    out["description"] = _clip(out.get("description"), DESCRIPTION_MAX)
    if out.get("availability"):
        out["availability"] = _humanize(out["availability"])
    return out


def _humanize(avail: str) -> str:
    """schema.org 'OutOfStock' -> 'Out of stock'; store wording is kept."""
    if re.fullmatch(r"[A-Z][a-zA-Z]+", avail):
        avail = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", avail).capitalize()
    return _clip(avail, 80) or avail


def enrich(ex: Extraction, page: str, url: str = "") -> Extraction:
    """Fill the extraction's missing title/description/image/condition, and its stock status."""
    found = read_details(page, url)
    ex.title = ex.title or found.get("title")
    ex.description = ex.description or found.get("description")
    ex.image = ex.image or found.get("image")
    if ex.condition is None and found.get("condition"):
        ex.condition = found["condition"]
    if found.get("availability"):
        ex.availability = found["availability"]
        stock = in_stock_from(found["availability"])
        if stock is not None:
            ex.in_stock = stock
    return ex
