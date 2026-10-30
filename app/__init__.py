import os
from datetime import timedelta
from decimal import Decimal

import click
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import text
from werkzeug.middleware.proxy_fix import ProxyFix

from .extensions import db, login_manager

csrf = CSRFProtect()
migrate = Migrate()


def create_app(test_config=None):
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="../templates",
        static_folder="../static",
    )
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or os.urandom(32),
        CHECK_CREDENTIAL_KEY=os.environ.get("CHECK_CREDENTIAL_KEY"),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", "sqlite:///competition.db"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower()
        == "true",
        SESSION_COOKIE_DOMAIN=os.environ.get("SESSION_COOKIE_DOMAIN") or None,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SCORING_THREADS=max(1, min(int(os.environ.get("SCORING_THREADS", "8")), 64)),
        SCORING_TICK_SECONDS=max(
            5, min(int(os.environ.get("SCORING_TICK_SECONDS", "15")), 300)
        ),
        DEFAULT_PAYOUT_PER_POINT=Decimal(
            os.environ.get("DEFAULT_PAYOUT_PER_POINT", "10")
        ),
        ENGINE_DOMAIN=os.environ.get("ENGINE_DOMAIN", "engine.example.org"),
        BANK_DOMAIN=os.environ.get("BANK_DOMAIN", "bank.example.org"),
        STORE_DOMAIN=os.environ.get("STORE_DOMAIN", "store.example.org"),
        ADMIN_DOMAIN=os.environ.get("ADMIN_DOMAIN", "admin.example.org"),
    )
    if test_config:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    migrate.init_app(app, db)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to access that page."
    login_manager.login_message_category = "info"

    from . import models  # noqa: F401
    from .admin import admin_bp
    from .api import api_bp
    from .auth import auth_bp
    from .bank import bank_bp
    from .main import main_bp
    from .store import store_bp

    @app.context_processor
    def inject_site_links():
        from .main import site_links

        return {"site_links": site_links()}

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(bank_bp, url_prefix="/bank")
    app.register_blueprint(store_bp, url_prefix="/store")

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        app.logger.warning("Rejected form submission due to CSRF validation: %s", error)
        flash("That form expired or could not be verified. Please try again.", "error")
        return redirect(url_for("main.home"))

    @app.errorhandler(403)
    def forbidden(_error):
        return (
            render_template(
                "error.html",
                code=403,
                message="Your account does not have permission to access this page.",
            ),
            403,
        )

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        app.logger.exception("Unhandled request error: %s", error)
        return (
            render_template(
                "error.html",
                code=500,
                message="The request could not be completed. Please try again.",
            ),
            500,
        )

    @app.cli.command("db-check")
    def db_check_command():
        """Verify that the configured database is reachable."""
        db.session.execute(text("SELECT 1"))
        click.echo("Database connection is healthy.")

    @app.cli.command("has-admin")
    def has_admin_command():
        """Return success only when a platform administrator already exists."""
        from .models import User

        if User.query.filter_by(role="admin").first() is None:
            click.echo("No platform administrator exists.")
            raise click.exceptions.Exit(1)
        click.echo("A platform administrator exists.")

    @app.cli.command("bootstrap-admin")
    @click.option("--username", prompt=True)
    @click.password_option()
    def bootstrap_admin_command(username, password):
        """Create the first platform administrator on a fresh install."""
        from .models import User

        if User.query.filter_by(role="admin").first():
            raise click.ClickException(
                "A platform administrator already exists; no account was changed."
            )
        if not username.strip():
            raise click.ClickException("Username cannot be empty.")
        if len(password) < 12:
            raise click.ClickException("Use a password with at least 12 characters.")

        user = User(username=username.strip(), role="admin")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Platform administrator '{user.username}' created.")

    return app


@login_manager.user_loader
def load_user(user_id):
    from .models import User

    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
