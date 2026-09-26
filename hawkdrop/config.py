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


def db_path(cli_value: str | None = None) -> Path:
    return Path(cli_value or os.environ.get("HAWKDROP_DB") or home() / "hawkdrop.db")


@dataclass
class Config:
    destination: dict = field(default_factory=dict)
    advisor: dict = field(default_factory=dict)
    stores: dict = field(default_factory=dict)
    server: dict = field(default_factory=dict)


def load_config(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("HAWKDROP_CONFIG") or home() / "config.toml")
    if not path.exists():
        return Config()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config(data.get("destination", {}), data.get("advisor", {}), data.get("stores", {}),
                  data.get("server", {}))


@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str | None = None
    check_every: float | None = None  # hours
    cert: str | None = None
    key: str | None = None
    base_path: str = ""


_SERVER_TYPES = {"host": str, "port": int, "token": str, "check_every": float, "cert": str, "key": str,
                 "base_path": str}


def server_settings(cli: dict, cfg: dict, env: dict | None = None) -> ServerSettings:
    """Resolve serve settings: CLI flag > environment variable > [server] in config.toml > default."""
    env = os.environ if env is None else env
    values = {}
    for name, cast in _SERVER_TYPES.items():
        for source in (cli.get(name), env.get(f"HAWKDROP_{name.upper()}"), cfg.get(name)):
            if source not in (None, ""):
                try:
                    values[name] = cast(source)
                except (TypeError, ValueError):
                    raise SystemExit(f"error: invalid value for {name}: {source!r}") from None
                break
    if "token" not in values:
        token_file = cli.get("token_file") or env.get("HAWKDROP_TOKEN_FILE") or cfg.get("token_file")
        if token_file:
            try:
                values["token"] = Path(token_file).read_text().strip() or None
            except OSError as exc:
                raise SystemExit(f"error: cannot read token file {token_file}: {exc}") from None
    if values.get("base_path"):
        values["base_path"] = "/" + values["base_path"].strip("/") if values["base_path"].strip("/") else ""
    if values.get("check_every") is not None and values["check_every"] <= 0:
        values["check_every"] = None
    return ServerSettings(**values)
