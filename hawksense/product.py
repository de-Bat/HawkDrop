"""Product details from a store page: title, image, description, condition and availability.

Each source fills only what the ones before it left empty:

1. schema.org ``Product`` JSON-LD (most shops: B&H, Best Buy, Shopify stores, many Israeli ones),
2. store-specific patterns for the big global stores (Amazon, eBay, AliExpress, Newegg),
3. OpenGraph / Twitter meta tags (``og:image``, ``og:description``, ``product:availability``...),
4. schema.org microdata (``itemprop="availability"``...),
5. the page ``<title>``.

Condition and availability are normalized to fixed values (``CONDITIONS``, ``AVAILABILITY``), so
"Renewed", "Seller refurbished" and ``schema.org/RefurbishedCondition`` all read as "refurbished".
Everything is bounded: pages come from anywhere.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
from dataclasses import dataclass, fields
from urllib.parse import urljoin, urlparse

CONDITIONS = ("new", "open_box", "refurbished", "used", "damaged", "for_parts")
AVAILABILITY = ("in_stock", "limited", "preorder", "backorder", "out_of_stock", "discontinued")
UNAVAILABLE = ("out_of_stock", "discontinued")  # can't be bought now

CONDITION_LABELS = {"new": "new", "open_box": "open box", "refurbished": "refurbished", "used": "used",
                    "damaged": "damaged", "for_parts": "for parts"}
AVAILABILITY_LABELS = {"in_stock": "in stock", "limited": "few left", "preorder": "pre-order",
                       "backorder": "back-order", "out_of_stock": "out of stock", "discontinued": "discontinued"}

TITLE_MAX, DESCRIPTION_MAX, URL_MAX = 300, 600, 1000


@dataclass
class Details:
    title: str | None = None
    image: str | None = None  # absolute http(s) URL of the main product image
    description: str | None = None
    condition: str | None = None  # one of CONDITIONS
    availability: str | None = None  # one of AVAILABILITY

    def fill(self, other: Details | None) -> Details:
        """Take ``other``'s values for the fields still empty here."""
        if other:
            for f in fields(self):
                if getattr(self, f.name) is None and getattr(other, f.name) is not None:
                    setattr(self, f.name, getattr(other, f.name))
        return self

    def __bool__(self) -> bool:
        return any(getattr(self, f.name) is not None for f in fields(self))


# ---- normalizing ---------------------------------------------------------------------------

_CONDITION_WORDS = [  # checked in order: "certified refurbished" before "new", "open box" before "new"
    (r"for parts|not working", "for_parts"),
    (r"damaged", "damaged"),
    (r"refurb|renewed|remanufactured|reconditioned", "refurbished"),
    (r"open[- ]?box|new other|like new|new \(other\)", "open_box"),
    (r"used|pre-?owned|second[- ]?hand|משומש", "used"),
    (r"\bnew\b|brand new|חדש", "new"),
]
_AVAILABILITY_WORDS = [
    (r"discontinued|no longer available|listing (?:has )?ended|this item is no longer", "discontinued"),
    (r"pre-?order|presale|pre-?sale|coming soon|not yet released", "preorder"),
    (r"back-?order|backorder|ships in \d+ ?(?:-|to) ?\d+ weeks|temporarily out of stock.*order now", "backorder"),
    (r"out of stock|sold out|currently unavailable|unavailable|not available|אזל", "out_of_stock"),
    (r"limited|only \d+ left|last one|few left|low stock|\b[1-5] available", "limited"),
    (r"in stock|instock|available|ships (?:today|tomorrow|in)|more than \d+ available|במלאי", "in_stock"),
]


def condition_of(text) -> str | None:
    """'https://schema.org/UsedCondition' -> 'used', 'Renewed' -> 'refurbished', '3000' (eBay) -> 'used'."""
    if text is None:
        return None
    text = str(text).strip()
    if text.isdigit():  # eBay condition ids
        n = int(text)
        return ("new" if n == 1000 else "open_box" if n in (1500, 1750) else
                "refurbished" if 2000 <= n < 3000 else "used" if 3000 <= n < 7000 else
                "for_parts" if n == 7000 else None)
    low = text.lower()
    schema = low.rsplit("/", 1)[-1]  # schema.org: NewCondition, UsedCondition, RefurbishedCondition...
    if schema.endswith("condition") and schema[:-9] in ("new", "used", "refurbished", "damaged"):
        return schema[:-9]
    for pattern, value in _CONDITION_WORDS:
        if re.search(pattern, low):
            return value
    return None


def availability_of(text) -> str | None:
    """'https://schema.org/PreOrder' -> 'preorder', 'Only 3 left in stock.' -> 'limited'."""
    if text is None:
        return None
    low = re.sub(r"\s+", " ", str(text)).strip().lower()[:300]
    schema = low.rsplit("/", 1)[-1]  # schema.org URLs and bare enum names
    enums = {"instock": "in_stock", "onlineonly": "in_stock", "instoreonly": "in_stock",
             "limitedavailability": "limited", "preorder": "preorder", "presale": "preorder",
             "backorder": "backorder", "outofstock": "out_of_stock", "soldout": "out_of_stock",
             "discontinued": "discontinued", "in_stock": "in_stock", "limited_stock": "limited",
             "out_of_stock": "out_of_stock"}
    if schema in enums:
        return enums[schema]
    for pattern, value in _AVAILABILITY_WORDS:
        if re.search(pattern, low):
            return value
    return None


_TAG = re.compile(r"<[^>]{0,2000}>")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def clean_text(text, limit: int) -> str | None:
    """Plain text: tags dropped, entities decoded, whitespace collapsed, cut at a word boundary."""
    if not isinstance(text, str):
        return None
    text = html_lib.unescape(_TAG.sub(" ", text[:limit * 20]))
    text = re.sub(r"\s+", " ", _CONTROL.sub("", text)).strip()  # no terminal escape codes from a hostile page
    if not text:
        return None
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
    return text


def clean_image(url, base: str = "") -> str | None:
    """An absolute http(s) image URL, or None."""
    if isinstance(url, dict):
        url = url.get("url") or url.get("contentUrl")
    if isinstance(url, list):
        url = next((u for u in (clean_image(x, base) for x in url[:10]) if u), None)
    if not isinstance(url, str) or not url.strip() or len(url) > URL_MAX:
        return None
    url = html_lib.unescape(url.strip())
    if url.startswith("//"):
        url = "https:" + url
    url = urljoin(base, url) if base else url
    if inner := re.match(r"https?://[^/]+/+(https?://.+)$", url):  # "https://shop/https://cdn/x.jpg" (a site bug)
        url = inner.group(1)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return url


_SITE_SUFFIX = re.compile(r"\s*[|\-–—:]\s*(?:[A-Z][\w.&' ]{1,30}\.(?:com|co\.uk|de|co\.il)|Newegg\.com|Amazon\.[\w.]+"
                          r"|eBay|AliExpress|B&H Photo(?: Video)?)\s*$", re.I)


def clean_title(text) -> str | None:
    title = clean_text(text, TITLE_MAX)
    if title:
        title = re.sub(r"^Amazon\.[\w.]+\s*:\s*", "", title)  # "Amazon.com : Sony WH-1000XM5 : Electronics"
        title = re.sub(r"\s*:\s*(?:Electronics|Computers|Home & Kitchen|Toys & Games|Everything Else)$", "", title)
        title = _SITE_SUFFIX.sub("", title).strip() or None
    return title


# ---- generic sources -------------------------------------------------------------------------

_LD_OPEN = re.compile(r'<script\b[^>]{0,300}?type=["\']application/ld(?:\+|&#x2b;|&#43;|&plus;)json["\'][^>]{0,300}>',
                      re.I)
LD_MAX = 500_000


def _ld_nodes(page: str):
    lower, pos = page.lower(), 0
    while m := _LD_OPEN.search(page, pos):
        end = lower.find("</script", m.end(), m.end() + LD_MAX)
        if end < 0:
            return
        pos = end
        try:
            data = json.loads(html_lib.unescape(page[m.end():end].strip()))
        except (json.JSONDecodeError, RecursionError):
            continue
        stack, seen = [data], 0
        while stack and seen < 2000:
            node = stack.pop()
            seen += 1
            if isinstance(node, dict):
                yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)


def _is(node: dict, name: str) -> bool:
    t = node.get("@type")
    return t == name or (isinstance(t, list) and name in t)


def from_json_ld(page: str, url: str = "") -> Details:
    for node in _ld_nodes(page):
        if not (_is(node, "Product") or _is(node, "ProductGroup")):
            continue
        d = Details(clean_title(node.get("name")), clean_image(node.get("image"), url),
                    clean_text(node.get("description"), DESCRIPTION_MAX), condition_of(node.get("itemCondition")))
        offers = node.get("offers")
        for offer in (offers if isinstance(offers, list) else [offers])[:50]:
            if isinstance(offer, dict):
                d.condition = d.condition or condition_of(offer.get("itemCondition"))
                d.availability = d.availability or availability_of(offer.get("availability"))
        if d:
            return d
    return Details()


_META_OPEN = re.compile(r"<meta\b[^>]{0,20000}>", re.I)
_ATTR = re.compile(r'([\w:-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')


def _metas(page: str) -> dict[str, str]:
    """{property/name/itemprop: content} of the page's <meta> tags (first one wins)."""
    out: dict[str, str] = {}
    for m in _META_OPEN.finditer(page[:300_000]):  # meta tags live in <head>
        attrs = {k.lower(): (a if a is not None else b) for k, a, b in _ATTR.findall(m.group(0))}
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        if key and "content" in attrs and key not in out:
            out[key] = html_lib.unescape(attrs["content"])
    return out


def _meta(page: str, key: str) -> str | None:
    return _metas(page).get(key.lower())


def from_meta_tags(page: str, url: str = "") -> Details:
    m = _metas(page)
    return Details(
        clean_title(m.get("og:title") or m.get("twitter:title")),
        clean_image(m.get("og:image:secure_url") or m.get("og:image") or m.get("twitter:image"), url),
        clean_text(m.get("og:description") or m.get("description") or m.get("twitter:description"), DESCRIPTION_MAX),
        condition_of(m.get("product:condition") or m.get("og:condition")),
        availability_of(m.get("product:availability") or m.get("og:availability")),
    )


def _itemprop(page: str, prop: str) -> str | None:
    """Value of the first itemprop: its content/href/src attribute, else its text."""
    m = re.search(r'<(\w+)\b([^>]{0,500}?)itemprop=["\']' + prop + r'["\']([^>]{0,500})>', page, re.I)
    if not m:
        return None
    attrs = m.group(2) + m.group(3)
    a = re.search(r'\b(?:content|href|src)=["\']([^"\']{1,2000})["\']', attrs, re.I)
    if a:
        return html_lib.unescape(a.group(1))
    end = page.find(f"</{m.group(1)}", m.end(), m.end() + 5000)
    return page[m.end():end] if end > 0 else None


def from_microdata(page: str, url: str = "") -> Details:
    return Details(None, clean_image(_itemprop(page, "image"), url),
                   clean_text(_itemprop(page, "description"), DESCRIPTION_MAX),
                   condition_of(_itemprop(page, "itemCondition")),
                   availability_of(_itemprop(page, "availability")))


def from_title_tag(page: str) -> Details:
    m = re.search(r"<title\b[^>]{0,200}>([^<]{1,1000})</title>", page[:300_000], re.I)
    return Details(clean_title(m.group(1)) if m else None)


# ---- global stores ---------------------------------------------------------------------------

def _after(page: str, marker: str, pattern: str, window: int = 5000, flags=re.S | re.I) -> str | None:
    """First group of ``pattern`` within ``window`` characters after ``marker``."""
    i = page.find(marker)
    if i < 0:
        return None
    m = re.search(pattern, page[i + len(marker):i + len(marker) + window], flags)
    return m.group(1) if m else None


def _list_items(fragment: str | None, limit: int = 6) -> str | None:
    if not fragment:
        return None
    items = [clean_text(li, 200) for li in re.findall(r"<li\b[^>]{0,300}>(.{0,2000}?)</li>", fragment, re.S)]
    return clean_text(" · ".join(i for i in items[:limit] if i), DESCRIPTION_MAX)


def _amazon(page: str, url: str) -> Details:
    title = _after(page, 'id="productTitle"', r"^[^>]{0,300}>(.{1,1000}?)</span>")
    tag = re.search(r'<img\b[^>]{0,5000}?id="landingImage"[^>]{0,5000}>', page)
    attr = (re.search(r'data-old-hires="([^"]{1,1000})"', tag.group(0)) or re.search(r'\bsrc="([^"]{1,1000})"',
                                                                                    tag.group(0))) if tag else None
    image = attr.group(1) if attr else _after(page, '"hiRes":"', r'^([^"]{1,1000})"', 1100)
    availability = _after(page, 'id="availability"', r"<span[^>]{0,300}>(.{1,500}?)</span>", 2000)
    bullets = _after(page, 'id="feature-bullets"', r"(<ul\b.{0,20000}?</ul>)", 25000)
    title_text = clean_title(title)
    renewed = bool(re.search(r"\(Renewed(?: Premium)?\)|/Renewed", (title or "") + url, re.I))
    used_only = 'id="usedOnlyBuybox"' in page  # the buy box offers only used copies
    return Details(title_text, clean_image(image, url), _list_items(bullets),
                   "refurbished" if renewed else "used" if used_only else "new" if title_text else None,
                   availability_of(clean_text(availability, 300)) if availability else None)


def _ebay(page: str, url: str) -> Details:
    title = _after(page, 'class="x-item-title__mainTitle"', r"<span[^>]{0,300}>(.{1,1000}?)</span>", 2000)
    image = (_after(page, 'class="ux-image-carousel-item', r'data-zoom-src="([^"]{1,1000})"', 3000)
             or _after(page, 'class="ux-image-carousel-item', r'\bsrc="([^"]{1,1000})"', 3000))
    condition = (_after(page, 'class="x-item-condition-text', r'class="ux-textspans"[^>]{0,200}>([^<]{1,200})<', 2000)
                 or _after(page, 'data-testid="x-item-condition', r'class="ux-textspans"[^>]{0,200}>([^<]{1,200})<',
                           3000))
    availability = (_after(page, 'id="qtyAvailability"', r'class="ux-textspans[^"]*"[^>]{0,200}>([^<]{1,200})<', 2000)
                    or _after(page, 'class="d-quantity__availability', r'class="ux-textspans[^"]*"[^>]{0,200}>'
                              r'([^<]{1,200})<', 2000))
    if re.search(r"This listing (?:was ended|has ended)|The seller has ended this listing", page[:400_000]):
        availability = "listing ended"
    return Details(clean_title(title), clean_image(image, url), None, condition_of(condition),
                   availability_of(availability))


def _aliexpress(page: str, url: str) -> Details:
    title = _after(page, '"subject":"', r'^([^"]{1,1000})"', 1100)
    image = _after(page, '"imagePathList":[', r'^"([^"]{1,1000})"', 1100)
    qty = _after(page, '"totalAvailQuantity":', r"^(\d{1,9})", 12)
    availability = None if qty is None else ("out_of_stock" if qty == "0" else
                                             "limited" if int(qty) <= 5 else "in_stock")
    if title and "\\" in title:
        try:
            title = json.loads(f'"{title}"')  # \u0026 and friends
        except json.JSONDecodeError:
            pass
    return Details(clean_title(title),
                   clean_image(image, url), None, "new" if title else None, availability)


def _newegg(page: str, url: str) -> Details:
    bullets = _after(page, 'class="product-bullets"', r"(<ul\b.{0,20000}?</ul>)", 21000)
    # the item's own record in the page data: {"Item":"19-113-844",...,"FinalPrice":220,"Instock":true,...}
    stock = re.search(r'"FinalPrice":[\d.]{1,12},"Instock":(true|false)', page)
    refurb = re.search(r'"IsRefurbished":(true|false)', page)
    title = clean_title(_meta(page[:300_000], "og:title"))
    condition = ("refurbished" if refurb and refurb.group(1) == "true" or re.search(r"refurbished", title or "", re.I)
                 else "open_box" if re.search(r"open box", title or "", re.I) else "new" if title else None)
    return Details(title, None, _list_items(bullets), condition,
                   None if not stock else "in_stock" if stock.group(1) == "true" else "out_of_stock")


_STORES = {"amazon.": _amazon, "ebay.": _ebay, "aliexpress.": _aliexpress, "newegg.": _newegg}


def from_store(page: str, url: str) -> Details:
    host = (urlparse(url).hostname or "").lower()
    for key, fn in _STORES.items():
        if key in host:
            return fn(page, url)
    return Details()


def extract_details(page: str, url: str = "") -> Details:
    """Everything the page says about the product (fields it doesn't say stay None)."""
    d = Details()
    for source in (from_json_ld(page, url), from_store(page, url), from_meta_tags(page, url),
                   from_microdata(page, url), from_title_tag(page)):
        d.fill(source)
        if all(getattr(d, f.name) is not None for f in fields(d)):
            break
    return d


# ---- images ----------------------------------------------------------------------------------

IMAGE_MAX = 2 * 1024 * 1024
_IMAGE_TYPES = [(b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"GIF87a", "image/gif"),
                (b"GIF89a", "image/gif")]


def image_type(data: bytes) -> str | None:
    """The image type from the file's own bytes (never trust the server's header; no SVG: it can run script)."""
    for magic, mime in _IMAGE_TYPES:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return None


def image_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
