"""Amazon data from the Keepa API (optional; needs a paid key from keepa.com/#!api).

With a key, Amazon product links are read from Keepa instead of Amazon's pages, which block servers with
captchas. One request per product gives: the buy-box price and shipping, stock, condition, title, features,
pictures, the package's size and weight, and years of price history.

Formats (from Keepa's own client, github.com/keepacom/api_backend):
- ``GET https://api.keepa.com/product?key=&domain=&asin=&stats=&history=&buybox=``
- domain ids: 1 .com, 2 .co.uk, 3 .de, 4 .fr, 5 .co.jp, 6 .ca, 8 .it, 9 .es
- prices are integers in the smallest unit (4900 = 49.00; yen as is); -1 = no offer, -2 = unknown
- times are "Keepa minutes": minutes since 2011-01-01 UTC (unix minutes - 21564000)
- sizes in millimetres, weights in grams
- ``csv[i]`` histories: [time, price, ...] pairs; types with shipping are [time, price, shipping] triples
"""

from __future__ import annotations

import gzip
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlencode, urlparse

from hawksense.fetch import Extraction, FetchError, OutOfStock
from hawksense.specs import Specs

API_ROOT = "https://api.keepa.com"
DOMAINS = {"amazon.com": (1, "USD"), "amazon.co.uk": (2, "GBP"), "amazon.de": (3, "EUR"), "amazon.fr": (4, "EUR"),
           "amazon.co.jp": (5, "JPY"), "amazon.ca": (6, "CAD"), "amazon.it": (8, "EUR"), "amazon.es": (9, "EUR")}
KEEPA_EPOCH_MINUTES = 21_564_000
IMAGE_ROOT = "https://m.media-amazon.com/images/I/"

# csv indexes (Product.CsvType)
AMAZON, NEW, USED, REFURBISHED = 0, 1, 2, 6
BUY_BOX_SHIPPING = 18  # [time, price, shipping] triples
HISTORY_DAYS = 730
STATS_DAYS = 180

_ASIN = re.compile(r"/(?:dp|gp/product|gp/aw/d|exec/obidos/asin|o/ASIN)/([A-Z0-9]{10})(?:[/?#]|$)", re.I)

HttpFn = Callable[[str], dict]


class KeepaError(FetchError):
    pass


def product_ref(url: str) -> tuple[str, int, str] | None:
    """(ASIN, Keepa domain id, currency) for an Amazon product link Keepa covers, else None."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("smile.")
    if host not in DOMAINS:
        return None
    m = _ASIN.search(parsed.path + "/")
    if not m:
        return None
    domain, currency = DOMAINS[host]
    return m.group(1).upper(), domain, currency


def keepa_time(minutes: int) -> datetime:
    return datetime.fromtimestamp((minutes + KEEPA_EPOCH_MINUTES) * 60, timezone.utc)


def _urllib_http(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
            return json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
            data = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
        except Exception:
            data = {}
        raise KeepaError(_error_text(exc.code, data)) from None
    except Exception as exc:
        raise KeepaError(f"Keepa unreachable: {exc}") from None


def _error_text(code: int, data: dict) -> str:
    err = data.get("error") or {}
    detail = err.get("message") or err.get("type") or ""
    if code == 402:
        return "Keepa: the API subscription is not active (payment required)"
    if code == 429:
        wait = data.get("refillIn")
        return f"Keepa: out of tokens{f' (refill in {round(wait / 1000)} s)' if wait else ''}"
    if code in (400, 401, 403) and not detail:
        detail = "invalid API key"
    return f"Keepa error {code}{f': {detail}' if detail else ''}"


def _money(value, divisor: int) -> float | None:
    """Keepa price -> amount; None for 'no offer' (-1) and 'unknown' (-2)."""
    if not isinstance(value, (int, float)) or value < 0:
        return None
    return round(value / divisor, 2)


def _history(csv: list, index: int) -> list[tuple[int, int, int | None]]:
    """[(keepa minute, price, shipping or None)] from one csv history."""
    series = csv[index] if csv and len(csv) > index and csv[index] else []
    step = 3 if index == BUY_BOX_SHIPPING else 2
    return [(series[i], series[i + 1], series[i + 2] if step == 3 else None)
            for i in range(0, len(series) - step + 1, step)]


def _description(p: dict) -> str | None:
    features = [re.sub(r"\s+", " ", f).strip() for f in p.get("features") or [] if isinstance(f, str)]
    if features := [f for f in features if f]:
        return " · ".join(features[:3])
    desc = p.get("description")
    if isinstance(desc, str) and desc.strip():
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", desc)).strip()
    return None


def _image(p: dict) -> str | None:
    for img in p.get("images") or []:
        if isinstance(img, dict) and (name := img.get("l") or img.get("m")):
            return IMAGE_ROOT + name
    if csv := p.get("imagesCSV"):
        return IMAGE_ROOT + str(csv).split(",")[0]
    return None


def _specs(p: dict) -> Specs:
    """The boxed size and weight (what couriers charge for), else the item's own."""
    out = Specs(method="keepa")
    for prefix, kind in (("package", "package"), ("item", "item")):
        sides = [p.get(f"{prefix}{s}") for s in ("Length", "Width", "Height")]
        if out.dims_cm is None and all(isinstance(s, int) and s > 0 for s in sides):
            out.dims_cm, out.dims_kind = tuple(round(s / 10, 1) for s in sides), kind
        grams = p.get(f"{prefix}Weight")
        if out.weight_kg is None and isinstance(grams, int) and grams > 0:
            out.weight_kg, out.weight_kind = round(grams / 1000, 3), kind
    return out


_AVAILABILITY = {0: "In stock", 1: "Pre-order", 3: "Back-order", 4: "Out of stock"}  # availabilityAmazon


def to_extraction(p: dict, domain: int, currency: str, url: str) -> Extraction:
    """The product's current offer, or ``OutOfStock`` when nobody sells it new right now."""
    divisor = 1 if domain == 5 else 100
    stats = p.get("stats") or {}
    current = stats.get("current") or []
    title = p.get("title")
    details = {"title": title, "description": _description(p), "image": _image(p)}

    price = _money(stats.get("buyBoxPrice"), divisor)
    shipping = _money(stats.get("buyBoxShipping"), divisor)
    condition = "Used" if stats.get("buyBoxIsUsed") else None
    method = "keepa-buybox"
    if price is None:  # no buy box: Amazon's own offer, then the lowest new marketplace offer
        for index, name in ((AMAZON, "keepa-amazon"), (NEW, "keepa-new")):
            if len(current) > index and (price := _money(current[index], divisor)) is not None:
                method, shipping, condition = name, None, None
                break
    availability = stats.get("buyBoxAvailabilityMessage") or _AVAILABILITY.get(p.get("availabilityAmazon"))
    if price is None:
        raise OutOfStock(availability if availability and availability != "In stock" else "Currently unavailable",
                         details)
    if shipping is not None and method == "keepa-buybox":
        shipping = max(shipping, 0.0)
    ex = Extraction(price, currency, True, shipping, title, method, url=url, specs=_specs(p),
                    image=details["image"], condition=condition, description=details["description"],
                    availability=availability or "In stock")
    return ex


def history(p: dict, domain: int, days: int = HISTORY_DAYS) -> list[tuple[datetime, float, float | None]]:
    """One price per day (the last seen that day) for the last ``days``: the buy box with shipping when Keepa
    has it, else the lowest new offer, else Amazon's own. Days with no offer are left out."""
    divisor = 1 if domain == 5 else 100
    csv = p.get("csv") or []
    series = next((s for s in (_history(csv, BUY_BOX_SHIPPING), _history(csv, NEW), _history(csv, AMAZON)) if s), [])
    since = datetime.now(timezone.utc) - timedelta(days=days)
    daily: dict = {}
    for minute, price, ship in series:
        ts = keepa_time(minute)
        value = _money(price, divisor)
        if ts < since or value is None:
            continue
        daily[ts.date()] = (ts, value, _money(ship, divisor) if ship is not None else None)
    return [daily[d] for d in sorted(daily)]


class KeepaSource:
    """Amazon products through Keepa, when a key is configured (``[keepa] key`` or HAWKSENSE_KEEPA_KEY)."""

    def __init__(self, cfg: dict | None = None, http: HttpFn | None = None, env: dict | None = None):
        cfg, env = cfg or {}, os.environ if env is None else env
        self.key = (env.get("HAWKSENSE_KEEPA_KEY") or cfg.get("key") or "").strip() or None
        self.http = http or _urllib_http

    @property
    def configured(self) -> bool:
        return self.key is not None

    def covers(self, url: str) -> bool:
        return self.configured and product_ref(url) is not None

    def product(self, url: str, with_history: bool = False) -> tuple[dict, int, str]:
        ref = product_ref(url)
        if not ref or not self.key:
            raise KeepaError("not an Amazon product link Keepa covers")
        asin, domain, currency = ref
        params = {"key": self.key, "domain": domain, "asin": asin, "stats": STATS_DAYS, "buybox": 1,
                  "history": 1 if with_history else 0}
        data = self.http(f"{API_ROOT}/product?{urlencode(params)}")
        if data.get("error"):
            raise KeepaError(_error_text(400, data))
        products = data.get("products") or []
        if not products or not isinstance(products[0], dict) or not products[0].get("title"):
            raise KeepaError(f"Keepa has no product {asin} on amazon domain {domain}")
        return products[0], domain, currency

    def fetch(self, url: str) -> Extraction:
        p, domain, currency = self.product(url)
        return to_extraction(p, domain, currency, url)

    def fetch_with_history(self, url: str) -> tuple[Extraction | None, list, OutOfStock | None]:
        """The current offer plus its daily price history (one request)."""
        p, domain, currency = self.product(url, with_history=True)
        past = history(p, domain)
        try:
            return to_extraction(p, domain, currency, url), past, None
        except OutOfStock as exc:
            return None, past, exc
