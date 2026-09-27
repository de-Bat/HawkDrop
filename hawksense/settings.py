"""Settings you change in the app, layered over config.toml.

Everything the app lets you configure lives in one database layer with the same
shape as config.toml. The effective configuration is config.toml with the app's
values on top, so a change in the app wins and takes effect right away (the server
re-reads it; no restart). Two exceptions:

* a secret (password, token, API key) that is also set in an **environment
  variable** keeps the environment's value; the app shows it as locked,
* ``hawksense --dest XX`` on the command line applies to that one command.

Secrets are write-only: the API reports whether one is set, never its value.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace

from hawksense.config import Config
from hawksense.vault import Vault, VaultError, is_encrypted

KV_KEY = "app_settings"


@dataclass(frozen=True)
class Field:
    path: str  # dotted, e.g. "notify.telegram.bot_token"
    label: str
    kind: str = "text"  # text | url | email | number | percent | bool | choice | secret
    help: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    min: float | None = None
    max: float | None = None
    placeholder: str = ""
    env: str | None = None  # environment variable that overrides it (and locks it in the app)
    default: object = None


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    description: str
    fields: tuple[Field, ...] = field(default=())


def _channel_env(path: str) -> str:
    _, channel, name = path.split(".")
    return f"HAWKSENSE_{channel.upper()}_{name.upper()}"


def _ch(path: str, label: str, kind: str = "text", help: str = "", **kw) -> Field:
    return Field(path, label, kind, help, env=_channel_env(path), **kw)


SECTIONS: tuple[Section, ...] = (
    Section("general", "General", "Where things are delivered and how the app is reached.", (
        Field("destination.code", "Deliver to", "choice",
              "Import taxes and the price currency follow the destination.",
              (("IL", "Israel (ILS)"), ("US", "United States (USD)"), ("EU", "European Union (EUR)"),
               ("UK", "United Kingdom (GBP)")), default="IL"),
        Field("notify.app_url", "App address", "url", "Used for links in notifications, e.g. your HTTPS address.",
              placeholder="https://hawksense.example.com"),
    )),
    Section("checks", "Automatic checks", "How often the server looks for new prices and rule updates.", (
        Field("schedule.check_every", "Check prices every (hours)", "number",
              "0 turns automatic price checks off.", min=0, max=168, placeholder="off"),
        Field("schedule.rules_every", "Check for rule updates every (days)", "number",
              "Taxes and forwarder rates. 0 turns it off.", min=0, max=90, default=7),
        Field("rules.feed_url", "Rules feed", "url", "Leave empty to use the default feed; type 'off' to disable.",
              placeholder="default (HawkSense repository)"),
    )),
    Section("advice", "Buy/wait advice", "How patient the advisor is.", (
        Field("advisor.max_wait_days", "Look ahead (days)", "number", "Sales further away than this are ignored.",
              min=7, max=365, default=75),
        Field("advisor.min_saving", "Minimum saving worth waiting for", "percent", "Share of the price.",
              min=0, max=0.5, default=0.03),
        Field("advisor.wait_cost_per_day", "Cost of waiting per day", "percent",
              "How much each day without the item is worth to you, as a share of its price.",
              min=0, max=0.01, default=0.0006),
    )),
    Section("email", "Email", "Send notifications through an SMTP server (Gmail: use an app password).", (
        _ch("notify.email.host", "SMTP server", placeholder="smtp.gmail.com"),
        _ch("notify.email.port", "Port", "number", "587 = STARTTLS, 465 = SSL", min=1, max=65535, default=587),
        _ch("notify.email.username", "Username", placeholder="me@gmail.com"),
        _ch("notify.email.password", "Password", "secret"),
        _ch("notify.email.sender", "From", "email", "Defaults to the username."),
        _ch("notify.email.to", "Send to", "email", placeholder="me@gmail.com"),
    )),
    Section("telegram", "Telegram", "Create a bot with @BotFather, send it a message, then find your chat id at "
            "api.telegram.org/bot<token>/getUpdates.", (
        _ch("notify.telegram.bot_token", "Bot token", "secret"),
        _ch("notify.telegram.chat_id", "Chat id", placeholder="123456789"),
    )),
    Section("whatsapp", "WhatsApp", "CallMeBot is free (get a key by messaging it); Twilio needs an account.", (
        _ch("notify.whatsapp.provider", "Provider", "choice", choices=(("callmebot", "CallMeBot"),
                                                                      ("twilio", "Twilio")), default="callmebot"),
        _ch("notify.whatsapp.phone", "Your number (CallMeBot)", placeholder="+972501234567"),
        _ch("notify.whatsapp.apikey", "API key (CallMeBot)", "secret"),
        _ch("notify.whatsapp.account_sid", "Account SID (Twilio)"),
        _ch("notify.whatsapp.auth_token", "Auth token (Twilio)", "secret"),
        _ch("notify.whatsapp.sender", "From number (Twilio)", placeholder="+14155238886"),
        _ch("notify.whatsapp.to", "To number (Twilio)", placeholder="+972501234567"),
    )),
    Section("ntfy", "ntfy push", "Free push notifications to the ntfy app on iOS, Android or desktop. Subscribe to "
            "the same topic in the app.", (
        _ch("notify.ntfy.topic", "Topic", help="Pick a long, hard-to-guess name.", placeholder="hawksense-…"),
        _ch("notify.ntfy.server", "Server", "url", placeholder="https://ntfy.sh"),
        _ch("notify.ntfy.token", "Access token", "secret", "Only for a private server."),
    )),
    Section("webhook", "Webhook", "POST every notification as JSON to a URL (Home Assistant, n8n, Zapier …).", (
        _ch("notify.webhook.url", "URL", "url", placeholder="https://…"),
    )),
    Section("ebay", "eBay API", "Optional: reliable eBay prices and shipping to your country. Free keys at "
            "developer.ebay.com (production keyset).", (
        Field("ebay.client_id", "App ID (client id)", env="HAWKSENSE_EBAY_CLIENT_ID"),
        Field("ebay.client_secret", "Cert ID (client secret)", "secret", env="HAWKSENSE_EBAY_CLIENT_SECRET"),
    )),
)

FIELDS: dict[str, Field] = {f.path: f for s in SECTIONS for f in s.fields}
SECRET_PATHS = tuple(p for p, f in FIELDS.items() if f.kind == "secret")

# per-store overrides (see hawksense.stores.with_overrides)
STORE_FIELDS: dict[str, Field] = {f.path: f for f in (
    Field("shipping_flat", "Shipping to you", "number", "In the store's currency. Empty = unknown.", min=0, max=10000),
    Field("shipping_free_over", "Free shipping over", "number", min=0, max=100000),
    Field("local_shipping_flat", "Domestic shipping (to a forwarder)", "number", min=0, max=10000),
    Field("local_free_over", "Free domestic shipping over", "number", min=0, max=100000),
    Field("ships_abroad", "Ships to you directly", "bool"),
    Field("collects_import_vat", "Charges import VAT at checkout", "bool"),
)}

SOURCE_KEYS = ("url", "target", "regex", "format", "scale")


# ---- storage ----------------------------------------------------------------------------------

def load(db) -> dict:
    """The app's settings as stored: secrets stay encrypted (see ``decrypted``)."""
    data = json.loads(db.get_kv(KV_KEY) or "{}")
    plain = [p for p in SECRET_PATHS if _get(data, p) is not None and not is_encrypted(_get(data, p))]
    if plain:  # saved by a version that didn't encrypt yet: encrypt them now
        vault = Vault.for_db(db)
        for path in plain:
            _set(data, path, vault.encrypt(str(_get(data, path))))
        _save(db, data)
    return data


def decrypted(db, data: dict) -> tuple[dict, list[str]]:
    """A copy of ``data`` with secrets decrypted, and the paths that couldn't be (wrong key)."""
    out = json.loads(json.dumps(data))
    bad = []
    vault = None
    for path in SECRET_PATHS:
        value = _get(out, path)
        if value is None:
            continue
        vault = vault or Vault.for_db(db)
        try:
            _set(out, path, vault.decrypt(value))
        except VaultError:
            _set(out, path, None)
            bad.append(path)
    return out, bad


def _save(db, data: dict):
    db.set_kv(KV_KEY, json.dumps(data, sort_keys=True))


def _get(tree: dict, path: str):
    node = tree
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set(tree: dict, path: str, value):
    *parents, leaf = path.split(".")
    node = tree
    for p in parents:
        node = node.setdefault(p, {})
    if value is None:
        node.pop(leaf, None)
    else:
        node[leaf] = value


def _prune(tree: dict) -> dict:
    return {k: (_prune(v) if isinstance(v, dict) else v) for k, v in tree.items()
            if not (isinstance(v, dict) and not _prune(v))}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def effective(db, file_cfg: Config) -> Config:
    """config.toml with the app's settings on top."""
    ui, _ = decrypted(db, load(db))
    merged = {name: _merge(getattr(file_cfg, name) or {}, ui.get(name) or {})
              for name in ("destination", "advisor", "stores", "server", "forwarders", "ebay", "rules", "notify")}
    if str(merged["rules"].get("feed_url", "")).strip().lower() == "off":
        merged["rules"]["feed_url"] = ""
    return replace(file_cfg, **merged, schedule=ui.get("schedule") or {})


def schedule(db, name: str, default: float | None) -> float | None:
    """A schedule interval: the app's value if set (0 = off), else the startup default."""
    value = (load(db).get("schedule") or {}).get(name)
    if value is None:
        return default
    return value or None


def dest_code(db) -> str | None:
    return (load(db).get("destination") or {}).get("code")


# ---- validation ---------------------------------------------------------------------------------

def _clean(f: Field, value):
    if value is None:
        return None
    if f.kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{f.label}: expected true or false")
    if f.kind in ("number", "percent"):
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{f.label}: enter a number") from None
        if (f.min is not None and v < f.min) or (f.max is not None and v > f.max):
            scale = 100 if f.kind == "percent" else 1
            raise ValueError(f"{f.label}: must be between {f.min * scale:g} and {f.max * scale:g}"
                             + ("%" if f.kind == "percent" else ""))
        return int(v) if f.path in ("advisor.max_wait_days", "notify.email.port") else v
    text = str(value).strip()
    if len(text) > 500:
        raise ValueError(f"{f.label}: too long")
    if not text:
        return None
    if f.kind == "choice" and text not in {c for c, _ in f.choices}:
        raise ValueError(f"{f.label}: choose one of {', '.join(c for c, _ in f.choices)}")
    if f.kind == "url" and not (re.match(r"^https?://\S+$", text) or (f.path == "rules.feed_url" and text == "off")):
        raise ValueError(f"{f.label}: enter a full address starting with https://")
    if f.kind == "email" and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+(\s*,\s*[^@\s]+@[^@\s]+\.[^@\s]+)*$", text):
        raise ValueError(f"{f.label}: enter an email address")
    return text


def _clean_source(name: str, src) -> dict | None:
    from hawksense.rules import normalize_path

    if src is None:
        return None
    if not re.match(r"^[\w-]{1,40}$", name):
        raise ValueError("source name: letters, digits, - and _ only")
    if not isinstance(src, dict):
        raise ValueError("a rules source needs url and target")
    out = {k: str(src[k]).strip() for k in SOURCE_KEYS if src.get(k) not in (None, "")}
    if not re.match(r"^https?://\S+$", out.get("url", "")):
        raise ValueError(f"source {name}: enter the page address (https://…)")
    if not out.get("target"):
        raise ValueError(f"source {name}: choose which rule it sets")
    out["target"] = normalize_path(out["target"])
    if out.get("format") not in (None, "table"):
        raise ValueError(f"source {name}: format must be 'table' or empty")
    if out.get("format") != "table":
        if not out.get("regex"):
            raise ValueError(f"source {name}: give a regex whose first group is the value, or use a table")
        try:
            if re.compile(out["regex"]).groups < 1:
                raise ValueError(f"source {name}: the regex needs a (group) around the value")
        except re.error as exc:
            raise ValueError(f"source {name}: bad regex: {exc}") from None
    if "scale" in out:
        out["scale"] = float(out["scale"])
    return out


def update(db, changes: dict, env: dict | None = None) -> list[str]:
    """Apply {path: value}; None (or "" for text) removes the app's value. Returns the paths changed.

    Paths: any field in SECTIONS, ``stores.<key>.<field>`` and ``rules.sources.<name>``.
    """
    env = os.environ if env is None else env
    data = load(db)
    changed = []
    for path, value in changes.items():
        if path in FIELDS:
            f = FIELDS[path]
            if f.env and env.get(f.env):
                raise ValueError(f"{f.label} is set by the {f.env} environment variable on the server")
            value = _clean(f, value)
            if f.kind == "secret" and value is not None:
                value = Vault.for_db(db).encrypt(value)  # never stored in the clear
            _set(data, path, value)
        elif path.startswith("stores.") and path.count(".") == 2:
            from hawksense.stores import STORES

            _, store, name = path.split(".")
            f = STORE_FIELDS.get(name)
            if f is None or not re.match(r"^[\w.-]{1,80}$", store):
                raise ValueError(f"unknown store setting {path!r}")
            if store not in STORES and "." not in store:
                raise ValueError(f"unknown store {store!r}")
            _set(data, path, _clean(f, value))
        elif path.startswith("rules.sources.") and path.count(".") == 2:
            name = path.split(".")[2]
            _set(data, path, _clean_source(name, value))
        else:
            raise ValueError(f"unknown setting {path!r}")
        changed.append(path)
    _save(db, _prune(data))
    return changed


# ---- what the app shows --------------------------------------------------------------------------

def view(db, file_cfg: Config, env: dict | None = None, startup: dict | None = None) -> dict:
    """Every setting with its value and where it comes from; secrets only say whether they're set."""
    env = os.environ if env is None else env
    stored = load(db)
    ui, unreadable = decrypted(db, stored)
    file_tree = {name: getattr(file_cfg, name) or {} for name in
                 ("destination", "advisor", "stores", "forwarders", "ebay", "rules", "notify")}
    file_tree["schedule"] = startup or {}
    sections = []
    for s in SECTIONS:
        rows = []
        for f in s.fields:
            env_value = env.get(f.env) if f.env else None
            ui_value, file_value = _get(ui, f.path), _get(file_tree, f.path)
            if env_value:
                value, source = env_value, "environment"
            elif ui_value is not None:
                value, source = ui_value, "app"
            elif file_value not in (None, ""):
                value, source = file_value, "config"
            else:
                value, source = f.default, "default"
            row = {"path": f.path, "label": f.label, "kind": f.kind, "help": f.help, "placeholder": f.placeholder,
                   "choices": [list(c) for c in f.choices], "min": f.min, "max": f.max, "source": source,
                   "locked": bool(env_value), "env": f.env}
            if f.kind == "secret":
                row["is_set"] = value not in (None, "")
                row["value"] = None
                if f.path in unreadable and not env_value:
                    row["unreadable"] = True  # saved with another secret key: enter it again
                    row["help"] = "Saved with a different secret key (e.g. restored from a backup) - enter it again."
            else:
                row["value"] = value
            rows.append(row)
        configured = None
        if s.key in ("email", "telegram", "whatsapp", "ntfy", "webhook"):
            from hawksense.notify import CHANNEL_TYPES

            cls = next(c for c in CHANNEL_TYPES if c.key == s.key)
            configured = cls(effective(db, file_cfg).notify.get(s.key) or {}, None, env).configured
        elif s.key == "ebay":
            eff = effective(db, file_cfg).ebay
            configured = bool((env.get("HAWKSENSE_EBAY_CLIENT_ID") or eff.get("client_id"))
                              and (env.get("HAWKSENSE_EBAY_CLIENT_SECRET") or eff.get("client_secret")))
        sections.append({"key": s.key, "title": s.title, "description": s.description, "fields": rows,
                         "configured": configured})
    from hawksense.stores import STORES

    store_over = _merge(file_tree["stores"], ui.get("stores") or {})
    stores = [{"key": st.key, "name": st.name, "currency": st.currency,
               "values": {k: getattr(st, k) for k in STORE_FIELDS},
               "overrides": {k: v for k, v in (store_over.get(st.key) or {}).items() if k in STORE_FIELDS},
               "app": sorted((ui.get("stores") or {}).get(st.key, {}))}
              for st in STORES.values()]
    sources = [{"name": n, **src, "from": "app" if n in ((ui.get("rules") or {}).get("sources") or {}) else "config"}
               for n, src in _merge((file_cfg.rules or {}).get("sources") or {},
                                    (ui.get("rules") or {}).get("sources") or {}).items()]
    return {"sections": sections, "stores": stores, "store_fields": [
        {"key": k, "label": f.label, "kind": f.kind, "help": f.help} for k, f in STORE_FIELDS.items()],
        "rule_sources": sources}
