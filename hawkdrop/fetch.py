"""Fetch a product page and extract its current price.

Extraction order:
1. a custom regex set on the offer (first capture group is the price),
2. schema.org ``Product`` JSON-LD (used by most modern shops, incl. many Israeli ones),
3. ``product:price:amount`` / ``og:price:amount`` meta tags and ``itemprop="price"`` microdata,
4. a few store-specific patterns (Amazon, eBay).

eBay listings and searches go through ``hawkdrop.ebay`` (official API when configured).

Sites that block bots can always be tracked with manual price entries.
"""

from __future__ import annotations

import gzip
import html as html_lib
import json
import re
import urllib.request
import zlib
from dataclasses import dataclass

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
    specs: object | None = None  # hawkdrop.specs.Specs read from the same page, if any


def fetch_html(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            charset = resp.headers.get_content_charset() or "utf-8"
    except Exception as exc:  # urllib raises many types; surface one
        raise FetchError(f"could not fetch {url}: {exc}") from exc
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
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

_LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)


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
    for block in _LD_RE.findall(page):
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
            return Extraction(value, currency, method="meta")
    m = re.search(r'itemprop=["\']price["\'][^>]*content=["\']([^"\']+)["\']', page, re.I) or \
        re.search(r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']price["\']', page, re.I)
    if m and (value := parse_number(m.group(1))):
        cur = re.search(r'itemprop=["\']priceCurrency["\'][^>]*content=["\']([A-Z]{3})["\']', page, re.I)
        return Extraction(value, cur.group(1) if cur else None, method="microdata")
    return None


# ---- store specific ------------------------------------------------------------

_STORE_PATTERNS = {
    "amazon": [
        r'id="corePrice(?:Display_desktop)?_feature_div".*?<span class="a-offscreen">([^<]+)</span>',
        r'<span class="a-price[^"]*"[^>]*><span class="a-offscreen">([^<]+)</span>',
    ],
    "ebay.": [
        r'<div class="x-price-primary"[^>]*>.*?<span class="ux-textspans">([^<]+)</span>',
        r'<span[^>]+itemprop="price"[^>]*>([^<]+)</span>',
    ],
}


def extract_store_specific(page: str, url: str) -> Extraction | None:
    for key, patterns in _STORE_PATTERNS.items():
        if key not in url:
            continue
        for pattern in patterns:
            m = re.search(pattern, page, re.S)
            if m and (value := parse_number(m.group(1))):
                return Extraction(value, guess_currency(m.group(1)), method=f"{key}-pattern")
    return None


def extract_price(page: str, url: str = "", price_regex: str | None = None) -> Extraction:
    if price_regex:
        m = re.search(price_regex, page, re.S)
        if m and (value := parse_number(m.group(1))):
            return Extraction(value, guess_currency(m.group(0)), method="custom-regex")
    for extractor in (extract_json_ld, extract_meta):
        result = extractor(page)
        if result:
            return result
    result = extract_store_specific(page, url)
    if result:
        return result
    if re.search(r"captcha|robot check|access denied", page, re.I):
        raise FetchError("the store blocked the request (captcha) - record the price manually with `hawkdrop price`")
    raise FetchError("no price found on the page - add --regex to the offer or record the price manually")


def fetch_price(url: str, price_regex: str | None = None) -> Extraction:
    from hawkdrop.specs import extract_specs

    page = fetch_html(url)
    ex = extract_price(page, url, price_regex)
    ex.specs = extract_specs(page)
    return ex
