"""HawkDrop web server: JSON API + the installable web client (PWA).

Standard library only. Every request opens its own SQLite connection, so the
threaded server is safe. Run with ``hawkdrop serve``.
"""

from __future__ import annotations

import json
import mimetypes
import re
import ssl
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from hawkdrop import api
from hawkdrop.config import Config
from hawkdrop.currency import FX
from hawkdrop.db import Database
from hawkdrop.demo import seed_demo
from hawkdrop.landed import destination_from_config
from hawkdrop.tracker import Tracker

WEB_ROOT = Path(__file__).parent / "web"
MAX_BODY = 1 << 20

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class ServerContext:
    db_path: Path
    config: Config
    dest_code: str | None = None
    offline_fx: bool = False
    token: str | None = None

    def tracker(self) -> Tracker:
        db = Database(self.db_path)
        dest_cfg = {"code": self.dest_code} if self.dest_code else dict(self.config.destination)
        return Tracker(db, FX(db, offline=self.offline_fx or None), destination_from_config(dest_cfg),
                       Tracker.settings_from(self.config.advisor), self.config.stores)


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
        return {"ok": True}

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
                t.add_offer(item, url.strip())
        if body.get("check") and body.get("urls"):
            t.check(item)
        return api.item_detail(t, t.db.get_item(item.id))

    @staticmethod
    @route("PATCH", r"/api/items/(\d+)")
    def update_item(t, body, query, item_id):
        item = _item(t, item_id)
        t.db.update_item(item, _str(body, "category"), _num(body, "target_price"))
        if "target_price" in body and body["target_price"] in (None, ""):
            with t.db.conn:
                t.db.conn.execute("UPDATE items SET target_price = NULL WHERE id = ?", (item.id,))
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
        t.add_offer(item, _str(body, "url", required=True), _num(body, "shipping"),
                    _str(body, "shipping_currency"), _str(body, "regex"))
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
        t.record_price(item, _str(body, "store", required=True), _num(body, "price", required=True),
                       _str(body, "currency"), _num(body, "shipping"), body.get("in_stock", True) is not False,
                       when_d, _str(body, "client_id"))
        return api.item_detail(t, item)

    @staticmethod
    @route("POST", r"/api/items/(\d+)/check")
    def check_item(t, body, query, item_id):
        item = _item(t, item_id)
        results = api.check_results(t.check(item))
        return {"results": results, "item": api.item_detail(t, item)}

    @staticmethod
    @route("POST", r"/api/check")
    def check_all(t, body, query):
        return {"results": {i.id: api.check_results(t.check(i)) for i in t.db.list_items()}}

    @staticmethod
    @route("POST", r"/api/demo")
    def demo(t, body, query):
        item = seed_demo(t.db)
        return api.item_detail(t, item)


# ---- HTTP handler --------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "HawkDrop"
    ctx: ServerContext  # set by make_server
    quiet = False

    def log_message(self, fmt, *args):
        if not self.quiet:
            super().log_message(fmt, *args)

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _send(self, status: int, payload: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
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
        if url.path.startswith("/api/"):
            self._api(method, url)
        elif method == "GET":
            self._static(url.path, parse_qs(url.query))
        else:
            self._json(405, {"error": "method not allowed"})

    def _authorized(self, query: dict) -> bool:
        token = self.ctx.token
        if not token:
            return True
        given = self.headers.get("X-HawkDrop-Token") or query.get("token", [None])[0]
        return given == token

    def _api(self, method: str, url):
        query = parse_qs(url.query)
        if not self._authorized(query):
            return self._json(401, {"error": "invalid or missing token"})
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
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
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
            manifest["start_url"] = "/?" + urlencode({"token": query["token"][0]})
            body = json.dumps(manifest, ensure_ascii=False).encode()
        self._send(200, body, ctype, headers)


def _checker_loop(ctx: ServerContext, every_hours: float, stop: threading.Event):
    while not stop.wait(every_hours * 3600):
        t = ctx.tracker()
        try:
            for item in t.db.list_items():
                t.check(item)
            print(f"[hawkdrop] scheduled price check done at {time.strftime('%H:%M')}")
        except Exception:
            traceback.print_exc()
        finally:
            t.db.close()


def make_server(ctx: ServerContext, host: str = "127.0.0.1", port: int = 8765, certfile: str | None = None,
                keyfile: str | None = None, quiet: bool = False) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx, "quiet": quiet})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    if certfile:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(certfile, keyfile)
        httpd.socket = tls.wrap_socket(httpd.socket, server_side=True)
    return httpd


def serve(ctx: ServerContext, host: str, port: int, certfile: str | None = None, keyfile: str | None = None,
          check_every_hours: float | None = None):
    httpd = make_server(ctx, host, port, certfile, keyfile)
    stop = threading.Event()
    if check_every_hours:
        threading.Thread(target=_checker_loop, args=(ctx, check_every_hours, stop), daemon=True).start()
    scheme = "https" if certfile else "http"
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0", "") else host
    suffix = f"/?token={ctx.token}" if ctx.token else "/"
    print(f"HawkDrop is running at {scheme}://{shown}:{port}{suffix}")
    if host in ("0.0.0.0", ""):
        print("  listening on all interfaces - open it from your phone via this computer's address")
    if not certfile and host not in ("127.0.0.1", "localhost"):
        print("  note: iOS only enables offline mode (service worker) over HTTPS - see README")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
