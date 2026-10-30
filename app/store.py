from datetime import datetime, timezone
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from .bank import balance_for
from .extensions import db
from .models import BankTransaction, Order, OrderAnswer, Product, ProductQuestion, Team
from .security import roles_required, team_required
from .utils import parse_amount, parse_local_datetime

store_bp = Blueprint("store", __name__)


@store_bp.route("/")
def catalog():
    products = (
        Product.query.filter_by(published=True)
        .order_by(Product.created_at.desc())
        .all()
    )
    now = datetime.now(timezone.utc)
    available_products = [
        product
        for product in products
        if product.available_until is None
        or _as_utc(product.available_until) > now
    ]
    balance = None
    if current_user.is_authenticated and current_user.role == "team":
        balance = balance_for(current_user.team_id)
    return render_template("store.html", products=available_products, balance=balance)


@store_bp.post("/products/<int:product_id>/purchase")
@team_required
def purchase(product_id):
    team = Team.query.filter_by(id=current_user.team_id).with_for_update().first()
    product = (
        Product.query.filter_by(id=product_id, published=True)
        .with_for_update()
        .first()
    )
    if team is None:
        flash("The team account could not be found. Contact an organizer.", "error")
        return redirect(url_for("store.catalog"))
    if product is None:
        flash("That item is not available for purchase.", "error")
        return redirect(url_for("store.catalog"))
    now = datetime.now(timezone.utc)
    if product.available_until and _as_utc(product.available_until) <= now:
        flash("The purchase window for that item has closed.", "error")
        return redirect(url_for("store.catalog"))

    try:
        quantity = int(request.form.get("quantity", "1"))
    except ValueError:
        quantity = 0
    if quantity < 1 or quantity > 50:
        flash("Quantity must be between 1 and 50.", "error")
        return redirect(url_for("store.catalog"))
    if product.stock_count is not None and product.stock_count < quantity:
        flash("There is not enough stock to complete that purchase.", "error")
        return redirect(url_for("store.catalog"))
    if product.max_per_team is not None:
        prior_quantity = (
            db.session.query(func.coalesce(func.sum(Order.quantity), 0))
            .filter(
                Order.team_id == team.id,
                Order.product_id == product.id,
                Order.status != "refunded",
            )
            .scalar()
        )
        if prior_quantity + quantity > product.max_per_team:
            flash(
                f"Your team may purchase at most {product.max_per_team} of this item.",
                "error",
            )
            return redirect(url_for("store.catalog"))

    total = product.price * quantity
    if balance_for(team.id) < total:
        flash(
            f"Insufficient balance. This order costs {total:.2f}; your available balance is {balance_for(team.id):.2f}.",
            "error",
        )
        return redirect(url_for("store.catalog"))

    answers = []
    for question in product.questions:
        answer = request.form.get(f"question_{question.id}", "").strip()
        if not answer or len(answer) > 500:
            flash(
                f"Please answer '{question.text}' using 1 to 500 characters.",
                "error",
            )
            return redirect(url_for("store.catalog"))
        answers.append((question, answer))

    order = Order(
        team_id=team.id,
        product_id=product.id,
        quantity=quantity,
        unit_price=product.price,
        status="pending",
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        BankTransaction(
            team_id=team.id,
            created_by_id=current_user.id,
            order_id=order.id,
            kind="debit",
            amount=total,
            description=f"Store purchase: {product.name}",
        )
    )
    db.session.add_all(
        [
            OrderAnswer(order=order, question=question, answer=answer)
            for question, answer in answers
        ]
    )
    if product.stock_count is not None:
        product.stock_count -= quantity
    db.session.commit()
    flash(f"Order #{order.id} placed. An organizer will update its fulfillment status.", "success")
    return redirect(url_for("store.orders"))


@store_bp.route("/orders")
@login_required
def orders():
    if current_user.role == "team":
        results = (
            Order.query.filter_by(team_id=current_user.team_id)
            .order_by(Order.created_at.desc())
            .all()
        )
        return render_template("orders.html", orders=results, is_admin=False)
    if current_user.role not in {"admin", "organizer"}:
        flash("Only team accounts and organizers can view orders.", "error")
        return redirect(url_for("store.catalog"))
    return redirect(url_for("store.manage_orders"))


@store_bp.route("/manage/orders")
@roles_required("admin", "organizer")
def manage_orders():
    orders = Order.query.order_by(Order.created_at.desc()).all()
    return render_template("orders.html", orders=orders, is_admin=True)


@store_bp.route("/manage/products", methods=["GET", "POST"])
@roles_required("admin", "organizer")
def manage_products():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        price = parse_amount(request.form.get("price", ""))
        stock_raw = request.form.get("stock_count", "").strip()
        max_raw = request.form.get("max_per_team", "").strip()
        available_until = parse_local_datetime(request.form.get("available_until", ""))
        questions = [
            line.strip()
            for line in request.form.get("questions", "").splitlines()
            if line.strip()
        ]

        try:
            stock_count = int(stock_raw) if stock_raw else None
            max_per_team = int(max_raw) if max_raw else None
        except ValueError:
            stock_count = -1
            max_per_team = -1

        if not name or len(name) > 120:
            flash("Item name must contain between 1 and 120 characters.", "error")
        elif price is None:
            flash("Price must be a positive amount no greater than 1,000,000.00.", "error")
        elif stock_count is not None and stock_count < 0:
            flash("Stock must be a non-negative whole number, or leave it blank for unlimited stock.", "error")
        elif max_per_team is not None and max_per_team < 1:
            flash("Per-team limit must be a positive whole number, or leave it blank for no limit.", "error")
        elif available_until is False:
            flash("Enter a valid availability date and time.", "error")
        elif len(questions) > 10 or any(len(question) > 255 for question in questions):
            flash("You can add up to 10 purchase questions, each up to 255 characters.", "error")
        else:
            product = Product(
                name=name,
                description=description[:2000],
                price=price,
                stock_count=stock_count,
                max_per_team=max_per_team,
                available_until=available_until,
                published=request.form.get("published") == "on",
            )
            product.questions = [ProductQuestion(text=question) for question in questions]
            db.session.add(product)
            db.session.commit()
            flash(f"Item '{name}' created.", "success")
            return redirect(url_for("store.manage_products"))

    products = Product.query.order_by(Product.created_at.desc()).all()
    return render_template("manage_products.html", products=products)


@store_bp.post("/manage/products/<int:product_id>/toggle")
@roles_required("admin", "organizer")
def toggle_product(product_id):
    product = db.session.get(Product, product_id)
    if product is None:
        flash("That item no longer exists.", "error")
    else:
        product.published = not product.published
        db.session.commit()
        flash(f"Item '{product.name}' {'published' if product.published else 'hidden'}.", "success")
    return redirect(url_for("store.manage_products"))


@store_bp.post("/manage/orders/<int:order_id>/complete")
@roles_required("admin", "organizer")
def complete_order(order_id):
    order = db.session.get(Order, order_id)
    if order is None:
        flash("That order no longer exists.", "error")
    elif order.status != "pending":
        flash("Only pending orders can be marked complete.", "error")
    else:
        order.status = "completed"
        order.completed_at = datetime.now(timezone.utc)
        order.completed_by_id = current_user.id
        db.session.commit()
        flash(f"Order #{order.id} marked complete.", "success")
    return redirect(url_for("store.manage_orders"))


@store_bp.post("/manage/orders/<int:order_id>/refund")
@roles_required("admin", "organizer")
def refund_order(order_id):
    order = Order.query.filter_by(id=order_id).with_for_update().first()
    if order is None:
        flash("That order no longer exists.", "error")
    elif order.status == "refunded":
        flash("That order has already been refunded.", "info")
    else:
        db.session.add(
            BankTransaction(
                team_id=order.team_id,
                created_by_id=current_user.id,
                kind="credit",
                amount=order.total,
                description=f"Refund for store order #{order.id}",
            )
        )
        if order.product.stock_count is not None:
            order.product.stock_count += order.quantity
        order.status = "refunded"
        db.session.commit()
        flash(f"Order #{order.id} refunded and balance restored.", "success")
    return redirect(url_for("store.manage_orders"))


def _as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
