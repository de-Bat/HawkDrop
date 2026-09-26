"""SQLite storage for items, offers, price history and FX cache."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from hawkdrop.forwarders import Account

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
        self.conn = sqlite3.connect(self.path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets the web server, the scheduled checker and CLI commands (e.g. `docker exec
        # hawkdrop hawkdrop check`) read and write concurrently without "database is locked"
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
                    weight_kg: float | None = None, dims: str | None = None):
        """Set the given fields; ``None`` leaves a field unchanged (see ``clear_item_field``)."""
        with self.conn:
            for col, value in (("category", category), ("target_price", target_price),
                               ("weight_kg", weight_kg), ("dims", dims)):
                if value is not None:
                    self.conn.execute(f"UPDATE items SET {col} = ? WHERE id = ?", (value, item.id))

    def clear_item_field(self, item: Item, col: str):
        if col not in ("target_price", "weight_kg", "dims"):
            raise ValueError(col)
        with self.conn:
            self.conn.execute(f"UPDATE items SET {col} = NULL WHERE id = ?", (item.id,))

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
        return Item(**dict(row)) if row else None

    def list_items(self) -> list[Item]:
        return [Item(**dict(r)) for r in self.conn.execute("SELECT * FROM items ORDER BY id")]

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
