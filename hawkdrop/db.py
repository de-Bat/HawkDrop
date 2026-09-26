"""SQLite storage for items, offers, price history and FX cache."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(prices)")}
        with self.conn:
            if "client_id" not in cols:
                self.conn.execute("ALTER TABLE prices ADD COLUMN client_id TEXT")
            # lets offline clients replay queued price entries without creating duplicates
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS prices_client_id ON prices (client_id)"
                              " WHERE client_id IS NOT NULL")

    def close(self):
        self.conn.close()

    # ---- items -------------------------------------------------------------
    def add_item(self, name: str, category: str = "default", target_price: float | None = None) -> Item:
        with self.conn:
            self.conn.execute(
                "INSERT INTO items (name, category, target_price, created_at) VALUES (?, ?, ?, ?)",
                (name, category, target_price, now_utc().isoformat()),
            )
        return self.get_item(name)

    def update_item(self, item: Item, category: str | None = None, target_price: float | None = None):
        with self.conn:
            if category is not None:
                self.conn.execute("UPDATE items SET category = ? WHERE id = ?", (category, item.id))
            if target_price is not None:
                self.conn.execute("UPDATE items SET target_price = ? WHERE id = ?", (target_price, item.id))

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
                  shipping_currency: str | None = None, price_regex: str | None = None) -> Offer:
        with self.conn:
            self.conn.execute(
                """INSERT INTO offers (item_id, store, url, shipping, shipping_currency, price_regex)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (item_id, store, url) DO UPDATE SET
                     shipping = COALESCE(excluded.shipping, shipping),
                     shipping_currency = COALESCE(excluded.shipping_currency, shipping_currency),
                     price_regex = COALESCE(excluded.price_regex, price_regex),
                     active = 1""",
                (item.id, store, url, shipping, shipping_currency, price_regex),
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
