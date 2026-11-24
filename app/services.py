"""Business logic: search with filters, price tracking and background monitor."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Mapping

from .domain import (
    CATEGORIES, Offer, PriceChange, Product, build_search_query, detect_brand,
    format_money, is_relevant, model_key,
)
from .notifications import Notifier
from .storage import Storage
from .stores import Store, StoreError

MAX_QUERY_LENGTH = 80


class ValidationError(ValueError):
    """User input is invalid; the message is safe to show to the user."""


@dataclass(frozen=True)
class ProductGroup:
    """One model with its offers from all stores."""

    product_id: int
    key: str
    title: str
    category: str
    brand: str
    offers: list[Offer]

    @property
    def min_price(self) -> int:
        in_stock = [o.price for o in self.offers if o.in_stock]
        return min(in_stock or [o.price for o in self.offers])

    @property
    def store_count(self) -> int:
        return len(self.offers)


@dataclass(frozen=True)
class SearchResult:
    groups: list[ProductGroup]
    errors: dict[str, str] = field(default_factory=dict)  # store name -> problem


@dataclass(frozen=True)
class SearchFilters:
    brand: str = ""
    min_price: int | None = None
    max_price: int | None = None
    phrases: tuple[str, ...] = ()
    only_in_stock: bool = False


def _parse_price(raw: str, label: str) -> int | None:
    raw = raw.replace(" ", "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ValidationError(f"{label}: введіть ціле невід'ємне число.")
    return int(raw)


def parse_filters(args: Mapping[str, str]) -> SearchFilters:
    """Build filters from query-string values, raising ValidationError."""
    low = _parse_price(args.get("min_price", ""), "Ціна від")
    high = _parse_price(args.get("max_price", ""), "Ціна до")
    if low is not None and high is not None and low > high:
        raise ValidationError("Мінімальна ціна більша за максимальну.")
    phrases = tuple(
        p.strip().lower() for p in args.get("keywords", "").split(",") if p.strip()
    )
    return SearchFilters(
        brand=args.get("brand", "").strip(),
        min_price=low,
        max_price=high,
        phrases=phrases,
        only_in_stock=args.get("only_in_stock") == "1",
    )


def apply_filters(groups: list[ProductGroup], filters: SearchFilters) -> list[ProductGroup]:
    """Keep only the models that match every filter."""
    result = []
    for group in groups:
        if filters.brand and group.brand != filters.brand:
            continue
        if filters.min_price is not None and group.min_price < filters.min_price:
            continue
        if filters.max_price is not None and group.min_price > filters.max_price:
            continue
        if filters.only_in_stock and not any(o.in_stock for o in group.offers):
            continue
        text = " ".join(f"{o.title} {o.details}" for o in group.offers).lower()
        if not all(p in text for p in filters.phrases):
            continue
        result.append(group)
    return result


class PriceTracker:
    """Saves offers and notifies users when a price drops."""

    def __init__(self, storage: Storage, notifier: Notifier, min_drop_percent: float = 1):
        self.storage = storage
        self.notifier = notifier
        self.min_drop_percent = min_drop_percent

    def record(self, product: Product, offers: list[Offer]) -> list[PriceChange]:
        changes = self.storage.save_offers(product.id, offers)
        for change in changes:
            if change.drop_percent < self.min_drop_percent:
                continue
            message = (
                f"{product.title}: у магазині {change.store_name} ціна впала з "
                f"{format_money(change.old_price)} до {format_money(change.new_price)} "
                f"(−{change.drop_percent:.0f}%)."
            )
            for user in self.storage.users_watching(product.id):
                self.notifier.send(user, product.id, message)
        return changes


def _query_stores(stores: list[Store], query: str) -> tuple[list[Offer], dict[str, str]]:
    """Ask all stores in parallel. A broken store never breaks the whole search."""

    def ask(store: Store) -> tuple[Store, list[Offer], str | None]:
        try:
            return store, store.search(query), None
        except StoreError as exc:
            return store, [], str(exc)
        except Exception as exc:  # parser/network bugs must not crash the page
            return store, [], f"Неочікувана помилка: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, len(stores))) as pool:
        answers = list(pool.map(ask, stores))
    offers = [o for _, found, _ in answers for o in found]
    errors = {s.name: err for s, _, err in answers if err}
    return offers, errors


class SearchService:
    """Searches all stores, groups offers by model and stores the results."""

    def __init__(self, stores: list[Store], tracker: PriceTracker,
                 cache_seconds: int = 300):
        self.stores = stores
        self.tracker = tracker
        self.cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str], tuple[float, SearchResult]] = {}

    def search(self, category_id: str, text: str) -> SearchResult:
        category = CATEGORIES.get(category_id)
        if category is None:
            raise ValidationError("Невідома категорія товарів.")
        text = " ".join(text.split())
        if len(text) > MAX_QUERY_LENGTH:
            raise ValidationError(f"Запит задовгий: не більше {MAX_QUERY_LENGTH} символів.")
        cache_key = (category_id, text.lower())
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]

        offers, errors = _query_stores(self.stores, f"{category.query_word} {text}".strip())
        tokens = [t for t in text.lower().split() if len(t) > 1]
        by_key: dict[str, list[Offer]] = {}
        for offer in offers:
            title = offer.title.lower()
            if is_relevant(offer.title, category) and all(t in title for t in tokens):
                by_key.setdefault(model_key(offer.title, category.id), []).append(offer)

        groups = []
        for key, found in by_key.items():
            title = min((o.title for o in found), key=len)
            product = self.tracker.storage.upsert_product(
                key, title, category.id, detect_brand(title), build_search_query(title)
            )
            self.tracker.record(product, found)
            groups.append(ProductGroup(product.id, key, title, category.id,
                                       product.brand, found))
        groups.sort(key=lambda g: (-g.store_count, g.min_price))
        result = SearchResult(groups, errors)
        self._cache[cache_key] = (time.monotonic(), result)
        return result


class PriceMonitor:
    """Re-checks tracked products, on demand and periodically in a thread."""

    def __init__(self, storage: Storage, stores: list[Store], tracker: PriceTracker,
                 interval_minutes: int = 360):
        self.storage = storage
        self.stores = stores
        self.tracker = tracker
        self.interval_seconds = interval_minutes * 60
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def refresh_product(self, product: Product) -> dict[str, str]:
        """Fetch current prices of one product. Returns problems per store."""
        found, errors = _query_stores(self.stores, product.search_query)
        offers = [o for o in found if model_key(o.title, product.category) == product.key]
        self.tracker.record(product, offers)
        return errors

    def refresh_wishlisted(self) -> int:
        """Refresh every product that someone has in the wishlist."""
        products = self.storage.wishlisted_products()
        for product in products:
            self.refresh_product(product)
        return len(products)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="price-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.refresh_wishlisted()
                self.last_error = ""
            except Exception as exc:  # one failed cycle must not kill the thread
                self.last_error = str(exc)
