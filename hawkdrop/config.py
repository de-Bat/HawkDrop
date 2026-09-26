"""User configuration (``$HAWKDROP_HOME/config.toml``, default ``~/.hawkdrop``).

Example::

    [destination]
    code = "IL"            # IL, US, EU, UK
    vat_rate = 0.18
    vat_exempt_usd = 75    # personal-import VAT exemption
    duty_exempt_usd = 500
    clearance_fee = 35     # courier fee (ILS) when taxes are due on arrival

    [destination.duty_rates]
    electronics = 0.0

    [advisor]
    max_wait_days = 75
    min_saving = 0.03
    wait_cost_per_day = 0.0006

    [stores.amazon_us]
    shipping_flat = 15
    shipping_free_over = 49
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("HAWKDROP_HOME", Path.home() / ".hawkdrop"))


@dataclass
class Config:
    destination: dict = field(default_factory=dict)
    advisor: dict = field(default_factory=dict)
    stores: dict = field(default_factory=dict)


def load_config(path: Path | None = None) -> Config:
    path = path or home() / "config.toml"
    if not path.exists():
        return Config()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config(data.get("destination", {}), data.get("advisor", {}), data.get("stores", {}))
