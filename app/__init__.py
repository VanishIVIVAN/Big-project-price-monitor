"""Price monitor for electronics stores: application factory."""
from __future__ import annotations

from dataclasses import dataclass

from flask import Flask

from .config import Config
from .domain import CATEGORIES, format_money
from .notifications import CompositeNotifier, EmailNotifier, InAppNotifier, Notifier
from .services import PriceMonitor, PriceTracker, SearchService
from .storage import Storage
from .stores import Store, build_stores


@dataclass
class Services:
    """Objects shared by all request handlers."""

    config: Config
    storage: Storage
    stores: list[Store]
    search: SearchService
    monitor: PriceMonitor


def create_app(config: Config | None = None, stores: list[Store] | None = None,
               notifier: Notifier | None = None, start_monitor: bool = False) -> Flask:
    """Create the Flask app. Stores and notifier can be injected (used by tests)."""
    config = config or Config.from_env()
    config.validate()
    storage = Storage(config.database_path)
    stores = stores if stores is not None else build_stores(config)
    if notifier is None:
        channels: list[Notifier] = [InAppNotifier(storage)]
        email = EmailNotifier.from_config(config)
        if email is not None:
            channels.append(email)
        notifier = CompositeNotifier(channels)
    tracker = PriceTracker(storage, notifier, config.min_drop_percent)
    monitor = PriceMonitor(storage, stores, tracker, config.monitor_interval_minutes)

    app = Flask(__name__)
    app.config.update(SECRET_KEY=config.secret_key, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_HTTPONLY=True)
    app.extensions["services"] = Services(
        config, storage, stores, SearchService(stores, tracker), monitor
    )
    app.jinja_env.filters["money"] = format_money
    app.jinja_env.filters["localtime"] = lambda dt: dt.astimezone().strftime("%d.%m.%Y %H:%M")
    app.jinja_env.globals["CATEGORIES"] = CATEGORIES

    from .views import bp
    app.register_blueprint(bp)
    if start_monitor:
        monitor.start()
    return app
