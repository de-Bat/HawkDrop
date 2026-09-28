"""eBay as a price source.

Two kinds of eBay offers:

* a **listing** (``https://www.ebay.com/itm/<id>``): the price of that listing,
* a **search** (``https://www.ebay.com/sch/i.html?_nkw=<query>``): the cheapest
  buy-it-now listing matching the query (price + shipping), checked again on
  every ``hawksense check``. Listings come and go, so a search is usually the
  better way to follow an item on eBay.

With eBay API credentials (a free developer account: https://developer.ebay.com,
"Application keys", production) prices come from the official Browse API, which
also gives the shipping cost *to your country*. Without them HawkSense reads the
public pages, which works for most listings but eBay may block it::

    [ebay]
    client_id = "YourApp-PRD-..."
    client_secret = "PRD-..."

or set ``HAWKSENSE_EBAY_CLIENT_ID`` / ``HAWKSENSE_EBAY_CLIENT_SECRET``.
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from hawksense.fetch import Extraction, FetchError, extract_price, fetch_html, guess_currency, parse_number
from hawksense.specs import extract_specs, specs_from_pairs
from hawksense.vault import Vault, VaultError

API_ROOT = "https://api.ebay.com"
SCOPE = "https://api.ebay.com/oauth/api_scope"

# site -> (marketplace id, default currency)
SITES = {"ebay.com": ("EBAY_US", "USD"), "ebay.co.uk": ("EBAY_GB", "GBP"), "ebay.de": ("EBAY_DE", "EUR")}
CONDITIONS = {"new": "1000", "used": "3000"}  # eBay condition ids (search filter and LH_ItemCondition)
DEST_COUNTRY = {"IL": "IL", "US": "US", "UK": "GB"}  # HawkSense destination -> country for shipping quotes

HttpFn = Callable[[str, str, dict, bytes | None], dict]


class EbayError(FetchError):
    pass


def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower().removeprefix("www.")


def site_of(url: str) -> str | None:
    host = _host(url)
    return next((s for s in SITES if host == s or host.endswith("." + s)), None)


def is_ebay(url: str) -> bool:
    return site_of(url) is not None


def listing_id(url: str) -> str | None:
    m = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,15})(?:[/?#]|$)", url)
    if m:
        return m.group(1)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    item = (q.get("item") or q.get("itm") or [""])[0]
    return item if item.isdigit() else None


def search_params(url: str) -> tuple[str, str | None] | None:
    """(query, condition) for an eBay search URL, else None."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.path.startswith("/sch/"):
        return None
    q = urllib.parse.parse_qs(parsed.query)
    query = (q.get("_nkw") or [""])[0].strip()
    if not query:
        return None
    cond_id = (q.get("LH_ItemCondition") or [""])[0]
    condition = next((name for name, cid in CONDITIONS.items() if cid == cond_id), None)
    return query, condition


def search_url(query: str, site: str = "ebay.com", condition: str | None = "new") -> str:
    """A search URL, sorted by price + shipping, buy-it-now only."""
    site = site if site in SITES else "ebay.com"
    params = {"_nkw": query, "LH_BIN": "1", "_sop": "15"}
    if condition:
        if condition not in CONDITIONS:
            raise ValueError(f"condition must be one of: {', '.join(CONDITIONS)}")
        params["LH_ItemCondition"] = CONDITIONS[condition]
    return f"https://www.{site}/sch/i.html?" + urllib.parse.urlencode(params)


# ---- Browse API ------------------------------------------------------------------------

def _urllib_http(method: str, url: str, headers: dict, body: bytes | None) -> dict:
    req = urllib.request.Request(url, data=body, method=method, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.load(res)
    except urllib.error.HTTPError as exc:
        try:
            errors = json.load(exc).get("errors") or [{}]
            message = errors[0].get("longMessage") or errors[0].get("message") or exc.reason
        except Exception:
            message = exc.reason
        raise EbayError(f"eBay API error {exc.code}: {message}") from None
    except Exception as exc:
        raise EbayError(f"eBay API unreachable: {exc}") from None


def _money(node: dict | None) -> tuple[float | None, str | None]:
    if not isinstance(node, dict):
        return None, None
    return parse_number(node.get("value")), node.get("currency")


def _shipping(options: list | None, currency: str | None) -> float | None:
    """Cheapest shipping option in the listing's currency (None if unknown or not shipped to you)."""
    costs = []
    for opt in options or []:
        value, cur = _money(opt.get("shippingCost"))
        if value is None and opt.get("shippingCostType") == "FREE":
            value, cur = 0.0, currency
        if value is not None and (cur is None or cur == currency):
            costs.append(value)
    return min(costs) if costs else None


class EbayApi:
    def __init__(self, client_id: str, client_secret: str, db=None, country: str | None = None,
                 http: HttpFn | None = None):
        self.client_id, self.client_secret = client_id, client_secret
        self.db = db  # caches the access token between runs
        self.country = country
        self.http = http or _urllib_http

    def _token(self) -> str:
        if self.db is not None and (cached := self.db.get_kv("ebay_token")):
            try:  # stored encrypted, like other secrets
                data = json.loads(Vault.for_db(self.db).decrypt(cached))
            except (VaultError, ValueError):
                data = None  # from an older version or another key: just get a new one
            if data and datetime.fromisoformat(data["expires"]) > datetime.now(timezone.utc) + timedelta(minutes=2):
                return data["token"]
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode()
        res = self.http("POST", f"{API_ROOT}/identity/v1/oauth2/token",
                        {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                        body)
        token = res.get("access_token")
        if not token:
            raise EbayError("eBay API: no access token (check client_id / client_secret)")
        expires = datetime.now(timezone.utc) + timedelta(seconds=int(res.get("expires_in", 7200)))
        if self.db is not None:
            self.db.set_kv("ebay_token", Vault.for_db(self.db).encrypt(
                json.dumps({"token": token, "expires": expires.isoformat()})))
        return token

    def _get(self, path: str, params: dict, marketplace: str) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}", "X-EBAY-C-MARKETPLACE-ID": marketplace}
        if self.country:  # quote shipping to the buyer's country
            headers["X-EBAY-C-ENDUSERCTX"] = "contextualLocation=" + urllib.parse.quote(f"country={self.country}")
        return self.http("GET", f"{API_ROOT}{path}?{urllib.parse.urlencode(params)}", headers, None)

    def item(self, legacy_id: str, marketplace: str = "EBAY_US") -> Extraction:
        data = self._get("/buy/browse/v1/item/get_item_by_legacy_id", {"legacy_item_id": legacy_id}, marketplace)
        price, currency = _money(data.get("price"))
        if price is None:
            raise EbayError("eBay API: listing has no fixed price (auction?)")
        status = " ".join(a.get("estimatedAvailabilityStatus", "") for a in data.get("estimatedAvailabilities") or [])
        aspects = [(a.get("name", ""), a.get("value", "")) for a in data.get("localizedAspects") or []]
        return Extraction(price, currency, "OUT_OF_STOCK" not in status or "IN_STOCK" in status,
                          _shipping(data.get("shippingOptions"), currency), data.get("title"), "ebay-api",
                          url=data.get("itemWebUrl"), specs=specs_from_pairs(aspects, "ebay item specifics"),
                          image=(data.get("image") or {}).get("imageUrl"))

    def search(self, query: str, condition: str | None = None, marketplace: str = "EBAY_US") -> Extraction:
        filters = ["buyingOptions:{FIXED_PRICE}"]
        if condition:
            filters.append(f"conditionIds:{{{CONDITIONS[condition]}}}")
        data = self._get("/buy/browse/v1/item_summary/search",
                         {"q": query, "filter": ",".join(filters), "sort": "price", "limit": "50"}, marketplace)
        found = []
        for s in data.get("itemSummaries") or []:
            price, currency = _money(s.get("price"))
            if price is None:
                continue
            ship = _shipping(s.get("shippingOptions"), currency)
            found.append(Extraction(price, currency, True, ship, s.get("title"), "ebay-api search",
                                    url=s.get("itemWebUrl"), image=(s.get("image") or {}).get("imageUrl")))
        return _cheapest(found, query)


def _cheapest(found: list[Extraction], query: str) -> Extraction:
    if not found:
        raise EbayError(f"no eBay listings found for {query!r}")
    # listings that state their shipping cost beat ones that don't (they may not ship to you)
    return min(found, key=lambda e: (e.shipping is None, e.price + (e.shipping or 0.0)))


# ---- public pages ------------------------------------------------------------------------

_ITEM_OPEN = re.compile(r'<li\b[^>]{0,500}?class="[^"]{0,300}?s-(?:item|card)\b', re.I)
ITEM_MAX = 50_000  # one result card; bounds the per-card patterns on hostile pages
_LINK_RE = re.compile(r'href="(https://www\.ebay\.[^"]+/itm/[^"]+)"')
_PRICE_RE = re.compile(r'class="[^"]*s-(?:item|card)__price[^"]*"[^>]*>(.*?)</span>', re.S)
_SHIP_RE = re.compile(r'class="[^"]*s-(?:item__shipping|item__logisticsCost|card__shipping)[^"]*"[^>]*>(.*?)</span>',
                      re.S)
_TITLE_RE = re.compile(r'class="[^"]*s-(?:item|card)__title[^"]*"[^>]*>(?:<span[^>]*>)?(.*?)</', re.S)
_IMG_RE = re.compile(r'<img[^>]+(?:data-)?src="(https://[^"]+)"', re.S)
_TAGS_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return html_lib.unescape(_TAGS_RE.sub(" ", fragment)).strip()


def _result_cards(page: str):
    """Each search result <li> (up to its first </li>), found in linear time."""
    lower, pos = page.lower(), 0
    while m := _ITEM_OPEN.search(page, pos):
        end = lower.find("</li>", m.end())
        if end < 0:
            return
        yield page[m.start():min(end, m.start() + ITEM_MAX)]
        pos = end + 5


def parse_search_page(page: str, currency: str | None = None) -> Extraction:
    """Cheapest listing on an eBay search results page (best effort: eBay changes its markup)."""
    found = []
    for block in _result_cards(page):
        link, price = _LINK_RE.search(block), _PRICE_RE.search(block)
        if not link or not price:
            continue
        url = html_lib.unescape(link.group(1))
        if "/itm/123456" in url:  # eBay's hidden template row
            continue
        price_text = _text(price.group(1))
        if " to " in price_text:  # a price range (variations) - not comparable
            continue
        value = parse_number(price_text)
        if not value:
            continue
        shipping = None
        if ship := _SHIP_RE.search(block):
            ship_text = _text(ship.group(1))
            if re.search(r"free", ship_text, re.I):
                shipping = 0.0
            elif re.search(r"\d", ship_text):
                shipping = parse_number(ship_text)
        title = _TITLE_RE.search(block)
        image = _IMG_RE.search(block)
        found.append(Extraction(value, guess_currency(price_text) or currency, True, shipping,
                                _text(title.group(1)) if title else None, "ebay-search-page", url=url,
                                image=image.group(1) if image else None))
    if not found:
        if re.search(r"captcha|robot|pardon our interruption", page, re.I):
            raise FetchError("eBay blocked the request - add eBay API keys (see README) or log the price manually")
        raise FetchError("no listings found on the eBay search page")
    return _cheapest(found, "the search")


class EbaySource:
    """Fetches eBay prices, through the API when configured, else from the public pages."""

    def __init__(self, cfg: dict | None = None, db=None, dest_code: str | None = None, http: HttpFn | None = None,
                 env: dict | None = None):
        cfg, env = cfg or {}, os.environ if env is None else env
        client_id = env.get("HAWKSENSE_EBAY_CLIENT_ID") or cfg.get("client_id")
        client_secret = env.get("HAWKSENSE_EBAY_CLIENT_SECRET") or cfg.get("client_secret")
        self.api = (EbayApi(client_id, client_secret, db, DEST_COUNTRY.get((dest_code or "").upper()), http)
                    if client_id and client_secret else None)

    def fetch(self, url: str, price_regex: str | None = None) -> Extraction:
        site = site_of(url) or "ebay.com"
        marketplace, currency = SITES[site]
        search = search_params(url)
        item_id = None if search else listing_id(url)
        api_error = None
        if self.api and not price_regex and (search or item_id):
            try:
                if search:
                    return self.api.search(search[0], search[1], marketplace)
                return self.api.item(item_id, marketplace)
            except EbayError as exc:
                api_error = exc
        try:
            page = fetch_html(url)
            if search:
                return parse_search_page(page, currency)
            ex = extract_price(page, url, price_regex)
            ex.currency = ex.currency or currency
            ex.specs = extract_specs(page)
            return ex
        except FetchError as exc:
            if api_error:
                raise FetchError(f"{api_error}; page: {exc}") from None
            raise
