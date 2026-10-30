from functools import wraps

from flask import abort, flash, redirect, url_for
from flask_login import current_user


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login"))
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def team_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        if current_user.role != "team" or current_user.team_id is None:
            flash("This page is available to team accounts.", "error")
            return redirect(url_for("main.home"))
        return view(*args, **kwargs)

    return wrapped
