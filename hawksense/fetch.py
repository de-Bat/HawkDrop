"""Fetch a product page and extract its current price.

Extraction order:
1. a custom regex set on the offer (first capture group is the price),
2. schema.org ``Product`` JSON-LD (used by most modern shops, incl. many Israeli ones),
3. ``product:price:amount`` / ``og:price:amount`` meta tags and ``itemprop="price"`` microdata,
4. a few store-specific patterns (Amazon, eBay).

eBay listings and searches go through ``hawksense.ebay`` (official API when configured).

Sites that block bots can always be tracked with manual price entries.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from hawksense import netguard

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

CURRENCY_SYMBOLS = {"₪": "ILS", "$": "USD", "€": "EUR", "£": "GBP", "¥": "CNY", "NIS": "ILS", "ש\"ח": "ILS"}


class FetchError(Exception):
    pass


@dataclass
class Extraction:
    price: float
    currency: str | None
    in_stock: bool = True
    shipping: float | None = None
    title: str | None = None
    method: str = ""
    url: str | None = None  # the listing actually priced (e.g. the cheapest eBay search result)
    specs: object | None = None  # hawksense.specs.Specs read from the same page, if any
    image: str | None = None  # a picture of the product, if the page named one
    condition: str | None = None  # "Renewed", "Pre-owned"... only when the listing is not new


def fetch_html(url: str, timeout: float = 20.0, extra_headers: dict | None = None) -> str:
    """Fetch a page from the public internet (see ``hawksense.netguard`` for what's refused)."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        **(extra_headers or {}),
    }
    try:
        raw, charset = netguard.fetch(url, headers, timeout)
    except netguard.BlockedAddress as exc:
        raise FetchError(f"won't fetch {url}: {exc}") from None
    except Exception as exc:  # urllib raises many types; surface one
        raise FetchError(f"could not fetch {url}: {exc}") from exc
    return raw.decode(charset, errors="replace")


def parse_number(text: str) -> float | None:
    """Parse '1,299.90', '1.299,90', '₪ 1299', '$49' ..."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = re.sub(r"[^\d.,]", "", str(text))
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # 1.299,90
            s = s.replace(".", "").replace(",", ".")
        else:  # 1,299.90
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = s.replace(",", "") if len(tail) == 3 else head.replace(",", "") + "." + tail
    try:
        return float(s)
    except ValueError:
        return None


def guess_currency(text: str) -> str | None:
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in text:
            return code
    return None


# ---- JSON-LD -----------------------------------------------------------------

# Pages come from anywhere, so parsing must stay linear in the page size: no ".*?" scans from
# every opening tag (a hostile page with thousands of unclosed tags would take hours).
# the "+" is sometimes written as an entity: type="application/ld&#x2B;json" (Zap)
_LD_OPEN = re.compile(r'<script\b[^>]{0,300}?type=["\']application/ld(?:\+|&#x2b;|&#43;|&plus;)json["\'][^>]{0,300}>',
                      re.I)


def ld_json_blocks(page: str):
    """The contents of each <script type="application/ld+json"> block."""
    lower = page.lower()
    pos = 0
    while m := _LD_OPEN.search(page, pos):
        end = lower.find("</script", m.end())
        if end < 0:
            return  # unclosed: nothing after it can be closed either
        yield page[m.end():end]
        pos = end


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _is_type(node: dict, name: str) -> bool:
    t = node.get("@type")
    return t == name or (isinstance(t, list) and name in t)


def _image_of(node: dict) -> str | None:
    img = node.get("image")
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, dict):
        img = img.get("url") or img.get("contentUrl")
    if isinstance(img, str) and img.strip():
        return img.strip()
    return None


def _from_offer(offer: dict) -> Extraction | None:
    price = offer.get("price")
    if price is None and "priceSpecification" in offer:
        spec = offer["priceSpecification"]
        spec = spec[0] if isinstance(spec, list) and spec else spec
        if isinstance(spec, dict):
            price = spec.get("price")
            offer = {**offer, "priceCurrency": offer.get("priceCurrency") or spec.get("priceCurrency")}
    if price is None:
        price = offer.get("lowPrice")  # AggregateOffer
    value = parse_number(price)
    if value is None or value <= 0:
        return None
    availability = str(offer.get("availability", ""))
    in_stock = not re.search(r"OutOfStock|SoldOut|Discontinued", availability, re.I)
    shipping = None
    details = offer.get("shippingDetails")
    details = details[0] if isinstance(details, list) and details else details
    if isinstance(details, dict):
        rate = details.get("shippingRate")
        if isinstance(rate, dict):
            shipping = parse_number(rate.get("value"))
    return Extraction(value, offer.get("priceCurrency"), in_stock, shipping, method="json-ld")


def extract_json_ld(page: str) -> Extraction | None:
    for block in ld_json_blocks(page):
        try:
            data = json.loads(html_lib.unescape(block.strip()))
        except json.JSONDecodeError:
            continue
        for node in _walk(data):
            if not _is_type(node, "Product"):
                continue
            offers = node.get("offers")
            candidates = offers if isinstance(offers, list) else [offers]
            found = [x for x in (_from_offer(o) for o in candidates if isinstance(o, dict)) if x]
            if found:
                best = min(found, key=lambda x: (not x.in_stock, x.price))
                best.title = node.get("name")
                best.image = _image_of(node)
                return best
    return None


# ---- meta / microdata ----------------------------------------------------------

def _meta(page: str, attr: str, name: str) -> str | None:
    for pattern in (
        rf'<meta[^>]+{attr}=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+{attr}=["\']{re.escape(name)}["\']',
    ):
        m = re.search(pattern, page, re.I)
        if m:
            return html_lib.unescape(m.group(1))
    return None


def extract_meta(page: str) -> Extraction | None:
    for name in ("product:price:amount", "og:price:amount"):
        amount = _meta(page, "property", name)
        if amount and (value := parse_number(amount)):
            currency = _meta(page, "property", name.replace("amount", "currency"))
            return Extraction(value, currency, method="meta", image=_meta(page, "property", "og:image"))
    m = re.search(r'itemprop=["\']price["\'][^>]*content=["\']([^"\']+)["\']', page, re.I) or \
        re.search(r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']price["\']', page, re.I)
    if m and (value := parse_number(m.group(1))):
        cur = re.search(r'itemprop=["\']priceCurrency["\'][^>]*content=["\']([A-Z]{3})["\']', page, re.I)
        return Extraction(value, cur.group(1) if cur else None, method="microdata",
                          image=_meta(page, "property", "og:image"))
    return None


# ---- store specific ------------------------------------------------------------

# host part -> [(marker, pattern matched within STORE_WINDOW characters after the marker)]
_STORE_PATTERNS = {
    "amazon.": [
        ('id="corePrice', r'<span class="a-offscreen">([^<]{1,40})</span>'),
        ('<span class="a-price', r'^[^"]{0,200}"[^>]{0,300}><span class="a-offscreen">([^<]{1,40})</span>'),
    ],
    "newegg.": [  # $<strong>220</strong><sup>.00</sup>; the first one is the item's buy box
        ('class="price-current',
         r'^[^>]{0,100}>(?:\s*<span[^>]{0,100}></span>)?\s*([$]?)\s*<strong>([\d,]{1,12})</strong>\s*<sup>(\.\d{1,2})?</sup>'),
    ],
    "ebay.": [
        ('<div class="x-price-primary"', r'<span class="ux-textspans">([^<]{1,40})</span>'),
        ('itemprop="price"', r'^[^>]{0,300}>([^<]{1,40})</span>'),
    ],
}
STORE_WINDOW = 5000


def extract_store_specific(page: str, url: str) -> Extraction | None:
    host = (urlparse(url).hostname or "").lower()
    for key, rules in _STORE_PATTERNS.items():
        if key not in host:
            continue
        for marker, pattern in rules:
            i, tries = page.find(marker), 0
            while i >= 0 and tries < 20:  # the first few occurrences are enough
                start = i + len(marker)
                m = re.search(pattern, page[start:start + STORE_WINDOW], re.S)
                text = "".join(g for g in m.groups() if g) if m else ""  # "$" + "220" + ".00"
                if m and (value := parse_number(text)):
                    return Extraction(value, guess_currency(text), method=f"{key.rstrip('.')}-pattern")
                i, tries = page.find(marker, start), tries + 1
    return None


_BLOCK_WORDS = r"captcha|robot check|access denied|just a moment|attention required|pardon our interruption"
_BLOCK_TITLE = re.compile(r"<title[^>]{0,100}>[^<]{0,200}?(?:" + _BLOCK_WORDS + ")", re.I)


def _blocked(page: str) -> bool:
    """A bot-check page, not a product page that merely loads a captcha script for its forms."""
    return bool(_BLOCK_TITLE.search(page) or (len(page) < 20_000 and re.search(_BLOCK_WORDS, page, re.I)))


def extract_price(page: str, url: str = "", price_regex: str | None = None) -> Extraction:
    if price_regex:
        m = re.search(price_regex, page, re.S)
        if m and (value := parse_number(m.group(1))):
            return Extraction(value, guess_currency(m.group(0)), method="custom-regex", image=_meta(page, "property", "og:image"))
    for extractor in (extract_json_ld, extract_meta):
        result = extractor(page)
        if result:
            if not result.image:
                result.image = _meta(page, "property", "og:image")
            return result
    result = extract_store_specific(page, url)
    if result:
        result.image = _meta(page, "property", "og:image")
        return result
    if _blocked(page):
        raise FetchError("the store blocked the request (captcha) - record the price manually with `hawksense price`")
    raise FetchError("no price found on the page - add --regex to the offer or record the price manually")


def fetch_price(url: str, price_regex: str | None = None) -> Extraction:
    from hawksense.specs import extract_specs

    page = fetch_html(url)
    ex = extract_price(page, url, price_regex)
    ex.specs = extract_specs(page)
    return ex
