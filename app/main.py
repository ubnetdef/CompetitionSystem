import hashlib
import ipaddress

from flask import abort, Blueprint, current_app, flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from .check_catalog import TEAM_CREDENTIAL_CHECK_TYPES, TEAM_EDITABLE_CREDENTIAL_FIELDS
from .check_secrets import decrypt_credentials, encrypt_credentials
from .extensions import db
from .models import (
    Competition,
    Host,
    HostGroup,
    PlatformSetting,
    Product,
    ScoreResult,
    ScoreRound,
    Service,
    Team,
    TeamServiceConfig,
)
from .security import team_required
from .utils import address_matches_ranges, valid_host

main_bp = Blueprint("main", __name__)


def site_links():
    hosts = {
        "engine": current_app.config.get("ENGINE_DOMAIN", "engine.example.org"),
        "bank": current_app.config.get("BANK_DOMAIN", "bank.example.org"),
        "store": current_app.config.get("STORE_DOMAIN", "store.example.org"),
        "admin": current_app.config.get("ADMIN_DOMAIN", "admin.example.org"),
    }
    configured_hosts = {host.lower() for host in hosts.values()}
    if request.host.split(":", 1)[0].lower() not in configured_hosts:
        return {name: "" for name in hosts}
    scheme = request.scheme
    return {name: f"{scheme}://{host}" for name, host in hosts.items()}


def redirect_to_site(site, endpoint):
    return redirect(f"{site_links()[site]}{url_for(endpoint)}")


@main_bp.route("/")
def home():
    site = request.host.split(":", 1)[0].split(".", 1)[0].lower()
    if current_user.is_authenticated:
        if current_user.role in {"admin", "organizer"}:
            return redirect_to_site("admin", "admin.dashboard")
        if current_user.role == "banker":
            return redirect_to_site("bank", "bank.home")
        if current_user.role == "team":
            return redirect_to_site("store", "store.catalog")
        return redirect(url_for("main.dashboard"))

    competitions = Competition.query.filter_by(status="active").order_by(Competition.name).all()
    products = Product.query.filter_by(published=True).order_by(Product.name).limit(4).all()
    return render_template("landing.html", competitions=competitions, products=products)


@main_bp.route("/dashboard")
def dashboard():
    if not current_user.is_authenticated:
        return render_template("landing.html", competitions=[], products=[])
    if current_user.role in {"admin", "organizer"}:
        return redirect_to_site("admin", "admin.dashboard")
    if current_user.role == "banker":
        return redirect_to_site("bank", "bank.home")
    if current_user.role == "team":
        return redirect_to_site("store", "store.catalog")
    return render_template("error.html", code=403, message="This account does not have a supported role."), 403


@main_bp.route("/scoreboard")
def scoreboard():
    query = Competition.query.filter_by(status="active")
    competitions = query.order_by(Competition.name).all()
    settings = db.session.get(PlatformSetting, 1)
    show_competition_selector = bool(
        settings and settings.scoreboard_competition_selector_enabled
    )
    competition_id = (
        request.args.get("competition", type=int)
        if show_competition_selector
        else None
    )
    selected = next(
        (competition for competition in competitions if competition.id == competition_id),
        competitions[0] if competitions else None,
    )
    rows = []
    services = []
    latest_results = {}
    if selected:
        rows, services, latest_results = _scoreboard_snapshot(selected)
    return render_template(
        "scoreboard.html",
        competitions=competitions,
        selected=selected,
        rows=rows,
        services=services,
        latest_results=latest_results,
        show_competition_selector=show_competition_selector,
    )


def _scoreboard_snapshot(competition):
    teams = sorted(competition.teams, key=lambda team: team.name.casefold())
    point_totals = (
        db.session.query(
            Team.id,
            func.coalesce(func.sum(ScoreResult.points), 0).label("score"),
        )
        .join(ScoreResult, ScoreResult.team_id == Team.id)
        .join(ScoreRound, ScoreResult.round_id == ScoreRound.id)
        .filter(
            ScoreRound.competition_id == competition.id,
            ScoreRound.status == "completed",
        )
        .group_by(Team.id)
        .all()
    )
    score_by_team = {team_id: score for team_id, score in point_totals}
    rows = sorted(
        (
            {"team": team, "score": score_by_team.get(team.id, 0)}
            for team in teams
        ),
        key=lambda row: (-row["score"], row["team"].name.casefold()),
    )
    services = (
        Service.query.outerjoin(HostGroup, Service.host_group_id == HostGroup.id)
        .filter(
            Service.competition_id == competition.id,
            Service.hidden_from_scoreboard.is_(False),
        )
        .order_by(HostGroup.name, Service.check_type, Service.name)
        .all()
    )
    latest_rounds = (
        db.session.query(
            ScoreResult.team_id.label("team_id"),
            ScoreResult.service_id.label("service_id"),
            func.max(ScoreRound.number).label("round_number"),
        )
        .join(ScoreRound, ScoreResult.round_id == ScoreRound.id)
        .filter(
            ScoreRound.competition_id == competition.id,
            ScoreRound.status == "completed",
        )
        .group_by(ScoreResult.team_id, ScoreResult.service_id)
        .subquery()
    )
    result_rows = (
        ScoreResult.query.join(ScoreRound, ScoreResult.round_id == ScoreRound.id)
        .join(
            latest_rounds,
            (ScoreResult.team_id == latest_rounds.c.team_id)
            & (ScoreResult.service_id == latest_rounds.c.service_id)
            & (ScoreRound.number == latest_rounds.c.round_number),
        )
        .filter(ScoreRound.competition_id == competition.id)
        .all()
    )
    latest_results = {
        (result.team_id, result.service_id): result for result in result_rows
    }
    return rows, services, latest_results


@main_bp.get("/scoreboard/live/<int:competition_id>")
def scoreboard_live(competition_id):
    competition = db.session.get(Competition, competition_id)
    if competition is None or competition.status != "active":
        return jsonify(error="Competition not found or not active."), 404

    latest_round = (
        ScoreRound.query.filter_by(competition_id=competition.id)
        .order_by(ScoreRound.number.desc())
        .first()
    )
    service_versions = Service.query.filter_by(
        competition_id=competition.id
    ).with_entities(
        Service.id,
        Service.enabled,
        Service.paused,
        Service.hidden_from_scoreboard,
    ).order_by(Service.id).all()
    version_parts = [
        str(competition.round_number),
        f"{latest_round.id}:{latest_round.status}" if latest_round else "no-rounds",
        *(f"{service.id}:{int(service.enabled)}:{int(service.paused)}:{int(service.hidden_from_scoreboard)}"
          for service in service_versions),
        *(f"{team.id}:{team.name}" for team in sorted(competition.teams, key=lambda item: item.id)),
    ]
    etag = hashlib.sha256("|".join(version_parts).encode("utf-8")).hexdigest()
    if request.if_none_match.contains(etag):
        response = make_response("", 304)
        response.set_etag(etag)
        response.headers["Cache-Control"] = "no-cache"
        return response

    rows, services, latest_results = _scoreboard_snapshot(competition)
    response = jsonify(
        competition={
            "id": competition.id,
            "round_number": competition.round_number,
        },
        teams=[
            {
                "id": row["team"].id,
                "name": row["team"].name,
                "score": row["score"],
                "rank": rank,
            }
            for rank, row in enumerate(rows, start=1)
        ],
        services=[
            {
                "id": service.id,
                "group": service.host_group.name if service.host_group else "General",
                "check_type": service.check_type.upper(),
                "enabled": service.enabled,
                "paused": service.paused,
            }
            for service in services
        ],
        results=[
            {
                "team_id": team_id,
                "service_id": service_id,
                "passed": result.passed,
            }
            for (team_id, service_id), result in latest_results.items()
        ],
    )
    response.set_etag(etag)
    response.headers["Cache-Control"] = "no-cache"
    return response


@main_bp.route("/team/checks", methods=["GET", "POST"])
@login_required
def team_checks():
    if current_user.role not in {"team", "admin", "organizer"}:
        abort(403)
    if current_user.role == "team" and current_user.team_id is None:
        abort(403)

    active_competitions = Competition.query.filter_by(status="active").order_by(
        Competition.name
    ).all()
    if current_user.role == "team":
        competitions = [
            competition
            for competition in active_competitions
            if any(team.id == current_user.team_id for team in competition.teams)
        ]
    else:
        competitions = active_competitions

    requested_competition_id = request.values.get("competition", type=int)
    selected_competition = next(
        (
            competition
            for competition in competitions
            if competition.id == requested_competition_id
        ),
        competitions[0] if competitions else None,
    )
    selected_team = None
    teams = []
    services = []
    hosts = []
    if selected_competition is not None:
        teams = sorted(selected_competition.teams, key=lambda team: team.name.casefold())
        if current_user.role == "team":
            selected_team = next(
                (team for team in teams if team.id == current_user.team_id), None
            )
        else:
            requested_team_id = request.values.get("team", type=int)
            selected_team = next(
                (team for team in teams if team.id == requested_team_id),
                teams[0] if teams else None,
            )

    if request.method == "POST":
        if selected_competition is None or selected_team is None:
            abort(404)
        service = Service.query.filter_by(
            id=request.form.get("service_id", type=int),
            competition_id=selected_competition.id,
        ).first()
        if service is None or service.check_type not in TEAM_CREDENTIAL_CHECK_TYPES:
            abort(404)
        host_group_ids = {
            host.host_group_id
            for host in Host.query.filter_by(
                competition_id=selected_competition.id,
                team_id=selected_team.id,
            ).all()
        }
        if service.host_group_id not in host_group_ids:
            abort(404)
        editable_fields = set(service.team_editable_fields or []) & set(
            TEAM_EDITABLE_CREDENTIAL_FIELDS
        )
        if not editable_fields:
            flash("This check has no fields unlocked for team editing.", "error")
            return _team_checks_redirect(selected_competition, selected_team)

        base_token = (service.check_config or {}).get("credentials")
        base_credentials = _decrypt_team_credentials(
            base_token, service.id, "competition"
        )
        configuration = TeamServiceConfig.query.filter_by(
            team_id=selected_team.id,
            service_id=service.id,
        ).first()
        override_credentials = _decrypt_team_credentials(
            configuration.credentials_override if configuration else None,
            service.id,
            selected_team.name,
        )
        if base_credentials is None or override_credentials is None:
            flash(
                "The saved credentials for this check could not be opened. Contact an organizer.",
                "error",
            )
            return _team_checks_redirect(selected_competition, selected_team)

        username = (
            override_credentials[0]
            if "username" in editable_fields and configuration
            else base_credentials[0]
        )
        password = (
            override_credentials[1]
            if "password" in editable_fields and configuration
            else base_credentials[1]
        )
        submitted_username = request.form.get("username", "").strip()
        submitted_password = request.form.get("password", "")
        if "username" in editable_fields and submitted_username:
            if len(submitted_username) > 120:
                flash("Username cannot exceed 120 characters.", "error")
                return _team_checks_redirect(selected_competition, selected_team)
            username = submitted_username
        if "password" in editable_fields and submitted_password:
            if len(submitted_password) > 1024:
                flash("Password cannot exceed 1,024 characters.", "error")
                return _team_checks_redirect(selected_competition, selected_team)
            password = submitted_password

        if not username or not password:
            flash(
                "This check needs both a username and password. Ask an organizer to unlock and configure the missing field.",
                "error",
            )
            return _team_checks_redirect(selected_competition, selected_team)
        if (username, password) == (
            override_credentials[0],
            override_credentials[1],
        ) and configuration:
            flash("No credential changes were submitted.", "info")
            return _team_checks_redirect(selected_competition, selected_team)
        if (
            username == base_credentials[0]
            and password == base_credentials[1]
            and configuration is None
        ):
            flash("No credential changes were submitted.", "info")
            return _team_checks_redirect(selected_competition, selected_team)

        if configuration is None:
            configuration = TeamServiceConfig(
                team_id=selected_team.id,
                service_id=service.id,
            )
            db.session.add(configuration)
        configuration.credentials_override = encrypt_credentials(username, password)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(
                "The credentials could not be saved because another update occurred. Reload the page and try again.",
                "error",
            )
            return _team_checks_redirect(selected_competition, selected_team)
        current_app.logger.info(
            "Team %s updated unlocked fields for service %s: %s",
            selected_team.id,
            service.id,
            ", ".join(sorted(editable_fields)),
        )
        flash(f"Credentials updated for {service.name}.", "success")
        return _team_checks_redirect(selected_competition, selected_team)

    if selected_competition and selected_team:
        host_rows = Host.query.filter_by(
            competition_id=selected_competition.id,
            team_id=selected_team.id,
        ).all()
        hosts = sorted(host_rows, key=lambda host: (host.host_group.name if host.host_group else "").casefold())
        host_group_ids = {host.host_group_id for host in hosts}
        service_host_groups = host_group_ids - {None}
        service_scope = Service.host_group_id.in_(service_host_groups)
        if None in host_group_ids:
            service_scope = or_(service_scope, Service.host_group_id.is_(None))
        service_query = Service.query.filter(
            Service.competition_id == selected_competition.id,
            service_scope,
        )
        for service in service_query.order_by(Service.name).all():
            fields = (
                set(service.team_editable_fields or [])
                & set(TEAM_EDITABLE_CREDENTIAL_FIELDS)
                if service.check_type in TEAM_CREDENTIAL_CHECK_TYPES
                else set()
            )
            configuration = (
                TeamServiceConfig.query.filter_by(
                    team_id=selected_team.id,
                    service_id=service.id,
                ).first()
                if fields
                else None
            )
            username = ""
            credentials_available = True
            if "username" in fields:
                token = (
                    configuration.credentials_override
                    if configuration
                    else (service.check_config or {}).get("credentials")
                )
                credentials = _decrypt_team_credentials(
                    token, service.id, selected_team.name
                )
                if credentials is None:
                    credentials_available = False
                else:
                    username = credentials[0]
            services.append(
                {
                    "service": service,
                    "editable_fields": fields,
                    "username": username,
                    "credentials_available": credentials_available,
                }
            )

    return render_template(
        "team_checks.html",
        competitions=competitions,
        selected_competition=selected_competition,
        teams=teams,
        selected_team=selected_team,
        services=services,
        hosts=hosts,
        is_team=current_user.role == "team",
    )


def _decrypt_team_credentials(token, service_id, team_name):
    if not token:
        return ("", "")
    try:
        return decrypt_credentials(token)
    except ValueError:
        current_app.logger.exception(
            "Could not decrypt credentials for service %s and team %s.",
            service_id,
            team_name,
        )
        return None


def _team_checks_redirect(competition, team):
    values = {"competition": competition.id}
    if current_user.role != "team":
        values["team"] = team.id
    return redirect(url_for("main.team_checks", **values))


@main_bp.post("/scoreboard/hosts/<int:host_id>")
@team_required
def update_scoreboard_host(host_id):
    host = db.session.get(Host, host_id)
    if (
        host is None
        or host.team_id != current_user.team_id
        or host.competition.status != "active"
        or not host.team_can_edit_address
    ):
        flash("This host is not available for address editing.", "error")
        return redirect(url_for("main.team_checks"))

    address = valid_host(request.form.get("address", "").strip())
    if address is None or not address_matches_ranges(address, host.allowed_address_ranges):
        flash("Enter an address included in the allowed range configured for this host.", "error")
        return redirect(url_for("main.team_checks", competition=host.competition_id))

    try:
        address_ip = ipaddress.ip_address(address)
    except ValueError:
        address_ip = None
    if address_ip is not None:
        address = address_ip.compressed
    duplicate = Host.query.filter(
        Host.competition_id == host.competition_id,
        Host.host_group_id == host.host_group_id,
        func.lower(Host.address) == address.casefold(),
        Host.id != host.id,
    ).first()
    if duplicate is not None:
        flash("That address is already assigned to another team in this device group.", "error")
        return redirect(url_for("main.team_checks", competition=host.competition_id))

    host.address = address
    db.session.commit()
    group_name = host.host_group.name if host.host_group else "Scoring host"
    flash(f"{group_name} address updated.", "success")
    return redirect(url_for("main.team_checks", competition=host.competition_id))


@main_bp.route("/healthz")
def healthz():
    return {"status": "ok"}
