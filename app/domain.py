"""Domain model: categories, offers, products and title-matching helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Category:
    """A product category. The site is limited to consumer electronics."""

    id: str
    title: str
    query_word: str
    keywords: tuple[str, ...]


CATEGORIES: dict[str, Category] = {
    c.id: c
    for c in (
        Category("laptops", "Ноутбуки", "ноутбук", ("ноутбук", "laptop", "macbook")),
        Category(
            "phones", "Смартфони", "смартфон",
            ("смартфон", "телефон", "iphone", "galaxy", "redmi", "poco", "pixel"),
        ),
        Category("tablets", "Планшети", "планшет", ("планшет", "ipad", "tablet")),
        Category("monitors", "Монітори", "монітор", ("монітор", "monitor")),
        Category(
            "headphones", "Навушники", "навушники",
            ("навушник", "airpods", "гарнітур", "headphone"),
        ),
        Category(
            "watches", "Смарт-годинники", "смарт-годинник", ("годинник", "watch")
        ),
        Category("tvs", "Телевізори", "телевізор", ("телевізор",)),
    )
}

# Words that mark an accessory instead of the device itself.
ACCESSORY_WORDS = (
    "чохол", "сумка", "рюкзак", "кабель", "зарядн", "скло", "плівк", "захисн",
    "кейс", "підставк", "ремін", "адаптер", "тримач", "стилус", "клавіатур",
    "мишк", "док-станц", "акумулятор", "кріплення", "бампер", "наклейк",
)

BRANDS = (
    "Apple", "Samsung", "Xiaomi", "Lenovo", "ASUS", "Acer", "HP", "Dell", "MSI",
    "Huawei", "Honor", "Motorola", "Realme", "OnePlus", "Google", "Sony", "JBL",
    "LG", "Philips", "Gigabyte", "Microsoft", "Nokia", "ZTE", "Oppo", "Vivo",
    "Tecno", "Infinix", "Nothing", "2E", "Dream Machines", "TCL", "Hisense",
    "Garmin", "Panasonic", "Bose", "Sennheiser", "Beats", "Anker", "Logitech",
)

_COLOR_WORDS = {
    "black", "white", "grey", "gray", "silver", "blue", "green", "red", "pink",
    "gold", "purple", "titanium", "graphite", "midnight", "starlight",
    "чорний", "білий", "сірий", "синій", "зелений", "червоний", "рожевий",
    "золотий", "сріблястий", "фіолетовий",
}
_STOP_WORDS = {"і", "та", "з", "для", "ігровий", "ультрабук"}
_BADGE_RE = re.compile(
    r"^(?:КРАЩ(?:\s+[OОo]нлайн|\s+online)?|Новинка|Хіт|Акція)\s+", re.IGNORECASE
)
_MPN_RE = re.compile(r"\(([A-Za-z0-9][A-Za-z0-9\-/.]{3,24})\)")


@dataclass(frozen=True)
class Offer:
    """One product listing found in one store."""

    store_id: str
    store_name: str
    title: str
    url: str
    price: int
    old_price: int | None = None
    in_stock: bool = True
    details: str = ""

    @property
    def discount_percent(self) -> int:
        """Store-announced discount (old price vs current), 0 if none."""
        if self.old_price and self.old_price > self.price:
            return round((self.old_price - self.price) * 100 / self.old_price)
        return 0


@dataclass(frozen=True)
class Product:
    """A product (model) tracked by the site."""

    id: int
    key: str
    title: str
    category: str
    brand: str
    search_query: str


@dataclass(frozen=True)
class User:
    id: int
    username: str
    email: str


@dataclass(frozen=True)
class PriceChange:
    """A price decrease detected for a product in one store."""

    store_id: str
    store_name: str
    old_price: int
    new_price: int
    url: str

    @property
    def drop_percent(self) -> float:
        return (self.old_price - self.new_price) * 100 / self.old_price


@dataclass(frozen=True)
class ProductSummary:
    product: Product
    min_price: int | None
    store_count: int


@dataclass(frozen=True)
class Notification:
    id: int
    product_id: int
    message: str
    created_at: datetime
    is_read: bool


def format_money(value: int | None) -> str:
    """Format a UAH amount, e.g. 35999 -> '35 999 ₴'."""
    if value is None:
        return "—"
    return f"{value:,}".replace(",", "\u00a0") + "\u00a0₴"


def clean_title(raw: str) -> str:
    """Normalize whitespace, drop store badges and de-duplicate doubled text."""
    text = _BADGE_RE.sub("", " ".join(raw.split()))
    half = len(text) // 2
    if len(text) % 2 == 1 and text[half] == " " and text[:half] == text[half + 1:]:
        text = text[:half]
    return text


def is_relevant(title: str, category: Category) -> bool:
    """Check that a listing is the device itself, not an accessory."""
    words = title.lower().split()
    head = " ".join(words[:2])
    head3 = " ".join(words[:3])
    if any(w in head3 for w in ACCESSORY_WORDS):
        return False
    return any(k in head for k in category.keywords)


def detect_brand(title: str) -> str:
    """Return the first known brand mentioned in the title."""
    lower = title.lower()
    best: tuple[int, str] | None = None
    for brand in BRANDS:
        match = re.search(rf"(?<!\w){re.escape(brand.lower())}(?!\w)", lower)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), brand)
    return best[1] if best else "Інше"


def extract_mpn(title: str) -> str | None:
    """Return the manufacturer part number written in parentheses, if any."""
    for match in reversed(list(_MPN_RE.finditer(title))):
        token = match.group(1)
        if re.search(r"\d", token) and re.search(r"[A-Za-z]", token):
            return token.upper()
    return None


def model_key(title: str, category_id: str) -> str:
    """Key that is equal for the same model sold in different stores."""
    mpn = extract_mpn(title)
    if mpn:
        return f"{category_id}:{re.sub(r'[^A-Z0-9]', '', mpn)}"
    category_words = {k for c in CATEGORIES.values() for k in c.keywords}
    tokens = {
        t
        for t in re.split(r"[^\w]+", title.lower())
        if t and t not in _COLOR_WORDS and t not in _STOP_WORDS
        and not any(t.startswith(k) for k in category_words)
    }
    return f"{category_id}:{'-'.join(sorted(tokens))}"[:200]


def build_search_query(title: str) -> str:
    """Query used to find the same product again in stores."""
    mpn = extract_mpn(title)
    if mpn:
        return mpn
    words = title.split()
    return " ".join(words[1:] if len(words) > 3 else words)
