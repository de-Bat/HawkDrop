"""Currency conversion with a daily cache and an offline fallback."""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

# Approximate USD-based rates, used only when the network is unavailable.
FALLBACK_USD_RATES = {
    "USD": 1.0, "ILS": 3.65, "EUR": 0.90, "GBP": 0.77, "CNY": 7.2, "JPY": 150.0,
    "CAD": 1.38, "AUD": 1.52, "CHF": 0.85, "PLN": 3.9, "SEK": 10.3,
}

RATES_URL = "https://api.frankfurter.app/latest?from=USD"
CACHE_TTL = timedelta(hours=12)


class FX:
    def __init__(self, db=None, offline: bool | None = None):
        self.db = db
        self.offline = offline if offline is not None else bool(os.environ.get("HAWKSENSE_OFFLINE"))
        self._rates: dict[str, float] | None = None
        self.source = "fallback"

    def _load(self) -> dict[str, float]:
        if self._rates is not None:
            return self._rates
        if self.db is not None:
            cached = self.db.get_fx()
            if cached and datetime.now(timezone.utc) - cached[1] < CACHE_TTL:
                self._rates, self.source = cached[0], "cache"
                return self._rates
        if not self.offline:
            try:
                req = urllib.request.Request(RATES_URL, headers={"User-Agent": "HawkSense/0.1"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.load(resp)
                rates = {k.upper(): float(v) for k, v in data["rates"].items()}
                rates["USD"] = 1.0
                self._rates, self.source = rates, "live"
                if self.db is not None:
                    self.db.save_fx(rates)
                return rates
            except Exception:
                pass
        if self.db is not None:
            cached = self.db.get_fx()
            if cached:  # stale cache still beats hard-coded rates
                self._rates, self.source = cached[0], "stale cache"
                return self._rates
        self._rates, self.source = dict(FALLBACK_USD_RATES), "fallback"
        return self._rates

    def rate(self, src: str, dst: str) -> float:
        src, dst = src.upper(), dst.upper()
        if src == dst:
            return 1.0
        rates = self._load()
        missing = [c for c in (src, dst) if c not in rates and c not in FALLBACK_USD_RATES]
        if missing:
            raise ValueError(f"Unknown currency: {', '.join(missing)}")
        r_src = rates.get(src, FALLBACK_USD_RATES.get(src))
        r_dst = rates.get(dst, FALLBACK_USD_RATES.get(dst))
        return r_dst / r_src

    def convert(self, amount: float, src: str, dst: str) -> float:
        return amount * self.rate(src, dst)
