"""Product search on Amazon's public results pages (best effort: Amazon changes its markup and blocks bots)."""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import quote_plus

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
                                image=html_lib.unescape(src.group(1)) if src else None))
    if not found:
        if re.search(r"captcha|robot check|automated access", page, re.I) or len(page) < 5000:
            raise FetchError(f"Amazon ({site}) blocked the request")
        raise FetchError(f"no listings found on the {site} search page")
    return found[:limit]


def search_candidates(query: str, site: str = "amazon.com", limit: int = 20) -> list[Extraction]:
    return parse_search_candidates(fetch_html(search_url(query, site)), site, limit)
