from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import case, func

from .extensions import db
from .models import BankTransaction, Team, User
from .security import roles_required, team_required

bank_bp = Blueprint("bank", __name__)


def balance_for(team_id):
    signed_amount = case(
        (BankTransaction.kind == "credit", BankTransaction.amount),
        else_=-BankTransaction.amount,
    )
    return db.session.query(func.coalesce(func.sum(signed_amount), 0)).filter(
        BankTransaction.team_id == team_id
    ).scalar()


@bank_bp.route("/")
@login_required
def home():
    if current_user.role == "team":
        return team_account()
    if current_user.role not in {"admin", "organizer", "banker"}:
        abort(403)
    teams = Team.query.filter(Team.user.has()).order_by(Team.name).all()
    balances = {team.id: balance_for(team.id) for team in teams}
    transactions = BankTransaction.query.order_by(BankTransaction.created_at.desc()).limit(100).all()
    return render_template("bank_admin.html", teams=teams, balances=balances, transactions=transactions)


@bank_bp.route("/account")
@team_required
def team_account():
    team = db.session.get(Team, current_user.team_id)
    if team is None:
        abort(404)
    transactions = (
        BankTransaction.query.filter_by(team_id=team.id)
        .order_by(BankTransaction.created_at.desc())
        .limit(50)
        .all()
    )
    return render_template(
        "bank_account.html",
        team=team,
        balance=balance_for(team.id),
        transactions=transactions,
    )


@bank_bp.post("/adjust")
@roles_required("admin", "organizer", "banker")
def adjust():
    team_id = request.form.get("team_id", type=int)
    kind = request.form.get("kind", "")
    description = request.form.get("description", "").strip()
    try:
        amount = Decimal(request.form.get("amount", "").strip())
    except (InvalidOperation, AttributeError):
        amount = Decimal("0")

    if kind not in {"credit", "debit"}:
        flash("Choose either a credit or a debit.", "error")
        return redirect(url_for("bank.home"))
    if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000"):
        flash("Enter an amount greater than 0 and no more than 1,000,000.00.", "error")
        return redirect(url_for("bank.home"))
    if not description or len(description) > 255:
        flash("Enter a description between 1 and 255 characters.", "error")
        return redirect(url_for("bank.home"))

    team = (
        Team.query.filter_by(id=team_id)
        .filter(Team.user.has())
        .with_for_update()
        .first()
    )
    if team is None:
        flash("That team account no longer exists.", "error")
        return redirect(url_for("bank.home"))
    if kind == "debit" and balance_for(team.id) < amount:
        flash("The debit exceeds the team's available balance.", "error")
        return redirect(url_for("bank.home"))

    db.session.add(
        BankTransaction(
            team_id=team.id,
            created_by_id=current_user.id,
            kind=kind,
            amount=amount.quantize(Decimal("0.01")),
            description=description,
        )
    )
    db.session.commit()
    flash(f"{kind.title()} of {amount:.2f} recorded for {team.name}.", "success")
    return redirect(url_for("bank.home"))
