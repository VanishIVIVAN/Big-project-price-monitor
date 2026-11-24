"""Tests for parsing, error handling and the main user scenario.

Run with:  python -m unittest discover -s tests -v   (or: pytest)
The HTML samples are small hand-made fragments that imitate the structure of
the real catalog pages (link + old price + discount + price); they are not
copies of the stores' markup.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import create_app  # noqa: E402
from app.config import Config, ConfigError  # noqa: E402
from app.domain import CATEGORIES, Offer, clean_title, model_key  # noqa: E402
from app.notifications import (  # noqa: E402
    CompositeNotifier, EmailNotifier, InAppNotifier, NotificationError, Notifier,
)
from app.services import (  # noqa: E402
    SearchService, ValidationError, parse_filters, apply_filters,
)
from app.storage import Storage, UserExistsError  # noqa: E402
from app.stores import (  # noqa: E402
    STORE_SPECS, HtmlCardStore, HttpFetcher, Store, StoreBlockedError, StoreError,
    build_stores, parse_offers,
)

COMFY_HTML = """
<div><div class="item">
  <a href="https://comfy.ua/ua/noutbuk-lenovo-ideapad-slim-3-15amn8-82xq01asra-arctic-grey.html">КРАЩ Ноутбук Lenovo IdeaPad Slim 3 15AMN8 (82XQ01ASRA) Arctic Grey</a>
  <a href="https://comfy.ua/ua/noutbuk-lenovo-ideapad-slim-3-15amn8-82xq01asra-arctic-grey-otzyvy.html">5 5 100</a>
  <span>Доставка за 1₴</span>
  <div><s>39 999 ₴</s> <span>-4 000 ₴</span> <b>35 999 ₴</b></div>
  <span>+180 ₴ на бонусний рахунок</span>
</div>
<div class="item">
  <a href="/ua/noutbuk-lenovo-loq-15irx9-83dv01kpra-luna-grey.html">Ноутбук ігровий Lenovo LOQ 15IRX9 (83DV01KPRA) Luna Grey</a>
  <b>52 999 ₴</b> <i>від 2 100 ₴/міс</i>
</div></div>
"""

MOYO_HTML = """
<ul><li class="card">
  <a href="/ua/noutbuk_lenovo_ideapad_slim_3_15amn8_arctic_grey_82xq01k9ra/764728.html"><img alt=""></a>
  <a href="/ua/noutbuk_lenovo_ideapad_slim_3_15amn8_arctic_grey_82xq01k9ra/764728.html">Ноутбук LENOVO IdeaPad Slim 3 15AMN8 Arctic Grey (82XQ01K9RA) Ноутбук LENOVO IdeaPad Slim 3 15AMN8 Arctic Grey (82XQ01K9RA)</a>
  <div>29 999 ₴</div><div>28 999 ₴</div><button>Купити</button>
  <ul><li>Діагональ екрана: 15.6"</li><li>Об'єм оперативної пам'яті: 16 Гб</li></ul>
</li></ul>
"""

EPICENTR_HTML = """
<ul>
<li><a href="/shop/noutbuk-lenovo-loq-15irx9-15-6-1451596-luna-grey.html" title='Ноутбук Lenovo LOQ 15IRX9 15,6" (83DV01F0RA) luna grey'><img></a>
  <a href="/shop/noutbuk-lenovo-loq-15irx9-15-6-1451596-luna-grey.html">Ноутбук Lenovo LOQ 15IRX9 15,6" (83DV01F0RA) luna grey</a>
  <s>63 499</s> <span>-500 ₴</span> <b>62 999 ₴/шт.</b></li>
<li><a href="/shop/noutbuk-lenovo-ideapad-slim-3-15amn8-15-6-1456094-arctic-grey.html">Ноутбук Lenovo IdeaPad Slim 3 15AMN8 15,6" (82XQ01K0RA) arctic grey</a>
  <span>Нет в наличии</span></li>
</ul>
"""


class FakeStore(Store):
    """Store whose offers are set by the test."""

    def __init__(self, store_id="fake", name="Fake", price=1000):
        self.id, self.name, self.price = store_id, name, price
        self.error: Exception | None = None

    def search(self, query, limit=40):
        if self.error:
            raise self.error
        return [Offer(self.id, self.name, "Ноутбук Lenovo Test 15 (ABC12345) Grey",
                      f"https://example.com/{self.id}", self.price)]


class ParserTests(unittest.TestCase):
    def test_comfy_old_price_discount_bonus_and_installment(self):
        offers = parse_offers(COMFY_HTML, STORE_SPECS["comfy"],
                              "https://comfy.ua/ua/catalogsearch/result/?q=lenovo")
        self.assertEqual(len(offers), 2)  # review link is not a product
        first, second = offers
        self.assertEqual(first.price, 35999)
        self.assertEqual(first.old_price, 39999)
        self.assertEqual(first.discount_percent, 10)
        self.assertEqual(first.title,
                         "Ноутбук Lenovo IdeaPad Slim 3 15AMN8 (82XQ01ASRA) Arctic Grey")
        self.assertEqual(second.price, 52999)  # "2 100 ₴/міс" is ignored
        self.assertIsNone(second.old_price)
        self.assertTrue(second.url.startswith("https://comfy.ua/ua/"))

    def test_moyo_doubled_title_and_two_prices(self):
        (offer,) = parse_offers(MOYO_HTML, STORE_SPECS["moyo"], "https://www.moyo.ua/ua/search/?q=x")
        self.assertEqual((offer.price, offer.old_price), (28999, 29999))
        self.assertEqual(offer.title.count("Ноутбук"), 1)
        self.assertIn("16 Гб", offer.details)

    def test_epicentr_strikethrough_price_and_no_price_item_skipped(self):
        offers = parse_offers(EPICENTR_HTML, STORE_SPECS["epicentr"], "https://epicentrk.ua/search/?q=x")
        self.assertEqual(len(offers), 1)  # the out-of-stock item has no price
        self.assertEqual((offers[0].price, offers[0].old_price), (62999, 63499))

    def test_garbage_html_gives_no_offers(self):
        self.assertEqual(parse_offers("", STORE_SPECS["comfy"], "https://comfy.ua/"), [])
        self.assertEqual(parse_offers("<p>нічого</p><<<", STORE_SPECS["moyo"], "https://x/"), [])

    def test_same_model_has_same_key_across_stores(self):
        a = model_key("Ноутбук Lenovo LOQ 15IRX9 (83DV01KPRA) Luna Grey", "laptops")
        b = model_key('Ноутбук Lenovo LOQ 15IRX9 15,6" (83DV01KPRA) luna grey', "laptops")
        self.assertEqual(a, b)

    def test_clean_title(self):
        self.assertEqual(clean_title("  КРАЩ   Ноутбук  X1 "), "Ноутбук X1")


class StoreErrorTests(unittest.TestCase):
    def _response(self, status, text="<html></html>"):
        resp = mock.Mock(status_code=status, text=text)
        session = mock.Mock()
        session.get.return_value = resp
        return HttpFetcher(session=session)

    def test_blocked_statuses(self):
        for status in (403, 429, 503):
            with self.assertRaises(StoreBlockedError):
                self._response(status)("https://x")

    def test_captcha_page(self):
        with self.assertRaises(StoreBlockedError):
            self._response(200, "<html>Please solve the CAPTCHA</html>")("https://x")

    def test_other_http_error(self):
        with self.assertRaises(StoreError):
            self._response(500)("https://x")

    def test_network_failure(self):
        import requests
        session = mock.Mock()
        session.get.side_effect = requests.ConnectionError("down")
        with self.assertRaises(StoreError):
            HttpFetcher(session=session)("https://x")

    def test_store_passes_encoded_query(self):
        seen = []
        store = HtmlCardStore(STORE_SPECS["comfy"], lambda url: seen.append(url) or "")
        store.search("lenovo ideapad 15,6")
        self.assertIn("lenovo+ideapad+15%2C6", seen[0])

    def test_unknown_store_in_config(self):
        with self.assertRaises(ConfigError):
            build_stores(Config(enabled_stores=("nope",)), fetch=lambda u: "")
        with self.assertRaises(ConfigError):
            build_stores(Config(enabled_stores=()), fetch=lambda u: "")

    def test_bad_environment_values(self):
        with mock.patch.dict(os.environ, {"REQUEST_TIMEOUT": "abc"}):
            with self.assertRaises(ConfigError):
                Config.from_env()
        with mock.patch.dict(os.environ, {"MIN_DROP_PERCENT": "500"}):
            with self.assertRaises(ConfigError):
                Config.from_env()


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(database_path=os.path.join(self.tmp.name, "t.db"))
        self.store_a = FakeStore("a", "Магазин А", 1000)
        self.store_b = FakeStore("b", "Магазин Б", 1100)
        self.app = create_app(self.config, stores=[self.store_a, self.store_b])
        self.client = self.app.test_client()
        self.services = self.app.extensions["services"]

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, name="tester", email=""):
        return self.client.post("/register", data={
            "username": name, "password": "secret1", "email": email,
        }, follow_redirects=True)


class SearchErrorTests(AppTestCase):
    def test_invalid_category_and_long_query(self):
        search = self.services.search
        with self.assertRaises(ValidationError):
            search.search("cars", "bmw")
        with self.assertRaises(ValidationError):
            search.search("laptops", "x" * 200)

    def test_broken_store_does_not_break_search(self):
        self.store_a.error = StoreBlockedError("captcha")
        self.store_b.error = RuntimeError("parser bug")
        result = self.services.search.search("laptops", "lenovo")
        self.assertEqual(result.groups, [])
        self.assertEqual(set(result.errors), {"Магазин А", "Магазин Б"})
        page = self.client.get("/?category=laptops&q=lenovo")
        self.assertEqual(page.status_code, 200)
        self.assertIn("captcha", page.get_data(as_text=True))

    def test_one_store_down_other_results_kept(self):
        self.store_a.error = StoreError("timeout")
        result = self.services.search.search("laptops", "lenovo")
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0].store_count, 1)
        self.assertIn("Магазин А", result.errors)

    def test_bad_filters(self):
        for args in ({"min_price": "abc"}, {"max_price": "-5"},
                     {"min_price": "500", "max_price": "100"}):
            with self.assertRaises(ValidationError):
                parse_filters(args)
        page = self.client.get("/?category=laptops&q=lenovo&min_price=abc")
        self.assertEqual(page.status_code, 200)
        self.assertIn("введіть ціле", page.get_data(as_text=True))
        self.assertIn("Test 15", page.get_data(as_text=True))  # results still shown

    def test_filters_apply(self):
        groups = self.services.search.search("laptops", "lenovo").groups
        self.assertEqual(len(apply_filters(groups, parse_filters({"brand": "Lenovo"}))), 1)
        self.assertEqual(apply_filters(groups, parse_filters({"brand": "Apple"})), [])
        self.assertEqual(apply_filters(groups, parse_filters({"min_price": "5000"})), [])
        self.assertEqual(len(apply_filters(groups, parse_filters({"keywords": "grey, test"}))), 1)

    def test_accessories_are_ignored(self):
        class Accessory(FakeStore):
            def search(self, query, limit=40):
                return [Offer("a", "A", "Чохол для ноутбука Lenovo (ABC12345)",
                              "https://example.com/c", 300)]
        svc = SearchService([Accessory()], self.services.search.tracker)
        self.assertEqual(svc.search("laptops", "lenovo").groups, [])


class WebErrorTests(AppTestCase):
    def test_missing_product_is_404(self):
        self.assertEqual(self.client.get("/product/12345").status_code, 404)
        self.assertEqual(self.client.post("/product/12345/refresh").status_code, 404)

    def test_wishlist_requires_login(self):
        self.client.get("/?category=laptops&q=lenovo")
        resp = self.client.post("/product/1/wish")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        self.assertEqual(self.client.get("/wishlist").status_code, 302)
        self.assertEqual(self.client.get("/notifications").status_code, 302)

    def test_registration_validation(self):
        bad = [
            {"username": "a", "password": "secret1"},
            {"username": "good_name", "password": "123"},
            {"username": "good_name", "password": "secret1", "email": "not-an-email"},
        ]
        for data in bad:
            resp = self.client.post("/register", data=data)
            self.assertEqual(resp.status_code, 200)
            with self.client.session_transaction() as sess:
                self.assertNotIn("user_id", sess)

    def test_duplicate_user_and_wrong_password(self):
        self.register("tester")
        self.client.post("/logout")
        resp = self.client.post("/register", data={"username": "TESTER", "password": "secret1"})
        self.assertIn("уже існує", resp.get_data(as_text=True))
        resp = self.client.post("/login", data={"username": "tester", "password": "wrong"})
        self.assertIn("Невірне", resp.get_data(as_text=True))
        with self.assertRaises(UserExistsError):
            Storage(self.config.database_path).create_user("tester", "x", "")

    def test_login_does_not_redirect_to_other_sites(self):
        self.register("tester")
        self.client.post("/logout")
        resp = self.client.post("/login?next=//evil.example", data={
            "username": "tester", "password": "secret1"})
        self.assertEqual(urlparse_path(resp.headers["Location"]), "/")


def urlparse_path(url):
    from urllib.parse import urlparse
    return urlparse(url).path


class NotificationTests(AppTestCase):
    def test_full_scenario_price_drop_creates_notification(self):
        self.register("tester")
        self.client.get("/?category=laptops&q=lenovo")
        page = self.client.get("/product/1")
        self.assertIn("Магазин А", page.get_data(as_text=True))
        self.client.post("/product/1/wish")
        self.assertIn("Прибрати з бажаного",
                      self.client.get("/product/1").get_data(as_text=True))

        self.store_a.price = 800  # discount in store A (-20%)
        self.client.post("/product/1/refresh")
        text = self.client.get("/notifications").get_data(as_text=True)
        self.assertIn("Магазин А", text)
        self.assertIn("−20%", text)
        self.assertEqual(self.services.storage.count_unread(1), 1)
        self.client.post("/notifications/read")
        self.assertEqual(self.services.storage.count_unread(1), 0)

    def test_price_increase_or_tiny_drop_does_not_notify(self):
        self.register("tester")
        self.client.get("/?category=laptops&q=lenovo")
        self.client.post("/product/1/wish")
        self.store_a.price = 1200
        self.client.post("/product/1/refresh")
        self.store_a.price = 1199  # 0.08% drop is below the 1% threshold
        self.client.post("/product/1/refresh")
        self.assertEqual(self.services.storage.count_unread(1), 0)

    def test_only_watchers_are_notified(self):
        self.client.get("/?category=laptops&q=lenovo")
        self.register("watcher")
        self.client.post("/product/1/wish")
        self.client.post("/logout")
        self.register("other")
        self.store_b.price = 500
        self.services.monitor.refresh_wishlisted()
        self.assertEqual(self.services.storage.count_unread(1), 1)
        self.assertEqual(self.services.storage.count_unread(2), 0)

    def test_failed_email_does_not_block_in_app_notice(self):
        class Broken(Notifier):
            def send(self, user, product_id, message):
                raise NotificationError("smtp down")

        storage = self.services.storage
        user = storage.create_user("mailer", "h", "m@example.com")
        product = storage.upsert_product("k", "T", "laptops", "Lenovo", "q")
        composite = CompositeNotifier([Broken(), InAppNotifier(storage)])
        composite.send(user, product.id, "hello")
        self.assertEqual(storage.count_unread(user.id), 1)
        self.assertEqual(composite.failures, ["smtp down"])

    def test_email_notifier_handles_smtp_errors(self):
        import smtplib
        from app.domain import User
        notifier = EmailNotifier("smtp.invalid", 587, "from@example.com")
        with mock.patch("smtplib.SMTP", side_effect=smtplib.SMTPException("boom")):
            with self.assertRaises(NotificationError):
                notifier.send(User(1, "u", "to@example.com"), 1, "m")
        notifier.send(User(1, "u", ""), 1, "m")  # no address: silently skipped

    def test_monitor_refresh_keeps_data_when_store_fails(self):
        self.client.get("/?category=laptops&q=lenovo")
        product = self.services.storage.get_product(1)
        self.store_a.error = StoreBlockedError("blocked")
        errors = self.services.monitor.refresh_product(product)
        self.assertIn("Магазин А", errors)
        self.assertEqual(len(self.services.storage.get_offers(1)), 2)  # old data kept


class HistoryTests(AppTestCase):
    def test_history_grows_when_price_changes(self):
        self.client.get("/?category=laptops&q=lenovo")
        self.assertEqual(sum(len(v) for v in self.services.storage.get_history(1).values()), 2)
        self.client.post("/product/1/refresh")  # same prices: no new points
        self.assertEqual(sum(len(v) for v in self.services.storage.get_history(1).values()), 2)
        self.store_a.price = 900
        self.client.post("/product/1/refresh")
        self.assertEqual(len(self.services.storage.get_history(1)["Магазин А"]), 2)
        self.assertIn("<svg", self.client.get("/product/1").get_data(as_text=True))

    def test_categories_are_electronics_only(self):
        self.assertEqual(set(CATEGORIES), {"laptops", "phones", "tablets", "monitors",
                                           "headphones", "watches", "tvs"})


if __name__ == "__main__":
    unittest.main()
