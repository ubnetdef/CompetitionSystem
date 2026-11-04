from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, case, false, func, or_

from .check_catalog import CHECK_TYPE_PAYOUTS
from .extensions import db
from .models import BankTransaction, Order, PlatformSetting, ScoreResult, Team, User
from .security import roles_required, team_required

bank_bp = Blueprint("bank", __name__)
BANK_STAFF_ROLES = {"admin", "banker"}
MAX_AMOUNT = Decimal("1000000")
TRANSACTION_FILTER_ARGS = (
    "q",
    "team_id",
    "kind",
    "source",
    "start_date",
    "end_date",
    "min_amount",
    "max_amount",
)


def balance_for(team_id):
    signed_amount = case(
        (BankTransaction.kind == "credit", BankTransaction.amount),
        else_=-BankTransaction.amount,
    )
    return db.session.query(func.coalesce(func.sum(signed_amount), 0)).filter(
        BankTransaction.team_id == team_id
    ).scalar()


def _query_money(name, label):
    raw = request.args.get(name, "").strip()
    if not raw:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        flash(f"Enter a valid {label} amount.", "error")
        return None
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
        flash(f"The {label} amount must be a non-negative whole-dollar amount.", "error")
        return None
    return amount.quantize(Decimal("1"))


def _apply_transaction_filters(query, staff):
    query = query.join(Team, BankTransaction.team_id == Team.id)
    if staff:
        query = query.outerjoin(User, BankTransaction.created_by_id == User.id)

    text = request.args.get("q", "").strip()
    if text:
        search = f"%{text}%"
        criteria = [BankTransaction.description.ilike(search), Team.name.ilike(search)]
        if staff:
            criteria.append(User.username.ilike(search))
        query = query.filter(or_(*criteria))

    kind = request.args.get("kind", "")
    if kind in {"credit", "debit"}:
        query = query.filter(BankTransaction.kind == kind)
    elif kind:
        flash("Choose a valid transaction type filter.", "error")
        query = query.filter(false())

    source = request.args.get("source", "")
    if source == "store":
        query = query.filter(
            or_(
                BankTransaction.order_id.isnot(None),
                BankTransaction.description.ilike("Refund for store order #%"),
            )
        )
    elif source == "scoring":
        query = query.filter(BankTransaction.score_result_id.isnot(None))
    elif source == "staff":
        query = query.filter(
            BankTransaction.created_by_id.isnot(None),
            BankTransaction.order_id.is_(None),
            BankTransaction.score_result_id.is_(None),
            ~BankTransaction.description.ilike("Refund for store order #%"),
        )
    elif source == "system":
        query = query.filter(
            BankTransaction.created_by_id.is_(None),
            BankTransaction.order_id.is_(None),
            BankTransaction.score_result_id.is_(None),
        )
    elif source:
        flash("Choose a valid transaction source filter.", "error")
        query = query.filter(false())

    if staff:
        raw_team_id = request.args.get("team_id", "").strip()
        if raw_team_id:
            try:
                team_id = int(raw_team_id)
            except ValueError:
                flash("Choose a valid team filter.", "error")
                query = query.filter(false())
            else:
                query = query.filter(BankTransaction.team_id == team_id)

    raw_start_day = request.args.get("start_date", "").strip()
    raw_end_day = request.args.get("end_date", "").strip()
    start_day = _query_date("start_date", "start")
    end_day = _query_date("end_date", "end")
    if (raw_start_day and start_day is None) or (raw_end_day and end_day is None):
        query = query.filter(false())
    elif start_day and end_day and end_day < start_day:
        flash("The end date must be on or after the start date.", "error")
        query = query.filter(false())
    else:
        if start_day:
            query = query.filter(
                BankTransaction.created_at
                >= datetime.combine(start_day, time.min, tzinfo=timezone.utc)
            )
        if end_day:
            query = query.filter(
                BankTransaction.created_at
                < datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=timezone.utc)
            )

    raw_min_amount = request.args.get("min_amount", "").strip()
    raw_max_amount = request.args.get("max_amount", "").strip()
    min_amount = _query_money("min_amount", "minimum")
    max_amount = _query_money("max_amount", "maximum")
    if (raw_min_amount and min_amount is None) or (
        raw_max_amount and max_amount is None
    ):
        query = query.filter(false())
    elif min_amount is not None and max_amount is not None and min_amount > max_amount:
        flash("The minimum amount cannot exceed the maximum amount.", "error")
        query = query.filter(false())
    else:
        if min_amount is not None:
            query = query.filter(BankTransaction.amount >= min_amount)
        if max_amount is not None:
            query = query.filter(BankTransaction.amount <= max_amount)
    return query


def _query_date(name, label):
    raw = request.args.get(name, "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        flash(f"Enter a valid {label} date.", "error")
        return None


def _filtered_accounts(teams):
    search = request.args.get("account_q", "").strip().casefold()
    min_balance = _query_money("min_balance", "minimum balance")
    max_balance = _query_money("max_balance", "maximum balance")
    if min_balance is not None and max_balance is not None and min_balance > max_balance:
        flash("The minimum balance cannot exceed the maximum balance.", "error")
        min_balance = max_balance = None

    balances = {team.id: balance_for(team.id) for team in teams}
    if search:
        teams = [
            team
            for team in teams
            if search in team.name.casefold()
            or (team.user and search in team.user.username.casefold())
        ]
    if min_balance is not None:
        teams = [team for team in teams if balances[team.id] >= min_balance]
    if max_balance is not None:
        teams = [team for team in teams if balances[team.id] <= max_balance]
    return teams, balances


@bank_bp.route("/")
@login_required
def home():
    if current_user.role == "team":
        return team_account()
    if current_user.role not in BANK_STAFF_ROLES:
        abort(403)

    teams = Team.query.filter(Team.user.has()).order_by(Team.name).all()
    teams, balances = _filtered_accounts(teams)
    transaction_filters_applied = any(
        request.args.get(name, "").strip() for name in TRANSACTION_FILTER_ARGS
    )
    transactions = []
    if transaction_filters_applied:
        transactions = _apply_transaction_filters(
            BankTransaction.query, staff=True
        ).order_by(BankTransaction.created_at.desc(), BankTransaction.id.desc()).all()
    settings = db.session.get(PlatformSetting, 1)
    return render_template(
        "bank_admin.html",
        teams=teams,
        balances=balances,
        all_teams=Team.query.filter(Team.user.has()).order_by(Team.name).all(),
        transactions=transactions,
        transaction_filters_applied=transaction_filters_applied,
        check_type_labels=CHECK_TYPE_PAYOUTS,
        check_type_payouts=(
            settings.check_type_payouts if settings else {}
        ) or {},
    )


@bank_bp.route("/account")
@team_required
def team_account():
    team = db.session.get(Team, current_user.team_id)
    if team is None:
        abort(404)
    transactions = _apply_transaction_filters(
        BankTransaction.query.filter_by(team_id=team.id), staff=False
    ).order_by(BankTransaction.created_at.desc(), BankTransaction.id.desc()).all()
    return render_template(
        "bank_account.html",
        team=team,
        balance=balance_for(team.id),
        transactions=transactions,
    )


@bank_bp.post("/check-payouts")
@roles_required("admin", "banker")
def update_check_type_payouts():
    payouts = {}
    for check_type, label in CHECK_TYPE_PAYOUTS.items():
        raw_amount = request.form.get(f"reward_{check_type}", "").strip()
        try:
            amount = Decimal(raw_amount)
        except (InvalidOperation, ValueError):
            flash(f"Enter a whole-dollar reward for {label}.", "error")
            return redirect(url_for("bank.home", _anchor="uptime-rewards"))
        if (
            not amount.is_finite()
            or amount < 0
            or amount > MAX_AMOUNT
            or amount != amount.to_integral_value()
        ):
            flash(
                f"The {label} reward must be a whole-dollar amount from 0 to 1,000,000.",
                "error",
            )
            return redirect(url_for("bank.home", _anchor="uptime-rewards"))
        payouts[check_type] = int(amount)

    settings = db.session.get(PlatformSetting, 1)
    if settings is None:
        settings = PlatformSetting(id=1)
        db.session.add(settings)
    settings.check_type_payouts = payouts
    db.session.commit()
    flash("Global uptime rewards saved for each check type.", "success")
    return redirect(url_for("bank.home", _anchor="uptime-rewards"))


@bank_bp.post("/adjust")
@roles_required("admin", "banker")
def adjust():
    action = request.form.get("action", "")
    if not action:
        action = {"credit": "add", "debit": "debit"}.get(
            request.form.get("kind", ""), ""
        )
    description = request.form.get("description", "").strip()
    all_teams_requested = request.form.get("all_teams") == "on"

    amount = None
    if action in {"add", "debit", "set"}:
        try:
            amount = Decimal(request.form.get("amount", "").strip())
        except (InvalidOperation, AttributeError, ValueError):
            amount = Decimal("0")
        if (
            not amount.is_finite()
            or amount < 0
            or amount > MAX_AMOUNT
            or amount != amount.to_integral_value()
        ):
            flash("Enter a whole-dollar amount from 0 to 1,000,000.", "error")
            return redirect(url_for("bank.home"))
        amount = amount.quantize(Decimal("1"))
        if action in {"add", "debit"} and amount <= 0:
            flash("Enter an amount greater than $0.", "error")
            return redirect(url_for("bank.home"))

    if action not in {"add", "debit", "set", "reset"}:
        flash("Choose Add funds, Debit funds, Set balance, or Reset balance.", "error")
        return redirect(url_for("bank.home"))
    if not description or len(description) > 200:
        flash("Enter a reason between 1 and 200 characters.", "error")
        return redirect(url_for("bank.home"))

    if all_teams_requested:
        teams = (
            Team.query.filter(Team.user.has())
            .order_by(Team.id)
            .with_for_update()
            .all()
        )
    else:
        team_id = request.form.get("team_id", type=int)
        team = (
            Team.query.filter_by(id=team_id)
            .filter(Team.user.has())
            .with_for_update()
            .first()
        )
        teams = [team] if team else []

    if not teams:
        flash(
            "No team accounts are available for this fund adjustment."
            if all_teams_requested
            else "That team account no longer exists.",
            "error",
        )
        return redirect(url_for("bank.home"))

    current_balances = {team.id: balance_for(team.id) for team in teams}
    if action == "debit" and any(
        balance < amount for balance in current_balances.values()
    ):
        flash(
            "The debit exceeds at least one selected team's available balance. "
            "No accounts were changed.",
            "error",
        )
        return redirect(url_for("bank.home"))

    transactions = []
    for team in teams:
        balance = current_balances[team.id]
        if action == "add":
            kind, transaction_amount = "credit", amount
        elif action == "debit":
            kind, transaction_amount = "debit", amount
        else:
            target_balance = amount if action == "set" else Decimal("0.00")
            difference = target_balance - balance
            if difference == 0:
                continue
            kind = "credit" if difference > 0 else "debit"
            transaction_amount = abs(difference)

        if action == "set":
            action_label = f"Balance set to ${amount:,.0f}"
        elif action == "reset":
            action_label = "Balance reset to $0"
        elif action == "add":
            action_label = "Fund addition"
        else:
            action_label = "Fund withdrawal"
        prefix = "All teams - " if all_teams_requested else ""
        transactions.append(
            BankTransaction(
                team_id=team.id,
                created_by_id=current_user.id,
                kind=kind,
                amount=transaction_amount.quantize(Decimal("1")),
                description=f"{prefix}{action_label}: {description}",
            )
        )

    if not transactions:
        flash("All selected accounts already have the requested balance.", "info")
        return redirect(url_for("bank.home"))

    db.session.add_all(transactions)
    db.session.commit()
    if action in {"set", "reset"}:
        result = (
            f"Set {len(teams)} team account(s) to ${amount:,.0f}."
            if action == "set"
            else f"Reset {len(teams)} team account(s) to $0."
        )
    else:
        verb = "Added" if action == "add" else "Removed"
        result = f"{verb} ${amount:,.0f} for {len(teams)} team account(s)."
    flash(result, "success")
    return redirect(url_for("bank.home"))


@bank_bp.route("/transactions/<int:transaction_id>")
@login_required
def transaction_detail(transaction_id):
    transaction = db.session.get(BankTransaction, transaction_id)
    if transaction is None:
        abort(404)
    if current_user.role == "team":
        if transaction.team_id != current_user.team_id:
            abort(404)
    elif current_user.role not in BANK_STAFF_ROLES:
        abort(403)

    related_order_id = transaction.order_id
    refund_prefix = "Refund for store order #"
    if related_order_id is None and transaction.description.startswith(refund_prefix):
        order_id = transaction.description[len(refund_prefix):].split(" ", 1)[0]
        try:
            related_order_id = int(order_id)
        except ValueError:
            related_order_id = None

    related_order = (
        db.session.get(Order, related_order_id) if related_order_id else None
    )
    score_result = (
        db.session.get(ScoreResult, transaction.score_result_id)
        if transaction.score_result_id
        else None
    )
    return render_template(
        "bank_transaction.html",
        transaction=transaction,
        related_order=related_order,
        score_result=score_result,
    )
