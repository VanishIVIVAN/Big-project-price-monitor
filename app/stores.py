"""Store adapters.

A store adapter turns a search query into a list of offers. Real stores are
handled by one generic parser (HtmlCardStore) configured with a StoreSpec; it
does not depend on CSS class names: it finds links that look like product
pages, then takes the smallest block around each link that holds one product
and a price. DemoStore generates fake data for demos and tests.
"""
from __future__ import annotations

import random
import re
import time
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote_plus, urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from .config import Config, ConfigError
from .domain import Offer, clean_title

Fetcher = Callable[[str], str]

PRICE_RE = re.compile(
    r"(?P<sign>[-−–+]\s?)?"
    r"(?P<num>\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d{3,7})"
    r"\s*(?:₴|грн)"
    r"(?P<per>\s*(?:/|на|в)?\s*міс)?",
    re.IGNORECASE,
)
OUT_OF_STOCK_RE = re.compile(
    r"немає в наявності|нет в наличии|закінчився|тимчасово відсутній|очікується|ожидается",
    re.IGNORECASE,
)
MAX_CARD_TEXT = 1500  # a product card is never longer than this many characters


class StoreError(Exception):
    """The store could not be queried."""


class StoreBlockedError(StoreError):
    """The store refused automated access (captcha, 403, 429...)."""


class Store(ABC):
    """A shop that can be searched for offers."""

    id: str
    name: str

    @abstractmethod
    def search(self, query: str, limit: int = 40) -> list[Offer]:
        """Return offers found for the query. Raise StoreError on failure."""


class HttpFetcher:
    """Downloads a page with browser-like headers and maps failures to StoreError."""

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.6,en;q=0.5",
    }

    def __init__(self, timeout: int = 10, session: requests.Session | None = None):
        self.timeout = timeout
        self.session = session or requests.Session()

    def __call__(self, url: str) -> str:
        try:
            response = self.session.get(url, headers=self.HEADERS, timeout=self.timeout)
        except requests.RequestException as exc:
            raise StoreError(f"Не вдалося з'єднатися з магазином: {exc}") from exc
        if response.status_code in (401, 403, 429, 503):
            raise StoreBlockedError(
                f"Магазин відхилив запит (HTTP {response.status_code}). "
                "Ймовірно, спрацював захист від ботів."
            )
        if response.status_code != 200:
            raise StoreError(f"Магазин відповів HTTP {response.status_code}.")
        text = response.text
        if len(text) < 30000 and "captcha" in text.lower():
            raise StoreBlockedError("Магазин показав captcha.")
        return text


@dataclass(frozen=True)
class StoreSpec:
    """Everything the generic parser needs to know about one store."""

    id: str
    name: str
    search_url: str  # must contain {q}
    product_path: re.Pattern
    exclude: re.Pattern | None = None

    def matches(self, url: str) -> bool:
        """True if the URL looks like a product page of this store."""
        if self.exclude and self.exclude.search(url):
            return False
        return bool(self.product_path.search(urlparse(url).path))


def _re(pattern: str) -> re.Pattern:
    return re.compile(pattern)


STORE_SPECS: dict[str, StoreSpec] = {
    s.id: s
    for s in (
        StoreSpec(
            "comfy", "Comfy", "https://comfy.ua/ua/catalogsearch/result/?q={q}",
            _re(r"^/(?:ua/)?[^/]+\.html$"), _re(r"otzyvy|reviews"),
        ),
        StoreSpec(
            "moyo", "Moyo", "https://www.moyo.ua/ua/search/?q={q}",
            _re(r"^/(?:ua/)?[^/]+/\d+\.html$"),
        ),
        StoreSpec(
            "foxtrot", "Foxtrot", "https://www.foxtrot.com.ua/uk/search?query={q}",
            _re(r"^/(?:uk|ru)/shop/[^/]+\.html$"),
        ),
        StoreSpec(
            "epicentr", "Епіцентр", "https://epicentrk.ua/search/?q={q}",
            _re(r"^/(?:ua/)?shop/[^/]+\.html$"),
        ),
        # Not enabled by default: see README ("Магазини").
        StoreSpec(
            "rozetka", "Rozetka", "https://rozetka.com.ua/ua/search/?text={q}",
            _re(r"/p\d+/?$"),
        ),
        StoreSpec(
            "citrus", "Цитрус", "https://www.citrus.ua/search?query={q}",
            _re(r"-\d+\.html$"),
        ),
        StoreSpec(
            "prom", "Prom", "https://prom.ua/ua/search?search_term={q}",
            _re(r"^/(?:ua/)?p\d+-[^/]+\.html$"),
        ),
    )
}


def _normalize(page_url: str, href: str) -> str:
    absolute, _fragment = urldefrag(urljoin(page_url, href))
    return absolute.split("?")[0]


def _product_links(node: Tag, spec: StoreSpec, page_url: str) -> set[str]:
    urls = (_normalize(page_url, a["href"]) for a in node.find_all("a", href=True))
    return {u for u in urls if spec.matches(u)}


def _find_card(anchor: Tag, spec: StoreSpec, page_url: str) -> Tag | None:
    """Largest ancestor of the link that has one product and a price."""
    card = None
    node = anchor
    while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
        node = node.parent
        if len(_product_links(node, spec, page_url)) > 1:
            break
        text = node.get_text(" ", strip=True)
        if len(text) > MAX_CARD_TEXT:
            break
        if PRICE_RE.search(text):
            card = node
    return card


def _title(anchors: list[Tag]) -> str:
    candidates: list[str] = []
    for a in anchors:
        raws = [a.get("title", ""), a.get_text(" ", strip=True)]
        raws += [img.get("alt", "") for img in a.find_all("img")]
        for raw in raws:
            text = clean_title(raw)
            if 10 <= len(text) <= 200 and not PRICE_RE.search(text):
                candidates.append(text)
    return max(candidates, key=len, default="")


def _prices(text: str) -> list[int]:
    """Plain prices in the text, skipping '-500 ₴' discounts, '+180 ₴' bonuses
    and monthly installments."""
    values = []
    for match in PRICE_RE.finditer(text):
        if match.group("sign") or match.group("per"):
            continue
        values.append(int(re.sub(r"\D", "", match.group("num"))))
    return values


def _old_price(card: Tag, values: list[int], current: int) -> int | None:
    strike = card.find(["s", "del", "strike"])
    if strike is not None:
        digits = re.sub(r"\D", "", strike.get_text())
        if digits and int(digits) > current:
            return int(digits)
    if len(values) > 1 and values[0] > current:
        return values[0]
    return None


def parse_offers(html: str, spec: StoreSpec, page_url: str) -> list[Offer]:
    """Extract offers from a search/catalog page of the store."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    anchors: dict[str, list[Tag]] = {}
    for a in soup.find_all("a", href=True):
        url = _normalize(page_url, a["href"])
        if spec.matches(url):
            anchors.setdefault(url, []).append(a)

    offers: list[Offer] = []
    for url, group in anchors.items():
        title = _title(group)
        card = _find_card(group[0], spec, page_url) if title else None
        if card is None:
            continue
        text = card.get_text(" ", strip=True)
        values = _prices(text)
        if not values:
            continue
        price = values[-1]
        details = " ".join(PRICE_RE.sub(" ", text).replace(title, " ").split())
        offers.append(Offer(
            store_id=spec.id, store_name=spec.name, title=title, url=url,
            price=price, old_price=_old_price(card, values, price),
            in_stock=not OUT_OF_STOCK_RE.search(text), details=details[:400],
        ))
    return offers


class HtmlCardStore(Store):
    """A real online store parsed with the generic card parser."""

    def __init__(self, spec: StoreSpec, fetch: Fetcher):
        self.spec = spec
        self.id = spec.id
        self.name = spec.name
        self._fetch = fetch

    def search(self, query: str, limit: int = 40) -> list[Offer]:
        url = self.spec.search_url.format(q=quote_plus(query))
        html = self._fetch(url)
        return parse_offers(html, self.spec, url)[:limit]


class DemoStore(Store):
    """Fake store with a small catalog; prices move every minute."""

    CATALOG = (
        ("Ноутбук Lenovo IdeaPad Slim 3 15AMN8 (82XQ01K9RA) Arctic Grey", "laptops", 29000),
        ("Ноутбук Lenovo LOQ 15IRX9 (83DV01KPRA) Luna Grey", "laptops", 53000),
        ("Ноутбук ASUS Vivobook 15 (X1504VA-BQ123) Silver", "laptops", 31000),
        ("Ноутбук Apple MacBook Air 13 M3 (MC8H4) Midnight", "laptops", 52000),
        ("Смартфон Apple iPhone 17 256GB (MG6J4) Black", "phones", 45000),
        ("Смартфон Samsung Galaxy S26 256GB (SM-S941BZKD) Black", "phones", 40000),
        ("Смартфон Xiaomi Redmi Note 14 8/256GB (MZB0XYZ) Blue", "phones", 10500),
        ("Планшет Apple iPad 11 2025 128GB (MD4A4) Blue", "tablets", 19500),
        ("Планшет Samsung Galaxy Tab S10 FE (SM-X520NZAA) Gray", "tablets", 17500),
        ("Навушники Apple AirPods Pro 3 (MXP63) White", "headphones", 10500),
        ("Монітор Samsung Odyssey G5 27 (LS27CG510EIXUA) Black", "monitors", 9500),
        ("Телевізор LG OLED55C4 (OLED55C46LA) Black", "tvs", 55000),
        ("Смарт-годинник Apple Watch Series 11 (MWWD3) Black", "watches", 19000),
    )
    NOISE = (-0.08, -0.04, -0.02, 0, 0, 0, 0, 0.02, 0.03)

    def __init__(self, store_id: str, name: str, price_factor: float,
                 clock: Callable[[], float] = time.time):
        self.id = store_id
        self.name = name
        self.price_factor = price_factor
        self._clock = clock

    def search(self, query: str, limit: int = 40) -> list[Offer]:
        tokens = query.lower().split()
        step = int(self._clock() // 60)
        offers = []
        for title, _category, base in self.CATALOG:
            if not all(t in title.lower() for t in tokens):
                continue
            if zlib.crc32(f"{self.id}|{title}".encode()) % 6 == 0:
                continue  # not every demo store sells every model
            regular = round(base * self.price_factor, -2) - 1
            noise = random.Random(f"{self.id}|{title}|{step}").choice(self.NOISE)
            price = round(regular * (1 + noise), -2) - 1
            offers.append(Offer(
                store_id=self.id, store_name=self.name, title=title,
                url=f"https://example.com/{self.id}/{zlib.crc32(title.encode())}",
                price=int(price), old_price=int(regular) if price < regular else None,
            ))
        return offers[:limit]


def build_stores(config: Config, fetch: Fetcher | None = None) -> list[Store]:
    """Create the stores selected in the configuration."""
    if config.demo_mode:
        return [
            DemoStore("demo-a", "Магазин А", 1.00),
            DemoStore("demo-b", "Магазин Б", 0.97),
            DemoStore("demo-c", "Магазин В", 1.04),
        ]
    fetch = fetch or HttpFetcher(config.request_timeout)
    stores: list[Store] = []
    for store_id in config.enabled_stores:
        spec = STORE_SPECS.get(store_id)
        if spec is None:
            known = ", ".join(sorted(STORE_SPECS))
            raise ConfigError(f"Невідомий магазин {store_id!r}. Доступні: {known}.")
        stores.append(HtmlCardStore(spec, fetch))
    if not stores:
        raise ConfigError("Не вибрано жодного магазину.")
    return stores
