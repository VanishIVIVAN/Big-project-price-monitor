"""SQLite storage for users, products, offers, price history and wishlists."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .domain import (
    Notification,
    Offer,
    PriceChange,
    Product,
    ProductSummary,
    User,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    brand TEXT NOT NULL,
    search_query TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offers (
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    store_id TEXT NOT NULL,
    store_name TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    price INTEGER NOT NULL,
    old_price INTEGER,
    in_stock INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (product_id, store_id)
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    store_id TEXT NOT NULL,
    price INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history ON price_history(product_id, store_id, id);
CREATE TABLE IF NOT EXISTS wishlist (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, product_id)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0
);
"""


class StorageError(Exception):
    """Base class for storage problems."""


class UserExistsError(StorageError):
    """A user with this name is already registered."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _product(row: sqlite3.Row) -> Product:
    return Product(
        id=row["id"], key=row["key"], title=row["title"],
        category=row["category"], brand=row["brand"],
        search_query=row["search_query"],
    )


class Storage:
    """Thin repository over SQLite. Each call uses its own connection,
    so the object is safe to share between web and monitor threads."""

    def __init__(self, path: str, history_interval: timedelta = timedelta(hours=6)):
        self.path = path
        self.history_interval = history_interval
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- users -------------------------------------------------------
    def create_user(self, username: str, password_hash: str, email: str) -> User:
        try:
            with self._connect() as db:
                cur = db.execute(
                    "INSERT INTO users(username, password_hash, email) VALUES (?,?,?)",
                    (username, password_hash, email),
                )
                return User(cur.lastrowid, username, email)
        except sqlite3.IntegrityError as exc:
            raise UserExistsError("Користувач з таким іменем уже існує.") from exc

    def find_user(self, username: str) -> tuple[User, str] | None:
        """Return (user, password_hash) or None."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        if row is None:
            return None
        return User(row["id"], row["username"], row["email"]), row["password_hash"]

    def get_user(self, user_id: int) -> User | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User(row["id"], row["username"], row["email"]) if row else None

    # ---- products and offers ----------------------------------------
    def upsert_product(
        self, key: str, title: str, category: str, brand: str, search_query: str
    ) -> Product:
        with self._connect() as db:
            db.execute(
                "INSERT INTO products(key, title, category, brand, search_query) "
                "VALUES (?,?,?,?,?) ON CONFLICT(key) DO NOTHING",
                (key, title, category, brand, search_query),
            )
            row = db.execute("SELECT * FROM products WHERE key = ?", (key,)).fetchone()
        return _product(row)

    def get_product(self, product_id: int) -> Product | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM products WHERE id = ?", (product_id,)
            ).fetchone()
        return _product(row) if row else None

    def save_offers(
        self, product_id: int, offers: list[Offer], now: datetime | None = None
    ) -> list[PriceChange]:
        """Store current offers, extend history and report price drops."""
        now = now or _now()
        changes: list[PriceChange] = []
        with self._connect() as db:
            previous = {
                r["store_id"]: r["price"]
                for r in db.execute(
                    "SELECT store_id, price FROM offers WHERE product_id = ?",
                    (product_id,),
                )
            }
            for offer in offers:
                db.execute(
                    "INSERT INTO offers(product_id, store_id, store_name, title, url,"
                    " price, old_price, in_stock, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(product_id, store_id) DO UPDATE SET "
                    "store_name=excluded.store_name, title=excluded.title,"
                    " url=excluded.url, price=excluded.price,"
                    " old_price=excluded.old_price, in_stock=excluded.in_stock,"
                    " updated_at=excluded.updated_at",
                    (product_id, offer.store_id, offer.store_name, offer.title,
                     offer.url, offer.price, offer.old_price, int(offer.in_stock),
                     now.isoformat()),
                )
                self._append_history(db, product_id, offer, now)
                old = previous.get(offer.store_id)
                if old is not None and offer.price < old:
                    changes.append(PriceChange(
                        offer.store_id, offer.store_name, old, offer.price, offer.url
                    ))
        return changes

    def _append_history(
        self, db: sqlite3.Connection, product_id: int, offer: Offer, now: datetime
    ) -> None:
        last = db.execute(
            "SELECT price, recorded_at FROM price_history "
            "WHERE product_id = ? AND store_id = ? ORDER BY id DESC LIMIT 1",
            (product_id, offer.store_id),
        ).fetchone()
        if last is not None and last["price"] == offer.price:
            age = now - datetime.fromisoformat(last["recorded_at"])
            if age < self.history_interval:
                return
        db.execute(
            "INSERT INTO price_history(product_id, store_id, price, recorded_at) "
            "VALUES (?,?,?,?)",
            (product_id, offer.store_id, offer.price, now.isoformat()),
        )

    def get_offers(self, product_id: int) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM offers WHERE product_id = ? ORDER BY price", (product_id,)
            ).fetchall()
        return [
            {**dict(r), "in_stock": bool(r["in_stock"]),
             "updated_at": datetime.fromisoformat(r["updated_at"])}
            for r in rows
        ]

    def get_history(self, product_id: int) -> dict[str, list[tuple[datetime, int]]]:
        """Price history grouped by store name."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT COALESCE(o.store_name, h.store_id) AS store_name,"
                " h.price, h.recorded_at FROM price_history h "
                "LEFT JOIN offers o ON o.product_id = h.product_id"
                " AND o.store_id = h.store_id "
                "WHERE h.product_id = ? ORDER BY h.recorded_at, h.id",
                (product_id,),
            ).fetchall()
        history: dict[str, list[tuple[datetime, int]]] = {}
        for row in rows:
            history.setdefault(row["store_name"], []).append(
                (datetime.fromisoformat(row["recorded_at"]), row["price"])
            )
        return history

    # ---- wishlist ----------------------------------------------------
    def add_to_wishlist(self, user_id: int, product_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO wishlist(user_id, product_id) VALUES (?,?)",
                (user_id, product_id),
            )

    def remove_from_wishlist(self, user_id: int, product_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM wishlist WHERE user_id = ? AND product_id = ?",
                (user_id, product_id),
            )

    def is_in_wishlist(self, user_id: int, product_id: int) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM wishlist WHERE user_id = ? AND product_id = ?",
                (user_id, product_id),
            ).fetchone()
        return row is not None

    def list_wishlist(self, user_id: int) -> list[ProductSummary]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT p.*, "
                " (SELECT MIN(price) FROM offers o WHERE o.product_id = p.id"
                "  AND o.in_stock = 1) AS min_price,"
                " (SELECT COUNT(*) FROM offers o WHERE o.product_id = p.id) AS stores "
                "FROM wishlist w JOIN products p ON p.id = w.product_id "
                "WHERE w.user_id = ? ORDER BY p.title",
                (user_id,),
            ).fetchall()
        return [ProductSummary(_product(r), r["min_price"], r["stores"]) for r in rows]

    def wishlisted_products(self) -> list[Product]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT p.* FROM products p JOIN wishlist w"
                " ON w.product_id = p.id"
            ).fetchall()
        return [_product(r) for r in rows]

    def users_watching(self, product_id: int) -> list[User]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT u.* FROM users u JOIN wishlist w ON w.user_id = u.id "
                "WHERE w.product_id = ?",
                (product_id,),
            ).fetchall()
        return [User(r["id"], r["username"], r["email"]) for r in rows]

    # ---- notifications ----------------------------------------------
    def add_notification(self, user_id: int, product_id: int, message: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO notifications(user_id, product_id, message, created_at)"
                " VALUES (?,?,?,?)",
                (user_id, product_id, message, _now().isoformat()),
            )

    def list_notifications(self, user_id: int, limit: int = 100) -> list[Notification]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM notifications WHERE user_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [
            Notification(r["id"], r["product_id"], r["message"],
                         datetime.fromisoformat(r["created_at"]), bool(r["is_read"]))
            for r in rows
        ]

    def count_unread(self, user_id: int) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND is_read = 0",
                (user_id,),
            ).fetchone()
        return row["n"]

    def mark_notifications_read(self, user_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user_id,)
            )
