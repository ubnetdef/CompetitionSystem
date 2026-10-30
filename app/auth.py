from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from sqlalchemy import func

from .extensions import db
from .models import User

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter(func.lower(User.username) == username.lower()).first()
        if user is None or not user.is_active or not user.check_password(password):
            current_app.logger.warning("Rejected sign-in for username %s", username[:80])
            flash("Sign-in failed. Check the username and password, or contact an organizer.", "error")
            return render_template("login.html"), 401

        login_user(user, remember=False, fresh=True)
        flash(f"Welcome back, {user.username}.", "success")
        return redirect(url_for("main.home"))

    return render_template("login.html")


@auth_bp.post("/logout")
def logout():
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("main.home"))
