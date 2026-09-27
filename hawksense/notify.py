"""Notifications: events you choose, sent where you choose.

Events (opt in per channel with ``hawksense notify subscribe`` or in the web app):

======================  =====================================================
buy_now                 an item's advice changes to "buy now"
target_hit              the best delivered price reaches your target
price_drop              the best delivered price drops by X% (default 5%)
sale_soon               a sales day for your items' stores starts in N days
rules_changed           tax or forwarder rules were updated automatically
rules_review            a fetched rule change looks odd and needs your OK
specs_alert             an item's weight/size couldn't be found, stores disagree, or values need confirming
check_failed            a store's price couldn't be read N times in a row
======================  =====================================================

Channels, configured in config.toml (secrets can also come from environment
variables, e.g. ``HAWKSENSE_TELEGRAM_BOT_TOKEN``)::

    [notify]
    app_url = "https://hawksense.example.com"   # links in messages (optional)

    [notify.email]
    host = "smtp.gmail.com"
    port = 587                 # 465 = SSL
    username = "me@gmail.com"
    password = "app-password"
    to = "me@gmail.com"

    [notify.telegram]          # create a bot with @BotFather, then message it once
    bot_token = "123:ABC"
    chat_id = "123456789"

    [notify.whatsapp]          # free CallMeBot key, or Twilio
    provider = "callmebot"
    phone = "+972501234567"
    apikey = "123456"

    [notify.ntfy]              # push to the ntfy phone/desktop app
    topic = "hawksense-some-secret-name"

    [notify.webhook]           # anything else: Slack, Discord, Home Assistant ...
    url = "https://..."

The **inbox** channel is always on: the web app shows it, and can also pop up
alerts on your phone or computer while it's open.
"""

from __future__ import annotations

import base64
import json
import os
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from typing import Callable

from hawksense.calendar_events import upcoming_events


@dataclass(frozen=True)
class Event:
    key: str
    title: str
    description: str


EVENTS: dict[str, Event] = {e.key: e for e in [
    Event("buy_now", "Buy now", "An item's advice changes to buy now"),
    Event("target_hit", "Target price reached", "The best delivered price reaches your target"),
    Event("price_drop", "Price drop", "The best delivered price drops by at least the set percentage"),
    Event("sale_soon", "Sale coming up", "A sales day for your items' stores starts soon"),
    Event("rules_changed", "Rules updated", "Tax or forwarder rules were updated automatically"),
    Event("rules_review", "Rule change to review", "A fetched rule change looks unusual and needs your OK"),
    Event("specs_alert", "Weight/size problem",
          "An item's weight or size couldn't be found, stores disagree, or some stores don't list it - please confirm"),
    Event("check_failed", "Price check failing", "A store's price couldn't be read several times in a row"),
]}

DEFAULT_SETTINGS = {"price_drop_pct": 5.0, "sale_soon_days": 3, "check_failed_after": 3}
NONE = "-"  # stored when you unsubscribe an event from every channel

HttpFn = Callable[[str, str, dict, bytes | None], None]


class NotifyError(Exception):
    pass


def _urllib_http(method: str, url: str, headers: dict, body: bytes | None) -> None:
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": "HawkSense", **headers})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            res.read()
    except urllib.error.HTTPError as exc:
        # status only: echoing the response body would let the test button read other services
        raise NotifyError(f"HTTP {exc.code} {exc.reason}") from None
    except Exception as exc:
        raise NotifyError(str(exc)) from None


@dataclass
class Message:
    event: str
    title: str
    body: str = ""
    url: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(x for x in (f"🦅 {self.title}", self.body, self.url or "") if x)


# ---- channels ----------------------------------------------------------------------------------

class Channel:
    key = ""
    name = ""
    fields: tuple[str, ...] = ()  # required settings

    def __init__(self, cfg: dict, http: HttpFn, env: dict):
        self.cfg = {f: env.get(f"HAWKSENSE_{self.key.upper()}_{f.upper()}") or cfg.get(f) for f in self.all_fields()}
        self.cfg = {k: v for k, v in self.cfg.items() if v not in (None, "")}
        self.http = http

    def all_fields(self) -> tuple[str, ...]:
        return self.fields

    @property
    def configured(self) -> bool:
        return all(f in self.cfg for f in self.fields)

    def send(self, msg: Message) -> None:
        raise NotImplementedError


class Inbox(Channel):
    key, name = "inbox", "In the app"

    def send(self, msg: Message) -> None:  # stored by the Notifier itself
        pass


class Email(Channel):
    key, name = "email", "Email"
    fields = ("host", "to")
    smtp_factory = None  # tests replace this

    def all_fields(self):
        return ("host", "port", "username", "password", "sender", "to", "ssl")

    def send(self, msg: Message) -> None:
        c = self.cfg
        port = int(c.get("port", 587))
        use_ssl = str(c.get("ssl", port == 465)).lower() in ("true", "1", "yes")
        em = EmailMessage()
        em["Subject"] = " ".join(f"HawkSense: {msg.title}".split())  # item names can't add header lines
        em["From"] = c.get("sender") or c.get("username") or "hawksense@localhost"
        em["To"] = c["to"] if isinstance(c["to"], str) else ", ".join(c["to"])
        em.set_content("\n\n".join(x for x in (msg.body, msg.url) if x) or msg.title)
        factory = self.smtp_factory or (smtplib.SMTP_SSL if use_ssl else smtplib.SMTP)
        try:
            kwargs = {"context": ssl.create_default_context()} if use_ssl and not self.smtp_factory else {}
            with factory(c["host"], port, timeout=20, **kwargs) as smtp:
                if not use_ssl and not self.smtp_factory:
                    smtp.starttls(context=ssl.create_default_context())
                if c.get("username"):
                    smtp.login(c["username"], c.get("password", ""))
                smtp.send_message(em)
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            raise NotifyError(f"email: {exc}") from None


class Telegram(Channel):
    key, name = "telegram", "Telegram"
    fields = ("bot_token", "chat_id")

    def send(self, msg: Message) -> None:
        body = json.dumps({"chat_id": self.cfg["chat_id"], "text": msg.text, "disable_web_page_preview": True})
        self.http("POST", f"https://api.telegram.org/bot{self.cfg['bot_token']}/sendMessage",
                  {"Content-Type": "application/json"}, body.encode())


class WhatsApp(Channel):
    key, name = "whatsapp", "WhatsApp"

    def all_fields(self):
        return ("provider", "phone", "apikey", "account_sid", "auth_token", "sender", "to")

    @property
    def configured(self) -> bool:
        if self.cfg.get("provider", "callmebot") == "twilio":
            return all(k in self.cfg for k in ("account_sid", "auth_token", "sender", "to"))
        return all(k in self.cfg for k in ("phone", "apikey"))

    def send(self, msg: Message) -> None:
        c = self.cfg
        if c.get("provider", "callmebot") == "twilio":
            auth = base64.b64encode(f"{c['account_sid']}:{c['auth_token']}".encode()).decode()
            form = urllib.parse.urlencode({"From": f"whatsapp:{c['sender']}", "To": f"whatsapp:{c['to']}",
                                           "Body": msg.text})
            self.http("POST", f"https://api.twilio.com/2010-04-01/Accounts/{c['account_sid']}/Messages.json",
                      {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
                      form.encode())
        else:
            q = urllib.parse.urlencode({"phone": c["phone"], "text": msg.text, "apikey": c["apikey"]})
            self.http("GET", f"https://api.callmebot.com/whatsapp.php?{q}", {}, None)


class Ntfy(Channel):
    key, name = "ntfy", "ntfy push"
    fields = ("topic",)

    def all_fields(self):
        return ("server", "topic", "token")

    def send(self, msg: Message) -> None:
        server = self.cfg.get("server", "https://ntfy.sh").rstrip("/")
        # header values must be latin-1; RFC 2047 keeps Hebrew and emoji titles intact
        title = "=?UTF-8?B?" + base64.b64encode(msg.title.encode()).decode() + "?="
        headers = {"Title": title, "Tags": "eagle"}
        if msg.url:
            headers["Click"] = msg.url
        if self.cfg.get("token"):
            headers["Authorization"] = f"Bearer {self.cfg['token']}"
        self.http("POST", f"{server}/{urllib.parse.quote(self.cfg['topic'])}", headers,
                  (msg.body or msg.title).encode())


class Webhook(Channel):
    key, name = "webhook", "Webhook"
    fields = ("url",)

    def send(self, msg: Message) -> None:
        body = json.dumps({"event": msg.event, "title": msg.title, "body": msg.body, "url": msg.url,
                           "text": msg.text}, ensure_ascii=False)
        self.http("POST", self.cfg["url"], {"Content-Type": "application/json"}, body.encode())


CHANNEL_TYPES = [Inbox, Email, Telegram, WhatsApp, Ntfy, Webhook]


# ---- the notifier -----------------------------------------------------------------------------------

class Notifier:
    def __init__(self, db, cfg: dict | None = None, http: HttpFn | None = None, env: dict | None = None):
        self.db = db
        self.cfg = cfg or {}
        env = os.environ if env is None else env
        self.channels: dict[str, Channel] = {
            c.key: c(self.cfg.get(c.key) or {}, http or _urllib_http, env) for c in CHANNEL_TYPES}

    # ---- preferences ----------------------------------------------------------------------
    def subscriptions(self) -> dict[str, list[str]]:
        stored = self.db.subscriptions()
        return {e: sorted(stored[e] - {NONE}) if e in stored else ["inbox"] for e in EVENTS}

    def subscribe(self, event: str, channels: list[str]):
        if event not in EVENTS:
            raise ValueError(f"unknown event {event!r} - one of: {', '.join(EVENTS)}")
        unknown = [c for c in channels if c not in self.channels]
        if unknown:
            raise ValueError(f"unknown channel {unknown[0]!r} - one of: {', '.join(self.channels)}")
        self.db.set_subscriptions(event, channels or [NONE])

    def settings(self) -> dict:
        stored = json.loads(self.db.get_kv("notify_settings") or "{}")
        cfg = {k: v for k, v in self.cfg.items() if k in DEFAULT_SETTINGS}
        return {**DEFAULT_SETTINGS, **cfg, **stored}

    def update_settings(self, values: dict):
        clean = {}
        for k, v in values.items():
            if k not in DEFAULT_SETTINGS:
                raise ValueError(f"unknown setting {k!r} - one of: {', '.join(DEFAULT_SETTINGS)}")
            v = float(v)
            if not 0 < v <= 100:
                raise ValueError(f"{k} must be between 0 and 100")
            clean[k] = v
        stored = json.loads(self.db.get_kv("notify_settings") or "{}")
        self.db.set_kv("notify_settings", json.dumps({**stored, **clean}))

    # ---- sending ------------------------------------------------------------------------------
    def _link(self, item_id: int | None) -> str | None:
        base = (self.cfg.get("app_url") or "").rstrip("/")
        return f"{base}/#/item/{item_id}" if base and item_id else (f"{base}/" if base else None)

    def emit(self, event: str, title: str, body: str = "", dedup: str | None = None,
             item_id: int | None = None) -> int | None:
        """Record and deliver a notification once per ``dedup`` key. Returns its id (None if a repeat)."""
        url = self._link(item_id)
        nid = self.db.add_notification(event, title, body, dedup, item_id, url)
        if nid is None:
            return None
        subs = self.subscriptions().get(event, [])
        deliveries = {}
        msg = Message(event, title, body, url)
        for key in subs:
            ch = self.channels.get(key)
            if ch is None or key == "inbox":
                continue
            if not ch.configured:
                deliveries[key] = "not configured"
                continue
            try:
                ch.send(msg)
                deliveries[key] = "sent"
            except NotifyError as exc:
                deliveries[key] = f"failed: {exc}"
        if "inbox" not in subs:
            self.db.mark_read([nid])  # kept as history, but not shown as new
        self.db.set_deliveries(nid, deliveries)
        return nid

    def test(self, channel: str) -> str:
        ch = self.channels.get(channel)
        if ch is None:
            raise ValueError(f"unknown channel {channel!r}")
        if channel == "inbox":
            self.db.add_notification("test", "Test notification", "Notifications work.")
            return "added to the inbox"
        if not ch.configured:
            raise NotifyError(f"{ch.name} is not configured - see [notify.{channel}] in the README")
        ch.send(Message("test", "Test notification", "HawkSense notifications work.", self._link(None)))
        return "sent"

    # ---- events from price checks --------------------------------------------------------------------
    def _state(self, item_id: int) -> dict:
        return json.loads(self.db.get_kv(f"notify_state:{item_id}") or "{}")

    def _save_state(self, item_id: int, state: dict):
        self.db.set_kv(f"notify_state:{item_id}", json.dumps(state))

    def record_check(self, tracker, item, results) -> list[int]:
        """Count consecutive failures per offer and alert after the configured number."""
        after = int(self.settings()["check_failed_after"])
        out = []
        for r in results:
            key = f"check_fails:{r.offer.id}"
            if r.error is None:
                self.db.set_kv(key, "0")
                continue
            if r.error.startswith("no URL"):
                continue
            n = int(self.db.get_kv(key) or 0) + 1
            self.db.set_kv(key, str(n))
            if n == after and not item.muted:
                nid = self.emit("check_failed", f"{item.name}: can't read {r.store.name}",
                                f"{n} checks in a row failed: {r.error}\nLog the price by hand, or fix the link.",
                                f"check_failed:{r.offer.id}:{date.today().isoformat()}", item.id)
                out += [nid] if nid else []
        return out

    def evaluate(self, tracker, items=None, today: date | None = None) -> list[int]:
        """Look at the current prices and advice and raise the events that happened since last time."""
        today = today or date.today()
        s = self.settings()
        cur = tracker.dest.currency
        new: list[int] = []
        sale_items: dict[tuple, list[str]] = {}
        for item in items if items is not None else tracker.db.list_items():
            item = tracker.db.get_item(item.id)
            if item is None or item.muted:
                continue
            state = self._state(item.id)
            adv, quotes = tracker.advise(item, today)
            in_stock = [q for q in quotes if q.point.in_stock and q.landed.hold is None]  # no price on hold
            best = in_stock[0] if in_stock else None
            if best:
                total, where = best.landed.total, f"{best.store.name} ({best.landed.route_label})"
                if adv.action == "BUY_NOW" and state.get("action") != "BUY_NOW":
                    new.append(self.emit("buy_now", f"Buy now: {item.name}",
                                         f"{_money(total, cur)} delivered at {where}. "
                                         f"Confidence {adv.confidence:.0%}.\n" + "\n".join(adv.reasons[:2]),
                                         f"buy_now:{item.id}:{today.isoformat()}", item.id))
                hit = item.target_price is not None and total <= item.target_price
                if hit and not state.get("target_hit"):
                    new.append(self.emit("target_hit", f"Target reached: {item.name}",
                                         f"{_money(total, cur)} delivered at {where} "
                                         f"(target {_money(item.target_price, cur)}).",
                                         f"target_hit:{item.id}:{round(total)}", item.id))
                ref = state.get("ref")
                if ref is None or total > ref:
                    ref = total
                elif total <= ref * (1 - s["price_drop_pct"] / 100):
                    new.append(self.emit("price_drop", f"Price drop: {item.name}",
                                         f"{_money(ref, cur)} → {_money(total, cur)} delivered at {where} "
                                         f"(-{(ref - total) / ref:.0%}).",
                                         f"price_drop:{item.id}:{round(total)}", item.id))
                    ref = total
                state.update(action=adv.action, target_hit=hit, ref=ref)
            else:
                state.update(action=adv.action)
            self._save_state(item.id, state)

            w_manual, d_manual = item.weight_source == "manual", item.dims_source == "manual"
            found, rows = tracker.specs(item) if not (w_manual and d_manual) else (None, [])
            if rows and found.alert and not w_manual:
                what = ("couldn't find the weight" if found.status == "missing"
                        else "store pages disagree on the weight/size")
                body = ("; ".join(found.messages) + ". Forwarder prices for this item are on hold until you set "
                        "the weight and size (edit the item in the app, or `hawksense track "
                        f"'{item.name}' --weight KG --dims LxWxH`).")
                new.append(self.emit("specs_alert", f"{item.name}: {what}", body,
                                     f"specs:{item.id}:{found.status}:{found.weight_kg}", item.id))
            elif rows and not found.alert and (ask := found.to_confirm(w_manual, d_manual)):
                # values are in use, but some store pages don't list them: ask once per set of values
                body = (" ".join(ask) + " Forwarder prices use these values. If they're right, confirm them "
                        f"(\"Looks right\" on the item in the app, or `hawksense specs '{item.name}' --confirm`); "
                        "if not, set them yourself.")
                new.append(self.emit("specs_alert", f"{item.name}: please confirm the weight and size", body,
                                     f"specs-confirm:{item.id}:{found.weight_kg}:{found.dims_text}", item.id))
            keys = {e for q in quotes for e in q.store.events} or {
                e for o in tracker.db.offers(item) for e in tracker.store_for(o).events}
            for occ in upcoming_events(today, int(s["sale_soon_days"])):
                if occ.event.key in keys and occ.days_until(today) > 0:
                    sale_items.setdefault((occ.event.key, occ.event.name, occ.start), []).append(item.name)
        for (key, name, start), names in sorted(sale_items.items(), key=lambda x: x[0][2]):
            days = (start - today).days
            new.append(self.emit("sale_soon", f"{name} starts in {days} day{'s' if days != 1 else ''}",
                                 f"On {start:%a %d %b}. Worth watching: {', '.join(names)}.",
                                 f"sale_soon:{key}:{start.isoformat()}"))
        return [n for n in new if n]

    # ---- events from rules checks ---------------------------------------------------------------------
    def rules_report(self, report) -> list[int]:
        from hawksense.rules import describe

        new = []
        if report.applied:
            lines = [f"{describe(c.path)}: {_fmt(c.old)} → {_fmt(c.new)}" for c in report.applied]
            new.append(self.emit("rules_changed", f"Rules updated ({len(lines)} change{'s' if len(lines) > 1 else ''})",
                                 "\n".join(lines[:15]) + ("\n…" if len(lines) > 15 else ""),
                                 "rules_changed:" + ",".join(str(c.id) for c in report.applied)))
        for c in report.pending:
            new.append(self.emit("rules_review", f"Check this rule change: {describe(c.path)}",
                                 f"{_fmt(c.old)} → {_fmt(c.new)} from {c.source} ({c.note}). "
                                 f"Accept with `hawksense rules accept {c.id}` or in the app's settings.",
                                 f"rules_review:{c.id}"))
        return [n for n in new if n]


def _money(v: float, cur: str) -> str:
    return f"{v:,.0f} {cur}"


def _fmt(v) -> str:
    if isinstance(v, float) and v < 1 and v > 0:
        return f"{v:g} ({v:.1%})"
    return "none" if v is None else f"{v:g}" if isinstance(v, float) else str(v)
