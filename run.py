"""Entry point: run the site or check the store adapters.

    python run.py                      start the site (http://127.0.0.1:5000)
    python run.py check-stores [text]  query every enabled store and print results
    python run.py refresh              refresh all wishlisted products once
"""
from __future__ import annotations

import argparse
import sys

from app import create_app
from app.config import Config, ConfigError
from app.stores import StoreError, build_stores


def check_stores(config: Config, query: str) -> int:
    """Diagnostics: show what each store returns for a query."""
    failures = 0
    for store in build_stores(config):
        try:
            offers = store.search(query)
        except StoreError as exc:
            failures += 1
            print(f"[помилка] {store.name}: {exc}")
            continue
        print(f"[ok] {store.name}: знайдено {len(offers)}")
        for offer in offers[:3]:
            print(f"      {offer.price} ₴  {offer.title}")
        if not offers:
            print("      0 товарів: перевірте адресу пошуку та шаблон посилань у app/stores.py")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="serve",
                        choices=("serve", "check-stores", "refresh"))
    parser.add_argument("text", nargs="?", default="lenovo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    try:
        config = Config.from_env()
        if args.command == "check-stores":
            return check_stores(config, args.text)
        app = create_app(config, start_monitor=args.command == "serve")
    except ConfigError as exc:
        print(f"Помилка конфігурації: {exc}", file=sys.stderr)
        return 2
    if args.command == "refresh":
        count = app.extensions["services"].monitor.refresh_wishlisted()
        print(f"Оновлено товарів: {count}")
        return 0
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
