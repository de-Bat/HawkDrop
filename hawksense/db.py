"""SQLite storage for items, offers, price history and FX cache."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from hawksense.forwarders import Account

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL DEFAULT 'default',
    target_price REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    store TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    shipping REAL,
    shipping_currency TEXT,
    price_regex TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (item_id, store, url)
);
CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    shipping REAL,
    in_stock INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'manual',
    client_id TEXT
);
CREATE INDEX IF NOT EXISTS prices_offer_ts ON prices (offer_id, ts);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS forwarder_accounts (
    id INTEGER PRIMARY KEY,
    forwarder TEXT NOT NULL,
    warehouse TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    suite TEXT NOT NULL DEFAULT '',
    sales_tax REAL,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (forwarder, warehouse)
);
CREATE TABLE IF NOT EXISTS spec_observations (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    weight_kg REAL,
    dims TEXT,
    weight_kind TEXT NOT NULL DEFAULT 'item',
    dims_kind TEXT NOT NULL DEFAULT 'item',
    ts TEXT NOT NULL,
    UNIQUE (item_id, url)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    dedup TEXT UNIQUE,
    item_id INTEGER,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    url TEXT,
    read INTEGER NOT NULL DEFAULT 0,
    deliveries TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS subscriptions (
    event TEXT NOT NULL,
    channel TEXT NOT NULL,
    PRIMARY KEY (event, channel)
);
CREATE TABLE IF NOT EXISTS rule_layers (
    layer TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_changes (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    layer TEXT NOT NULL,
    source TEXT NOT NULL,
    path TEXT NOT NULL,
    old TEXT,
    new TEXT,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS offer_details (
    offer_id INTEGER PRIMARY KEY REFERENCES offers(id) ON DELETE CASCADE,
    title TEXT,
    image_url TEXT,
    image_key TEXT,
    description TEXT,
    condition TEXT,
    availability TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS images (
    key TEXT PRIMARY KEY,
    mime TEXT NOT NULL,
    data BLOB NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fx (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rates TEXT NOT NULL,
    ts TEXT NOT NULL
);
"""


@dataclass
class Item:
    id: int
    name: str
    category: str
    target_price: float | None
    created_at: str
    weight_kg: float | None = None  # shipping weight, for forwarders' rate cards
    dims: str | None = None  # boxed size "LxWxH" in cm, for volumetric weight
    weight_source: str | None = None  # "manual" (set by you, never overwritten) or "auto" (from store pages)
    dims_source: str | None = None
    specs_status: str | None = None  # last consensus: verified | unverified | conflict | missing
    muted: bool = False  # no notifications about this item


@dataclass
class Offer:
    id: int
    item_id: int
    store: str
    url: str
    shipping: float | None
    shipping_currency: str | None
    price_regex: str | None
    active: bool
    local_shipping: float | None = None  # store's shipping to a forwarder's warehouse (shipping_currency)


@dataclass
class PricePoint:
    offer_id: int
    ts: datetime
    price: float
    currency: str
    shipping: float | None
    in_stock: bool
    source: str


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if str(self.path) != ":memory:":
            # your items, addresses and settings: readable by you only (-wal/-shm inherit this mode)
            if not self.path.exists():
                os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
            elif self.path.stat().st_mode & 0o077:
                try:
                    os.chmod(self.path, 0o600)  # created by an older version
                except OSError:
                    pass
        self.conn = sqlite3.connect(self.path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets the web server, the scheduled checker and CLI commands (e.g. `docker exec
        # hawksense hawksense check`) read and write concurrently without "database is locked"
        if str(self.path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self):
        cols = self._columns("prices")
        item_cols = self._columns("items")
        offer_cols = self._columns("offers")
        with self.conn:
            if "client_id" not in cols:
                self.conn.execute("ALTER TABLE prices ADD COLUMN client_id TEXT")
            if "weight_kg" not in item_cols:
                self.conn.execute("ALTER TABLE items ADD COLUMN weight_kg REAL")
            if "dims" not in item_cols:
                self.conn.execute("ALTER TABLE items ADD COLUMN dims TEXT")
            if "local_shipping" not in offer_cols:
                self.conn.execute("ALTER TABLE offers ADD COLUMN local_shipping REAL")
            for col, decl in (("weight_source", "TEXT"), ("dims_source", "TEXT"), ("specs_status", "TEXT"),
                              ("muted", "INTEGER NOT NULL DEFAULT 0")):
                if col not in item_cols:
                    self.conn.execute(f"ALTER TABLE items ADD COLUMN {col} {decl}")
            # lets offline clients replay queued price entries without creating duplicates
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS prices_client_id ON prices (client_id)"
                              " WHERE client_id IS NOT NULL")

    def close(self):
        self.conn.close()

    def backup(self, target: str | Path):
        """Consistent snapshot of the database, safe while other processes are writing."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
        dst = sqlite3.connect(tmp)
        try:
            self.conn.backup(dst)
        finally:
            dst.close()
        tmp.replace(target)

    def healthy(self) -> bool:
        return self.conn.execute("SELECT 1").fetchone()[0] == 1

    # ---- items -------------------------------------------------------------
    def add_item(self, name: str, category: str = "default", target_price: float | None = None) -> Item:
        with self.conn:
            self.conn.execute(
                "INSERT INTO items (name, category, target_price, created_at) VALUES (?, ?, ?, ?)",
                (name, category, target_price, now_utc().isoformat()),
            )
        return self.get_item(name)

    def update_item(self, item: Item, category: str | None = None, target_price: float | None = None,
                    weight_kg: float | None = None, dims: str | None = None, source: str = "manual",
                    muted: bool | None = None):
        """Set the given fields; ``None`` leaves a field unchanged (see ``clear_item_field``).

        A weight or size set with ``source="manual"`` is never replaced by values read from store pages.
        """
        with self.conn:
            for col, value in (("category", category), ("target_price", target_price),
                               ("weight_kg", weight_kg), ("dims", dims)):
                if value is not None:
                    self.conn.execute(f"UPDATE items SET {col} = ? WHERE id = ?", (value, item.id))
            if weight_kg is not None:
                self.conn.execute("UPDATE items SET weight_source = ? WHERE id = ?", (source, item.id))
            if dims is not None:
                self.conn.execute("UPDATE items SET dims_source = ? WHERE id = ?", (source, item.id))
            if muted is not None:
                self.conn.execute("UPDATE items SET muted = ? WHERE id = ?", (int(muted), item.id))

    def set_specs_status(self, item: Item, status: str | None):
        with self.conn:
            self.conn.execute("UPDATE items SET specs_status = ? WHERE id = ?", (status, item.id))

    def clear_item_field(self, item: Item, col: str):
        """Clearing a weight/size hands it back to automatic detection."""
        if col not in ("target_price", "weight_kg", "dims"):
            raise ValueError(col)
        source_col = {"weight_kg": "weight_source", "dims": "dims_source"}.get(col)
        with self.conn:
            self.conn.execute(f"UPDATE items SET {col} = NULL WHERE id = ?", (item.id,))
            if source_col:
                self.conn.execute(f"UPDATE items SET {source_col} = NULL WHERE id = ?", (item.id,))

    def get_item(self, ref: str | int) -> Item | None:
        if isinstance(ref, int) or str(ref).isdigit():
            row = self.conn.execute("SELECT * FROM items WHERE id = ?", (int(ref),)).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM items WHERE name = ? COLLATE NOCASE", (ref,)).fetchone()
            if row is None:
                rows = self.conn.execute(
                    "SELECT * FROM items WHERE name LIKE ? COLLATE NOCASE", (f"%{ref}%",)
                ).fetchall()
                row = rows[0] if len(rows) == 1 else None
        return self._item(row) if row else None

    def list_items(self) -> list[Item]:
        return [self._item(r) for r in self.conn.execute("SELECT * FROM items ORDER BY id")]

    @staticmethod
    def _item(row) -> Item:
        d = dict(row)
        d["muted"] = bool(d.get("muted"))
        return Item(**d)

    def delete_item(self, item: Item):
        with self.conn:
            self.conn.execute("DELETE FROM items WHERE id = ?", (item.id,))

    # ---- offers ------------------------------------------------------------
    def add_offer(self, item: Item, store: str, url: str = "", shipping: float | None = None,
                  shipping_currency: str | None = None, price_regex: str | None = None,
                  local_shipping: float | None = None) -> Offer:
        with self.conn:
            self.conn.execute(
                """INSERT INTO offers (item_id, store, url, shipping, shipping_currency, price_regex, local_shipping)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (item_id, store, url) DO UPDATE SET
                     shipping = COALESCE(excluded.shipping, shipping),
                     shipping_currency = COALESCE(excluded.shipping_currency, shipping_currency),
                     price_regex = COALESCE(excluded.price_regex, price_regex),
                     local_shipping = COALESCE(excluded.local_shipping, local_shipping),
                     active = 1""",
                (item.id, store, url, shipping, shipping_currency, price_regex, local_shipping),
            )
        row = self.conn.execute(
            "SELECT * FROM offers WHERE item_id = ? AND store = ? AND url = ?", (item.id, store, url)
        ).fetchone()
        return self._offer(row)

    def offers(self, item: Item, active_only: bool = True) -> list[Offer]:
        q = "SELECT * FROM offers WHERE item_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY id"
        return [self._offer(r) for r in self.conn.execute(q, (item.id,))]

    def find_offer(self, item: Item, store: str) -> Offer | None:
        row = self.conn.execute(
            "SELECT * FROM offers WHERE item_id = ? AND store = ? ORDER BY id LIMIT 1", (item.id, store)
        ).fetchone()
        return self._offer(row) if row else None

    def deactivate_offer(self, offer_id: int):
        with self.conn:
            self.conn.execute("UPDATE offers SET active = 0 WHERE id = ?", (offer_id,))

    @staticmethod
    def _offer(row) -> Offer:
        d = dict(row)
        d["active"] = bool(d["active"])
        return Offer(**d)

    # ---- product details (title, image, description, condition, availability) ----------
    def save_offer_details(self, offer: Offer, details) -> None:
        """Keep what the page said; a field it left out this time keeps its last known value,
        except availability, which is only as good as the latest check."""
        with self.conn:
            self.conn.execute(
                """INSERT INTO offer_details (offer_id, title, image_url, description, condition, availability, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (offer_id) DO UPDATE SET
                     title = COALESCE(excluded.title, title),
                     image_key = CASE WHEN excluded.image_url IS NOT NULL AND excluded.image_url IS NOT image_url
                                      THEN NULL ELSE image_key END,
                     image_url = COALESCE(excluded.image_url, image_url),
                     description = COALESCE(excluded.description, description),
                     condition = COALESCE(excluded.condition, condition),
                     availability = excluded.availability, ts = excluded.ts""",
                (offer.id, details.title, details.image, details.description, details.condition,
                 details.availability, now_utc().isoformat()))

    def offer_details(self, item: Item) -> dict[int, dict]:
        rows = self.conn.execute(
            "SELECT d.* FROM offer_details d JOIN offers o ON o.id = d.offer_id WHERE o.item_id = ?", (item.id,))
        return {r["offer_id"]: dict(r) for r in rows}

    def image_to_fetch(self, offer_id: int) -> str | None:
        """The offer's image URL when it hasn't been downloaded yet."""
        row = self.conn.execute("SELECT image_url FROM offer_details WHERE offer_id = ? AND image_key IS NULL",
                                (offer_id,)).fetchone()
        return row["image_url"] if row else None

    def set_offer_image(self, offer_id: int, key: str, mime: str, data: bytes) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO images (key, mime, data, ts) VALUES (?, ?, ?, ?)",
                              (key, mime, data, now_utc().isoformat()))
            self.conn.execute("UPDATE offer_details SET image_key = ? WHERE offer_id = ?", (key, offer_id))
            # images no offer shows any more
            self.conn.execute("DELETE FROM images WHERE key NOT IN "
                              "(SELECT image_key FROM offer_details WHERE image_key IS NOT NULL)")

    def image(self, key: str) -> tuple[str, bytes] | None:
        row = self.conn.execute("SELECT mime, data FROM images WHERE key = ?", (key,)).fetchone()
        return (row["mime"], bytes(row["data"])) if row else None

    # ---- prices ------------------------------------------------------------
    def add_price(self, offer: Offer, price: float, currency: str, shipping: float | None = None,
                  in_stock: bool = True, source: str = "manual", ts: datetime | None = None,
                  client_id: str | None = None) -> bool:
        """Returns False when ``client_id`` was already recorded (a replayed offline entry)."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO prices (offer_id, ts, price, currency, shipping, in_stock, source, client_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (offer.id, (ts or now_utc()).isoformat(), price, currency.upper(), shipping, int(in_stock), source,
                 client_id),
            )
        return cur.rowcount == 1

    def prices(self, offer: Offer) -> list[PricePoint]:
        rows = self.conn.execute("SELECT * FROM prices WHERE offer_id = ? ORDER BY ts", (offer.id,))
        return [
            PricePoint(r["offer_id"], datetime.fromisoformat(r["ts"]), r["price"], r["currency"], r["shipping"],
                       bool(r["in_stock"]), r["source"])
            for r in rows
        ]

    # ---- forwarder accounts ------------------------------------------------------
    def save_account(self, forwarder: str, warehouse: str, address: str = "", suite: str = "",
                     sales_tax: float | None = None) -> Account:
        with self.conn:
            self.conn.execute(
                """INSERT INTO forwarder_accounts (forwarder, warehouse, address, suite, sales_tax)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (forwarder, warehouse) DO UPDATE SET
                     address = excluded.address, suite = excluded.suite, sales_tax = excluded.sales_tax,
                     active = 1""",
                (forwarder, warehouse.upper(), address.strip(), suite.strip(), sales_tax),
            )
        return next(a for a in self.accounts() if a.forwarder == forwarder and a.warehouse == warehouse.upper())

    def accounts(self) -> list[Account]:
        rows = self.conn.execute("SELECT * FROM forwarder_accounts WHERE active = 1 ORDER BY forwarder, warehouse")
        return [Account(**{**dict(r), "active": bool(r["active"])}) for r in rows]

    def delete_accounts(self, forwarder: str, warehouse: str | None = None) -> int:
        with self.conn:
            if warehouse:
                cur = self.conn.execute("DELETE FROM forwarder_accounts WHERE forwarder = ? AND warehouse = ?",
                                        (forwarder, warehouse.upper()))
            else:
                cur = self.conn.execute("DELETE FROM forwarder_accounts WHERE forwarder = ?", (forwarder,))
        return cur.rowcount

    # ---- item specs (weight / size read from store pages) ----------------------------
    def save_spec_observation(self, item: Item, source: str, url: str, weight_kg: float | None, dims: str | None,
                              weight_kind: str = "item", dims_kind: str = "item"):
        with self.conn:
            self.conn.execute(
                """INSERT INTO spec_observations (item_id, source, url, weight_kg, dims, weight_kind, dims_kind, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (item_id, url) DO UPDATE SET source = excluded.source,
                     weight_kg = excluded.weight_kg, dims = excluded.dims, weight_kind = excluded.weight_kind,
                     dims_kind = excluded.dims_kind, ts = excluded.ts""",
                (item.id, source, url, weight_kg, dims, weight_kind, dims_kind, now_utc().isoformat()),
            )

    def spec_observations(self, item: Item) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM spec_observations WHERE item_id = ? ORDER BY id", (item.id,))
        return [dict(r) for r in rows]

    # ---- notifications -------------------------------------------------------------
    def add_notification(self, event: str, title: str, body: str = "", dedup: str | None = None,
                         item_id: int | None = None, url: str | None = None) -> int | None:
        """Returns the new id, or None when ``dedup`` was already notified."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO notifications (ts, event, dedup, item_id, title, body, url)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now_utc().isoformat(), event, dedup, item_id, title, body, url),
            )
        return cur.lastrowid if cur.rowcount == 1 else None

    def set_deliveries(self, notification_id: int, deliveries: dict):
        with self.conn:
            self.conn.execute("UPDATE notifications SET deliveries = ? WHERE id = ?",
                              (json.dumps(deliveries), notification_id))

    def notifications(self, limit: int = 50, unread_only: bool = False) -> list[dict]:
        q = "SELECT * FROM notifications" + (" WHERE read = 0" if unread_only else "") + " ORDER BY id DESC LIMIT ?"
        out = []
        for r in self.conn.execute(q, (limit,)):
            d = dict(r)
            d["read"], d["deliveries"] = bool(d["read"]), json.loads(d["deliveries"] or "{}")
            out.append(d)
        return out

    def mark_read(self, ids: list[int] | None = None):
        with self.conn:
            if ids is None:
                self.conn.execute("UPDATE notifications SET read = 1")
            else:
                self.conn.executemany("UPDATE notifications SET read = 1 WHERE id = ?", [(i,) for i in ids])

    def subscriptions(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for r in self.conn.execute("SELECT event, channel FROM subscriptions"):
            out.setdefault(r["event"], set()).add(r["channel"])
        return out

    def set_subscriptions(self, event: str, channels: list[str]):
        with self.conn:
            self.conn.execute("DELETE FROM subscriptions WHERE event = ?", (event,))
            self.conn.executemany("INSERT INTO subscriptions (event, channel) VALUES (?, ?)",
                                  [(event, c) for c in dict.fromkeys(channels)])

    # ---- rules (taxes, forwarder rates): fetched and manual layers --------------------------
    def rule_layer(self, layer: str) -> dict:
        row = self.conn.execute("SELECT data FROM rule_layers WHERE layer = ?", (layer,)).fetchone()
        return json.loads(row["data"]) if row else {}

    def rule_layer_updated(self, layer: str) -> str | None:
        row = self.conn.execute("SELECT updated_at FROM rule_layers WHERE layer = ?", (layer,)).fetchone()
        return row["updated_at"] if row else None

    def save_rule_layer(self, layer: str, data: dict):
        with self.conn:
            self.conn.execute(
                "INSERT INTO rule_layers (layer, data, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (layer) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                (layer, json.dumps(data, sort_keys=True), now_utc().isoformat()),
            )

    def log_rule_change(self, layer: str, source: str, path: str, old, new, status: str, note: str = "") -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO rule_changes (ts, layer, source, path, old, new, status, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now_utc().isoformat(), layer, source, path, json.dumps(old), json.dumps(new), status, note),
            )
        return cur.lastrowid

    def rule_changes(self, status: str | None = None, limit: int = 100) -> list[dict]:
        q = "SELECT * FROM rule_changes" + (" WHERE status = ?" if status else "") + " ORDER BY id DESC LIMIT ?"
        rows = self.conn.execute(q, (status, limit) if status else (limit,))
        return [{**dict(r), "old": json.loads(r["old"]), "new": json.loads(r["new"])} for r in rows]

    def set_rule_change_status(self, change_id: int, status: str):
        with self.conn:
            self.conn.execute("UPDATE rule_changes SET status = ? WHERE id = ?", (status, change_id))

    # ---- small settings store -------------------------------------------------
    def get_kv(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str):
        with self.conn:
            self.conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)"
                              " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    # ---- fx ------------------------------------------------------------------
    def get_fx(self) -> tuple[dict[str, float], datetime] | None:
        row = self.conn.execute("SELECT rates, ts FROM fx WHERE id = 1").fetchone()
        if not row:
            return None
        return json.loads(row["rates"]), datetime.fromisoformat(row["ts"])

    def save_fx(self, rates: dict[str, float]):
        with self.conn:
            self.conn.execute(
                "INSERT INTO fx (id, rates, ts) VALUES (1, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET rates = excluded.rates, ts = excluded.ts",
                (json.dumps(rates), now_utc().isoformat()),
            )
