"""Web routes."""
from __future__ import annotations

import re
from functools import wraps
from urllib.parse import urlparse

from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request,
    session, url_for,
)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

from .charts import render_price_chart
from .domain import CATEGORIES
from .services import SearchFilters, ValidationError, apply_filters, parse_filters
from .storage import UserExistsError

bp = Blueprint("web", __name__)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_SHOWN = 60


def services():
    return current_app.extensions["services"]


@bp.before_app_request
def load_user() -> None:
    g.user = None
    g.unread = 0
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = services().storage.get_user(user_id)
        if g.user is None:
            session.clear()
        else:
            g.unread = services().storage.count_unread(g.user.id)


@bp.app_context_processor
def inject_user() -> dict:
    return {"user": g.get("user"), "unread": g.get("unread", 0)}


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            flash("Увійдіть, щоб скористатися цією можливістю.")
            return redirect(url_for("web.login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def _safe_next(target: str | None) -> str:
    """Only allow redirects to paths on this site."""
    if target and target.startswith("/") and not target.startswith("//") \
            and not urlparse(target).netloc:
        return target
    return url_for("web.index")


@bp.get("/")
def index():
    args = request.args
    searched = "category" in args
    category = args.get("category", "laptops")
    text = args.get("q", "")
    groups, brands, errors, total = [], [], {}, 0
    if searched:
        try:
            result = services().search.search(category, text)
        except ValidationError as exc:
            flash(str(exc))
        else:
            try:
                filters = parse_filters(args)
            except ValidationError as exc:
                flash(str(exc))
                filters = SearchFilters()
            total = len(result.groups)
            brands = sorted({gr.brand for gr in result.groups})
            groups = apply_filters(result.groups, filters)[:MAX_SHOWN]
            errors = result.errors
    return render_template("index.html", searched=searched, category=category,
                           q=text, groups=groups, brands=brands, errors=errors,
                           total=total, args=args)


@bp.get("/product/<int:product_id>")
def product(product_id: int):
    store = services().storage
    item = store.get_product(product_id)
    if item is None:
        abort(404)
    offers = store.get_offers(product_id)
    history = store.get_history(product_id)
    chart = Markup(render_price_chart(history))
    points = sum(len(p) for p in history.values())
    in_stock = [o["price"] for o in offers if o["in_stock"]]
    best = next((o for o in offers if o["in_stock"]), None)
    return render_template(
        "product.html", product=item, offers=offers, chart=chart, points=points,
        best=best, cheapest=min(in_stock) if in_stock else None,
        wished=bool(g.user and store.is_in_wishlist(g.user.id, product_id)),
        category=CATEGORIES.get(item.category),
    )


@bp.post("/product/<int:product_id>/refresh")
def refresh(product_id: int):
    item = services().storage.get_product(product_id)
    if item is None:
        abort(404)
    errors = services().monitor.refresh_product(item)
    flash("Ціни оновлено.")
    for name, problem in errors.items():
        flash(f"{name}: {problem}")
    return redirect(url_for("web.product", product_id=product_id))


@bp.post("/product/<int:product_id>/wish")
@login_required
def toggle_wish(product_id: int):
    store = services().storage
    if store.get_product(product_id) is None:
        abort(404)
    if store.is_in_wishlist(g.user.id, product_id):
        store.remove_from_wishlist(g.user.id, product_id)
        flash("Товар прибрано зі списку бажаного.")
    else:
        store.add_to_wishlist(g.user.id, product_id)
        flash("Товар додано до бажаного. Ми повідомимо, коли ціна впаде.")
    return redirect(url_for("web.product", product_id=product_id))


@bp.get("/wishlist")
@login_required
def wishlist():
    return render_template("wishlist.html", items=services().storage.list_wishlist(g.user.id))


@bp.get("/notifications")
@login_required
def notifications():
    storage = services().storage
    items = storage.list_notifications(g.user.id)
    return render_template("notifications.html", items=items)


@bp.post("/notifications/read")
@login_required
def read_notifications():
    services().storage.mark_notifications_read(g.user.id)
    return redirect(url_for("web.notifications"))


@bp.route("/register", methods=("GET", "POST"))
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        email = request.form.get("email", "").strip()
        problem = None
        if not USERNAME_RE.match(username):
            problem = "Ім'я: 3–30 символів, латиниця, цифри, «_», «.» або «-»."
        elif len(password) < 6:
            problem = "Пароль має містити щонайменше 6 символів."
        elif email and not EMAIL_RE.match(email):
            problem = "Введіть коректну адресу пошти або залиште поле порожнім."
        if problem is None:
            try:
                user = services().storage.create_user(
                    username, generate_password_hash(password), email)
            except UserExistsError as exc:
                problem = str(exc)
            else:
                session.clear()
                session["user_id"] = user.id
                return redirect(url_for("web.index"))
        flash(problem)
    return render_template("auth.html", mode="register")


@bp.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        found = services().storage.find_user(request.form.get("username", "").strip())
        password = request.form.get("password", "")
        if found and check_password_hash(found[1], password):
            session.clear()
            session["user_id"] = found[0].id
            return redirect(_safe_next(request.args.get("next")))
        flash("Невірне ім'я або пароль.")
    return render_template("auth.html", mode="login")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.index"))


@bp.app_errorhandler(404)
def not_found(_error):
    return render_template("error.html", message="Сторінку не знайдено."), 404
