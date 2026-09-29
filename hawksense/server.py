"""HawkSense web server: JSON API + the installable web client (PWA).

Standard library only. Every request opens its own SQLite connection, so the
threaded server is safe. Run with ``hawksense serve``.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import mimetypes
import os
import re
import signal
import socket
import ssl
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from hawksense import __version__, api, rules
from hawksense import settings as app_settings
from hawksense.config import Config
from hawksense.currency import FX
from hawksense.db import Database
from hawksense.demo import seed_demo
from hawksense.fetch import FetchError
from hawksense.forwarders import parse_dims
from hawksense.notify import NotifyError
from hawksense.tracker import Tracker

WEB_ROOT = Path(__file__).parent / "web"
MAX_BODY = 1 << 20

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # the access token can be in the page URL - never leak it to store sites via Referer
    "Referrer-Policy": "no-referrer",
}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
       "connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")


_TOKEN_IN_URL = re.compile(r"(token=)[^&\s\"]+", re.I)
MAX_FAILURES, FAILURE_WINDOW = 20, 60.0  # wrong tokens per client per minute before it's told to wait
LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".home.arpa", ".internal")


def host_allowed(host_header: str, extra: tuple[str, ...] = ()) -> bool:
    """Host names a token-less server answers to: IP addresses, localhost, local network names, and ``extra``.

    A DNS-rebinding page always arrives with its own public domain in the Host header.
    """
    host = host_header.strip().lower()
    if host.startswith("["):
        name = host[1:host.find("]")] if "]" in host else ""
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name == "localhost" or "." not in name or name.endswith(LOCAL_SUFFIXES) or name in extra


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class ServerContext:
    db_path: Path
    config: Config
    dest_code: str | None = None
    offline_fx: bool | None = None
    token: str | None = None
    base_path: str = ""  # e.g. "/hawksense" when a reverse proxy forwards the prefix unchanged
    check_every: float | None = None  # startup defaults; the app's settings override them
    rules_every: float | None = 7.0
    allowed_hosts: tuple[str, ...] = ()  # extra host names for a token-less server (HAWKSENSE_ALLOWED_HOSTS)
    _failures: dict = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        env = tuple(h.strip().lower() for h in os.environ.get("HAWKSENSE_ALLOWED_HOSTS", "").split(",") if h.strip())
        self.allowed_hosts = tuple(h.lower() for h in self.allowed_hosts) + env

    def record_failure(self, client: str):
        with self._lock:
            now = time.monotonic()
            self._failures[client] = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW] + [now]

    def too_many_failures(self, client: str) -> bool:
        with self._lock:
            now = time.monotonic()
            recent = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW]
            self._failures[client] = recent
            return len(recent) >= MAX_FAILURES

    def tracker(self) -> Tracker:
        db = Database(self.db_path)
        cfg = app_settings.effective(db, self.config)
        t = Tracker.from_config(db, FX(db, offline=self.offline_fx or None), cfg,
                                app_settings.dest_code(db) or self.dest_code)
        t.file_config = self.config
        t.startup = {"check_every": self.check_every, "rules_every": self.rules_every}
        return t

    def interval(self, name: str) -> float | None:
        db = Database(self.db_path)
        try:
            return app_settings.schedule(db, name, getattr(self, name))
        finally:
            db.close()


# ---- request parsing helpers ------------------------------------------------------

def _num(body: dict, key: str, required: bool = False) -> float | None:
    v = body.get(key)
    if v in (None, ""):
        if required:
            raise ApiError(400, f"'{key}' is required")
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"'{key}' must be a number") from None


def _str(body: dict, key: str, required: bool = False) -> str | None:
    v = body.get(key)
    v = v.strip() if isinstance(v, str) else v
    if not v:
        if required:
            raise ApiError(400, f"'{key}' is required")
        return None
    return str(v)


def _item(t: Tracker, item_id: str):
    item = t.db.get_item(int(item_id))
    if not item:
        raise ApiError(404, "item not found")
    return item


# ---- routes -------------------------------------------------------------------------

ROUTES: list[tuple[str, re.Pattern, str]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn.__name__))
        return fn
    return deco


class Api:
    """Route handlers. Each gets a fresh Tracker, the path params and the JSON body."""

    @staticmethod
    @route("GET", r"/api/health")
    def health(t, body, query):
        return {"ok": t.db.healthy(), "version": __version__, "last_check": t.db.get_kv("last_auto_check")}

    @staticmethod
    @route("GET", r"/api/meta")
    def get_meta(t, body, query):
        return api.meta(t)

    @staticmethod
    @route("GET", r"/api/snapshot")
    def get_snapshot(t, body, query):
        return api.snapshot(t)

    @staticmethod
    @route("GET", r"/api/events")
    def get_events(t, body, query):
        return api.events(date.today(), int(query.get("days", ["365"])[0]))

    @staticmethod
    @route("GET", r"/api/items/(\d+)")
    def get_item(t, body, query, item_id):
        return api.item_detail(t, _item(t, item_id))

    @staticmethod
    @route("POST", r"/api/search")
    def search_candidates(t, body, query):
        q = _str(body, "query", required=True)
        condition = _str(body, "condition")
        if condition not in (None, "new", "used", "any"):
            raise ApiError(400, "'condition' must be new, used or any")
        try:
            found = t.ebay.search_candidates(q, None if condition in (None, "any") else condition)
        except FetchError as exc:
            raise ApiError(502, str(exc)) from None
        return {"results": [api.search_candidate(ex) for ex in found]}

    @staticmethod
    @route("POST", r"/api/items")
    def create_item(t, body, query):
        name = _str(body, "name", required=True)
        category = _str(body, "category") or "default"
        target = _num(body, "target_price")
        item = t.db.get_item(name) if not name.isdigit() else None
        if item and item.name.lower() == name.lower():  # idempotent: replayed offline creates
            t.db.update_item(item, category, target)
        else:
            item = t.db.add_item(name, category, target)
        for url in body.get("urls") or []:
            if isinstance(url, str) and url.strip():
                try:
                    t.add_offer(item, url.strip())
                except ValueError as exc:
                    raise ApiError(400, f"{url.strip()[:80]}: {exc}") from None
        if body.get("search_all_stores"):
            # adding the offers is instant (no network); prices for them arrive on the next check,
            # scheduled or manual, so creating the item never blocks on fetching a dozen store pages
            t.search_all_stores(item, name)
        if body.get("check") and body.get("urls"):
            t.check(item)
        return api.item_detail(t, t.db.get_item(item.id))

    @staticmethod
    @route("POST", r"/api/items/(\d+)/search")
    def search_stores(t, body, query, item_id):
        item = _item(t, item_id)
        added = t.search_all_stores(item, _str(body, "query") or item.name)
        if added and body.get("check", True):
            t.check(item)
        return api.item_detail(t, item)

    @staticmethod
    @route("PATCH", r"/api/items/(\d+)")
    def update_item(t, body, query, item_id):
        item = _item(t, item_id)
        dims = _str(body, "dims")
        if dims:
            try:
                dims = "x".join(f"{d:g}" for d in parse_dims(dims))
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
        image_url = _str(body, "image_url")
        if image_url and not re.match(r"^https?://", image_url, re.I):
            raise ApiError(400, "'image_url' must be a http(s) link")
        t.db.update_item(item, _str(body, "category"), _num(body, "target_price"), _num(body, "weight_kg"), dims,
                         muted=body["muted"] is True if "muted" in body else None, image_url=image_url)
        for col in ("target_price", "weight_kg", "dims", "image_url"):
            if col in body and body[col] in (None, ""):
                t.db.clear_item_field(item, col)
        return api.item_detail(t, t.db.get_item(item.id))

    @staticmethod
    @route("DELETE", r"/api/items/(\d+)")
    def delete_item(t, body, query, item_id):
        item = t.db.get_item(int(item_id))
        if item:  # deleting twice (offline replay) is fine
            t.db.delete_item(item)
        return {"deleted": int(item_id)}

    @staticmethod
    @route("POST", r"/api/items/(\d+)/offers")
    def add_offer(t, body, query, item_id):
        item = _item(t, item_id)
        try:
            t.add_offer(item, _str(body, "url", required=True), _num(body, "shipping"),
                        _str(body, "shipping_currency"), _str(body, "regex"), _num(body, "local_shipping"))
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        return api.item_detail(t, item)

    @staticmethod
    @route("DELETE", r"/api/items/(\d+)/offers/(\d+)")
    def remove_offer(t, body, query, item_id, offer_id):
        item = _item(t, item_id)
        t.db.deactivate_offer(int(offer_id))
        return api.item_detail(t, item)

    @staticmethod
    @route("POST", r"/api/items/(\d+)/prices")
    def add_price(t, body, query, item_id):
        item = _item(t, item_id)
        when = _str(body, "date")
        try:
            when_d = date.fromisoformat(when[:10]) if when else None
        except ValueError:
            raise ApiError(400, "'date' must be YYYY-MM-DD") from None
        try:
            t.record_price(item, _str(body, "store", required=True), _num(body, "price", required=True),
                           _str(body, "currency"), _num(body, "shipping"), body.get("in_stock", True) is not False,
                           when_d, _str(body, "client_id"))
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        t.notifier.evaluate(t, [item])
        return api.item_detail(t, item)

    @staticmethod
    @route("POST", r"/api/items/(\d+)/check")
    def check_item(t, body, query, item_id):
        item = _item(t, item_id)
        results = api.check_results(t.check_and_notify([item])[item.id])
        return {"results": results, "item": api.item_detail(t, item)}

    @staticmethod
    @route("POST", r"/api/check")
    def check_all(t, body, query):
        return {"results": {i: api.check_results(r) for i, r in t.check_and_notify().items()}}

    @staticmethod
    @route("POST", r"/api/items/(\d+)/specs")
    def fetch_specs(t, body, query, item_id):
        item = _item(t, item_id)
        try:
            t.fetch_specs(item, _str(body, "url", required=True))
        except FetchError as exc:
            raise ApiError(502, str(exc)) from None
        t.notifier.evaluate(t, [item])
        return api.item_detail(t, item)

    # ---- settings ------------------------------------------------------------------------
    @staticmethod
    @route("GET", r"/api/settings")
    def get_settings(t, body, query):
        return api.settings_view(t)

    @staticmethod
    @route("PUT", r"/api/settings")
    def put_settings(t, body, query):
        changes = body.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise ApiError(400, "'changes' must be an object of {setting: value}")
        try:
            app_settings.update(t.db, changes)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        return api.settings_view(t)

    # ---- rules ----------------------------------------------------------------------------
    @staticmethod
    @route("GET", r"/api/rules")
    def get_rules(t, body, query):
        return api.rules_summary(t)

    @staticmethod
    @route("POST", r"/api/rules/check")
    def check_rules(t, body, query):
        report = rules.check_updates(t.db, t.config)
        t.notifier.rules_report(report)
        return {"applied": len(report.applied), "pending": len(report.pending), "errors": report.errors,
                "rules": api.rules_summary(t)}

    @staticmethod
    @route("POST", r"/api/rules/manual")
    def set_rules(t, body, query):
        changes = body.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise ApiError(400, "'changes' must be an object of {rule: value}")
        try:
            rules.set_manual(t.db, changes, t.forwarders)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        return api.rules_summary(t)

    @staticmethod
    @route("POST", r"/api/rules/changes/(\d+)/(accept|reject)")
    def decide_rule(t, body, query, change_id, decision):
        try:
            rules.decide(t.db, int(change_id), decision == "accept")
        except ValueError:
            pass  # already decided (e.g. a replayed offline action)
        return api.rules_summary(t)

    # ---- notifications -------------------------------------------------------------------------
    @staticmethod
    @route("GET", r"/api/notifications")
    def get_notifications(t, body, query):
        return api.notifications(t)

    @staticmethod
    @route("POST", r"/api/notifications/read")
    def read_notifications(t, body, query):
        ids = body.get("ids")
        if ids is not None and not (isinstance(ids, list) and all(isinstance(i, int) for i in ids)):
            raise ApiError(400, "'ids' must be a list of numbers")
        t.db.mark_read(ids)
        return api.notifications(t)

    @staticmethod
    @route("PUT", r"/api/notify/settings")
    def notify_settings(t, body, query):
        try:
            for event, channels in (body.get("subscriptions") or {}).items():
                if not isinstance(channels, list):
                    raise ValueError(f"channels for {event} must be a list")
                t.notifier.subscribe(event, channels)
            if body.get("settings"):
                t.notifier.update_settings(body["settings"])
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        return api.notifications(t)

    @staticmethod
    @route("POST", r"/api/notify/test")
    def notify_test(t, body, query):
        try:
            return {"result": t.notifier.test(_str(body, "channel", required=True))}
        except (NotifyError, ValueError) as exc:
            raise ApiError(400, str(exc)) from None

    @staticmethod
    @route("GET", r"/api/forwarders")
    def get_forwarders(t, body, query):
        return api.forwarders(t)

    @staticmethod
    @route("POST", r"/api/forwarders/accounts")
    def save_forwarder_account(t, body, query):
        try:
            t.save_account(_str(body, "forwarder", required=True), _str(body, "warehouse", required=True),
                           _str(body, "address") or "", _str(body, "suite") or "", _num(body, "sales_tax"))
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        return api.forwarders(t)

    @staticmethod
    @route("DELETE", r"/api/forwarders/accounts/([\w-]+)/([\w-]+)")
    def delete_forwarder_account(t, body, query, forwarder, warehouse):
        t.remove_account(forwarder, warehouse)  # removing twice (offline replay) is fine
        return api.forwarders(t)

    @staticmethod
    @route("POST", r"/api/demo")
    def demo(t, body, query):
        item = seed_demo(t.db)
        return api.item_detail(t, item)


# ---- HTTP handler --------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "HawkSense"
    ctx: ServerContext  # set by make_server
    quiet = False

    def log_message(self, fmt, *args):
        if not self.quiet:  # the app URL can carry ?token=...: never write it to the log
            super().log_message("%s", _TOKEN_IN_URL.sub(r"\1[redacted]", fmt % args))

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _send(self, status: int, payload: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: int, data):
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _dispatch(self, method: str):
        url = urlparse(self.path)
        base = self.ctx.base_path
        if base:
            if url.path == base:  # "/hawksense" -> "/hawksense/" so relative URLs resolve
                return self._send(308, b"", "text/plain", {"Location": base + "/" + (f"?{url.query}" if url.query else "")})
            if not url.path.startswith(base + "/"):
                return self._send(404, b"not found", "text/plain")
            url = url._replace(path=url.path[len(base):])
        if url.path.startswith("/api/"):
            if not self.ctx.token and not host_allowed(self.headers.get("Host", ""), self.ctx.allowed_hosts):
                # a web page can point its own domain at 127.0.0.1 (DNS rebinding) and read a token-less API
                return self._json(403, {"error": "unknown host name - add it to HAWKSENSE_ALLOWED_HOSTS "
                                                 "or protect the server with an access token"})
            self._api(method, url)
        elif method == "GET":
            self._static(url.path, parse_qs(url.query))
        else:
            self._json(405, {"error": "method not allowed"})

    def _authorized(self, path: str, query: dict) -> bool:
        token = self.ctx.token
        if not token or path == "/api/health":  # health stays open for container probes
            return True
        given = (self.headers.get("X-HawkSense-Token") or self.headers.get("X-HawkDrop-Token")  # pre-rename apps
                 or query.get("token", [""])[0])
        return hmac.compare_digest(given.encode(), token.encode())

    def _cross_site(self) -> bool:
        """True when a browser tells us another site sent this request."""
        site = self.headers.get("Sec-Fetch-Site")
        if site:
            return site not in ("same-origin", "none")
        origin = self.headers.get("Origin")
        if not origin:
            return False  # not from a browser page (curl, scripts, the CLI)
        hosts = {h.strip().lower() for h in (self.headers.get("Host", ""), self.headers.get("X-Forwarded-Host", ""))}
        return origin == "null" or urlparse(origin).netloc.lower() not in hosts

    def _api(self, method: str, url):
        query = parse_qs(url.query)
        client = self.client_address[0]
        if self.ctx.too_many_failures(client):
            return self._json(429, {"error": "too many wrong tokens - try again in a minute"})
        if not self._authorized(url.path, query):
            self.ctx.record_failure(client)
            return self._json(401, {"error": "invalid or missing token"})
        if method != "GET":
            # a page on another site can send a "simple" POST (text/plain, forms) without asking;
            # it can't send JSON or custom headers without the server's consent, which it never gets
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._json(415, {"error": "send JSON (Content-Type: application/json)"})
            if self._cross_site():
                return self._json(403, {"error": "cross-site request refused"})
        for m, pattern, name in ROUTES:
            match = pattern.match(url.path)
            if m == method and match:
                break
        else:
            return self._json(404, {"error": "no such endpoint"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "request too large")
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if not isinstance(body, dict):
                raise ApiError(400, "JSON object expected")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid JSON"})
        except ApiError as exc:
            return self._json(exc.status, {"error": str(exc)})
        t = self.ctx.tracker()
        try:
            result = getattr(Api, name)(t, body, query, *match.groups())
            self._json(201 if method == "POST" and name in ("create_item",) else 200, result)
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception as exc:  # keep the server alive, report the error
            traceback.print_exc()
            self._json(500, {"error": "internal error - details are in the server log"})
        finally:
            t.db.close()

    def _static(self, path: str, query: dict):
        rel = unquote(path).lstrip("/") or "index.html"
        target = (WEB_ROOT / rel).resolve()
        if not target.is_relative_to(WEB_ROOT.resolve()) or not target.is_file():
            if "." not in Path(rel).name:  # client-side route -> app shell
                target = WEB_ROOT / "index.html"
            else:
                return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("json", "javascript")):
            ctype += "; charset=utf-8"
        headers = {"Cache-Control": "no-cache"}  # the service worker does the caching
        body = target.read_bytes()
        if target.name == "sw.js":
            headers["Service-Worker-Allowed"] = "/"
        if target.name == "manifest.webmanifest" and query.get("token"):
            # so an app added to the iOS home screen starts with the access token
            manifest = json.loads(body)
            manifest["start_url"] = "./?" + urlencode({"token": query["token"][0]})
            body = json.dumps(manifest, ensure_ascii=False).encode()
        self._send(200, body, ctype, headers)


def run_scheduled_check(ctx: ServerContext):
    t = ctx.tracker()
    try:
        results = t.check_and_notify()
        ok = sum(r.error is None for rs in results.values() for r in rs)
        failed = sum(r.error is not None for rs in results.values() for r in rs)
        t.db.set_kv("last_auto_check", datetime.now(timezone.utc).isoformat())
        log(f"scheduled price check: {len(results)} items, {ok} prices updated, {failed} failed")
    except Exception:
        traceback.print_exc()
    finally:
        t.db.close()


def run_rules_check(ctx: ServerContext):
    t = ctx.tracker()
    try:
        report = rules.check_updates(t.db, t.config)
        t.notifier.rules_report(report)
        log(f"rules check: {len(report.applied)} updated, {len(report.pending)} to review"
            + (f", problems: {'; '.join(report.errors)}" if report.errors else ""))
    except Exception:
        traceback.print_exc()
    finally:
        t.db.close()


def _last_rules_check(t) -> str | None:
    last = rules.last_check(t.db)
    return last["ts"] if last else None


def _next_check_delay(ctx: ServerContext, every: timedelta, last_of=lambda t: t.db.get_kv("last_auto_check"),
                      first_delay: float = 60.0) -> float:
    """Seconds until the next check, remembering the last run across restarts."""
    t = ctx.tracker()
    try:
        last = last_of(t)
    finally:
        t.db.close()
    if not last:
        return first_delay  # first start: give the server a moment, then check
    due = datetime.fromisoformat(last) + every
    return max(30.0, (due - datetime.now(timezone.utc)).total_seconds())


def _schedule_loop(ctx: ServerContext, name: str, unit: timedelta, last_of, run, first_delay: float,
                   stop: threading.Event):
    """Run ``run`` every ``ctx.interval(name)`` units; the interval is re-read so changes in the app apply live."""
    while not stop.is_set():
        every = ctx.interval(name)
        if not every:
            if stop.wait(60):
                return
            continue
        delay = _next_check_delay(ctx, unit * every, last_of, first_delay)
        if delay > 300:  # wake up now and then to notice a changed interval
            stop.wait(300)
            continue
        if stop.wait(delay):
            return
        run(ctx)


def _checker_loop(ctx: ServerContext, stop: threading.Event):
    _schedule_loop(ctx, "check_every", timedelta(hours=1), lambda t: t.db.get_kv("last_auto_check"),
                   run_scheduled_check, 60.0, stop)


def _rules_loop(ctx: ServerContext, stop: threading.Event):
    _schedule_loop(ctx, "rules_every", timedelta(days=1), _last_rules_check, run_rules_check, 120.0, stop)


def log(message: str):
    print(f"[hawksense {time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def make_server(ctx: ServerContext, host: str = "127.0.0.1", port: int = 8765, certfile: str | None = None,
                keyfile: str | None = None, quiet: bool = False) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx, "quiet": quiet})
    server_cls = ThreadingHTTPServer
    if ":" in host:  # IPv6, e.g. "::" for all interfaces
        server_cls = type("V6Server", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6})
    httpd = server_cls((host, port), handler)
    httpd.daemon_threads = True
    if certfile:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(certfile, keyfile)
        httpd.socket = tls.wrap_socket(httpd.socket, server_side=True)
    return httpd


def serve(ctx: ServerContext, host: str, port: int, certfile: str | None = None, keyfile: str | None = None,
          check_every_hours: float | None = None, rules_every_days: float | None = None):
    httpd = make_server(ctx, host, port, certfile, keyfile)
    stop = threading.Event()
    ctx.check_every, ctx.rules_every = check_every_hours, rules_every_days
    for loop in (_checker_loop, _rules_loop):  # both idle while their interval is off
        threading.Thread(target=loop, args=(ctx, stop), daemon=True).start()
    check_every_hours, rules_every_days = ctx.interval("check_every"), ctx.interval("rules_every")

    def on_term(signum, frame):  # `docker stop` / systemd send SIGTERM
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)

    scheme = "https" if certfile else "http"
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::", "") else host
    public = host not in ("127.0.0.1", "localhost", "::1")
    log(f"HawkSense {__version__} listening on {scheme}://{shown}:{port}{ctx.base_path}/  (db: {ctx.db_path})")
    if ctx.token:
        log("access token required - open the app once with ?token=<your token>")
    elif public:
        log("WARNING: reachable from the network without an access token - set --token / HAWKSENSE_TOKEN")
    if check_every_hours:
        log(f"automatic price checks every {check_every_hours:g} h")
    if rules_every_days:
        log(f"checking for tax/forwarder rule updates every {rules_every_days:g} days")
    if public and not certfile:
        log("note: iOS enables offline mode only over HTTPS - put a TLS proxy in front (see README)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        stop.set()
        httpd.server_close()
