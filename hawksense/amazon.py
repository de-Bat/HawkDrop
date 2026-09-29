"""Product search on Amazon's public results pages (best effort: Amazon changes its markup and blocks bots)."""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import parse_qs, quote_plus, urlparse

from hawksense.fetch import Extraction, FetchError, fetch_html, guess_currency, parse_number

SITES = {"amazon.com": "USD", "amazon.co.uk": "GBP", "amazon.de": "EUR"}
_CARD_SPLIT = re.compile(r'(?=<div[^>]*data-component-type="s-search-result")')
_ASIN = re.compile(r'data-asin="(\w{10})"')
_PRICE = re.compile(r'class="a-offscreen">([^<]+)<')
_IMG = re.compile(r'<img\b[^>]*class="s-image"[^>]*>')
_SRC = re.compile(r'\ssrc="([^"]+)"')
_ALT = re.compile(r'\salt="([^"]*)"')
_H2 = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_TAGS = re.compile(r"<[^>]+>")
CARD_MAX = 20000
_USED = re.compile(r"\b(renewed|refurbished|pre-?owned|open[- ]box|certified refurb|used - \\w+)", re.I)


def condition_of(text: str) -> str | None:
    """"Renewed" / "Refurbished" / ... when the card says the item is not new."""
    m = _USED.search(text)
    return m.group(1).replace("-", " ").capitalize() if m else None


def search_url(query: str, site: str = "amazon.com") -> str:
    return f"https://www.{site}/s?k={quote_plus(query)}"


def parse_search_candidates(page: str, site: str = "amazon.com", limit: int = 20) -> list[Extraction]:
    found = []
    for card in _CARD_SPLIT.split(page)[1:]:
        card = card[:CARD_MAX]
        asin, price = _ASIN.search(card), _PRICE.search(card)
        if not asin or not price:
            continue
        text = html_lib.unescape(price.group(1))
        value = parse_number(text)
        if not value:
            continue
        img = _IMG.search(card)
        alt = _ALT.search(img.group(0)) if img else None
        src = _SRC.search(img.group(0)) if img else None
        title = html_lib.unescape(alt.group(1)).strip() if alt else None
        if not title and (h2 := _H2.search(card)):
            title = html_lib.unescape(_TAGS.sub(" ", h2.group(1))).strip()
        found.append(Extraction(value, guess_currency(text) or SITES.get(site), True, None, title or None,
                                "amazon-search-page", url=f"https://www.{site}/dp/{asin.group(1)}",
                                image=html_lib.unescape(src.group(1)) if src else None,
                                condition=condition_of(_TAGS.sub(" ", card))))
    if not found:
        if re.search(r"captcha|robot check|automated access", page, re.I) or len(page) < 5000:
            raise FetchError(f"Amazon ({site}) blocked the request")
        raise FetchError(f"no listings found on the {site} search page")
    return found[:limit]


def search_candidates(query: str, site: str = "amazon.com", limit: int = 20) -> list[Extraction]:
    return parse_search_candidates(fetch_html(search_url(query, site)), site, limit)


_TITLE_ID = re.compile(r'id="productTitle"[^>]*>(.*?)</span>', re.S)
_PAGE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_LANDING = re.compile(r'"hiRes":"(https://[^"]+)"|id="landingImage"[^>]*data-a-dynamic-image="\{&quot;(https://[^&]+)&quot;', re.S)


def listing_from_url(url: str) -> Extraction:
    """The one product a pasted link points at: title, price, picture and (when the page lists them) size/weight."""
    from hawksense.fetch import extract_price
    from hawksense.specs import extract_specs

    page = fetch_html(url)
    ex = extract_price(page, url)
    title = None
    if m := _TITLE_ID.search(page):
        title = html_lib.unescape(_TAGS.sub(" ", m.group(1))).strip()
    elif m := _PAGE_TITLE.search(page):
        title = re.sub(r"^(?:Amazon\.[a-z.]+\s*:\s*)", "", html_lib.unescape(m.group(1)).strip())
    if m := _LANDING.search(page):
        ex.image = html_lib.unescape(m.group(1) or m.group(2))
    ex.title = title or ex.title
    ex.url = ex.url or url
    if ex.specs is None:
        ex.specs = extract_specs(page)
    return ex


def search_query(url: str) -> tuple[str, str] | None:
    """(query, site) when ``url`` is an Amazon search-results page, else None."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.")
    if host not in SITES or not parsed.path.startswith("/s"):
        return None
    query = (parse_qs(parsed.query).get("k") or [""])[0].strip()
    return (query, host) if query else None


def price_search_page(url: str) -> Extraction:
    """A search-page offer's price: the cheapest *new* listing whose title matches the whole query - never just
    the first price on the page, which is whatever product Amazon happens to rank first."""
    from hawksense.api import matches_query  # the same relevance rule the product picker uses

    query, site = search_query(url)
    found = [e for e in parse_search_candidates(fetch_html(url), site, limit=60)
             if e.condition is None and matches_query(query, e.title)]
    if not found:
        raise FetchError(f"no new listing on {site} matches \u201c{query}\u201d")
    best = min(found, key=lambda e: e.price)
    best.method = "amazon-search-match"
    return best
