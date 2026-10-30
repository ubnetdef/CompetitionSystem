from datetime import datetime, timezone
import ipaddress
import re

import dns.exception
import dns.resolver
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .check_catalog import (
    CHECK_CATALOG,
    TEAM_CREDENTIAL_CHECK_TYPES,
    TEAM_EDITABLE_CREDENTIAL_FIELDS,
)
from .check_secrets import decrypt_credentials, encrypt_credentials
from .extensions import db
from .models import (
    BankTransaction,
    Competition,
    Host,
    HostGroup,
    IPTemplate,
    Order,
    ScoreRound,
    ScoreResult,
    Service,
    ServiceGroup,
    PlatformSetting,
    Team,
    TeamServiceConfig,
    User,
)
from .security import roles_required
from .utils import parse_amount, valid_address_ranges, valid_host

admin_bp = Blueprint("admin", __name__)


@admin_bp.app_context_processor
def _credential_helpers():
    def saved_username(service):
        token = ((service.check_config or {}).get("credentials")) if service else None
        if not token:
            return ""
        try:
            return decrypt_credentials(token)[0]
        except Exception:
            return ""

    return {"saved_username": saved_username}


@admin_bp.route("/")
@roles_required("admin", "organizer")
def dashboard():
    return render_template(
        "admin_dashboard.html",
        user_count=User.query.count(),
        team_count=User.query.filter_by(role="team").count(),
        competitions=Competition.query.filter_by(is_archived=False)
        .order_by(Competition.created_at.desc())
        .all(),
        recent_rounds=ScoreRound.query.join(Competition)
        .filter(Competition.is_archived.is_(False))
        .order_by(ScoreRound.id.desc())
        .limit(8)
        .all(),
    )


@admin_bp.route("/management-controls", methods=["GET", "POST"])
@roles_required("admin", "organizer")
def management_controls():
    settings = db.session.get(PlatformSetting, 1)
    if request.method == "POST":
        if settings is None:
            settings = PlatformSetting(id=1)
            db.session.add(settings)
        settings.scoreboard_competition_selector_enabled = (
            request.form.get("scoreboard_competition_selector_enabled") == "on"
        )
        db.session.commit()
        flash("Management controls saved.", "success")
        return redirect(url_for("admin.management_controls"))
    return render_template(
        "management_controls.html",
        scoreboard_competition_selector_enabled=bool(
            settings and settings.scoreboard_competition_selector_enabled
        ),
        archived_boards=Competition.query.filter_by(is_archived=True)
        .order_by(Competition.created_at.desc())
        .all(),
    )


@admin_bp.route("/users", methods=["GET", "POST"])
@roles_required("admin", "organizer")
def users():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "")
        team_name = request.form.get("team_name", "").strip()
        team_id = request.form.get("team_id", type=int)
        selected_team = db.session.get(Team, team_id) if team_id else None

        allowed_roles = (
            {"team", "banker", "organizer", "admin"}
            if _is_admin()
            else {"team", "banker"}
        )
        if role not in allowed_roles:
            flash("Choose a role you are permitted to create.", "error")
        elif not username or len(username) > 80:
            flash("Username must contain between 1 and 80 characters.", "error")
        elif len(password) < 12:
            flash("New accounts must use a password with at least 12 characters.", "error")
        elif User.query.filter(func.lower(User.username) == username.lower()).first():
            flash("That username is already in use.", "error")
        elif role == "team" and team_id and (
            selected_team is None or selected_team.user is not None
        ):
            flash("Choose an existing team without an assigned login.", "error")
        elif role == "team" and not team_id and (
            not team_name or len(team_name) > 120
        ):
            flash("Choose an existing team or enter a new team name.", "error")
        elif role == "team" and not team_id and Team.query.filter(func.lower(Team.name) == team_name.lower()).first():
            flash("That team name is already in use.", "error")
        else:
            user = User(username=username, role=role)
            user.set_password(password)
            if role == "team":
                team = selected_team or Team(name=team_name)
                if selected_team is None:
                    db.session.add(team)
                    db.session.flush()
                user.team = team
            db.session.add(user)
            try:
                db.session.commit()
                flash(f"Account '{username}' created.", "success")
                return redirect(url_for("admin.users"))
            except IntegrityError:
                db.session.rollback()
                flash("The account could not be saved because its username or team name is already in use.", "error")

    return render_template(
        "users.html",
        users=User.query.order_by(User.created_at.desc()).all(),
        unassigned_teams=Team.query.filter(~Team.user.has()).order_by(Team.name).all(),
        is_admin=_is_admin(),
        admin_count=User.query.filter_by(role="admin").count(),
    )


@admin_bp.post("/users/<int:user_id>/reset-password")
@roles_required("admin", "organizer")
def reset_password(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        flash("That account no longer exists.", "error")
    elif current_user_role_is_limited() and user.role in {"admin", "organizer"}:
        flash("Organizers cannot reset another administrator or organizer password.", "error")
    else:
        password = request.form.get("password", "")
        if len(password) < 12:
            flash("Use a password with at least 12 characters.", "error")
        else:
            user.set_password(password)
            db.session.commit()
            flash(f"Password reset for '{user.username}'.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.post("/users/<int:user_id>/toggle")
@roles_required("admin", "organizer")
def toggle_user(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        flash("That account no longer exists.", "error")
    elif user.id == current_user.id:
        flash("You cannot disable the account you are currently using.", "error")
    elif current_user_role_is_limited() and user.role in {"admin", "organizer"}:
        flash("Organizers cannot disable another administrator or organizer.", "error")
    else:
        user.is_active = not user.is_active
        db.session.commit()
        flash(f"Account '{user.username}' {'enabled' if user.is_active else 'disabled'}.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.post("/users/<int:user_id>/username")
@roles_required("admin", "organizer")
def update_username(user_id):
    user = db.session.get(User, user_id)
    username = request.form.get("username", "").strip()
    if user is None:
        flash("That account no longer exists.", "error")
    elif current_user_role_is_limited() and user.role in {"admin", "organizer"}:
        flash("Organizers cannot edit another administrator or organizer account.", "error")
    elif not username or len(username) > 80:
        flash("Username must contain between 1 and 80 characters.", "error")
    elif User.query.filter(
        func.lower(User.username) == username.lower(),
        User.id != user.id,
    ).first():
        flash("That username is already in use.", "error")
    else:
        old_username = user.username
        user.username = username
        try:
            db.session.commit()
            flash(f"Username changed from '{old_username}' to '{username}'.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("That username is already in use. No account was changed.", "error")
    return redirect(url_for("admin.users"))


@admin_bp.post("/users/<int:user_id>/delete")
@roles_required("admin", "organizer")
def delete_user(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        flash("That account no longer exists.", "error")
        return redirect(url_for("admin.users"))
    if user.id == current_user.id:
        flash("You cannot remove the account you are currently using.", "error")
        return redirect(url_for("admin.users"))
    if current_user_role_is_limited() and user.role in {"admin", "organizer"}:
        flash("Organizers cannot remove another administrator or organizer account.", "error")
        return redirect(url_for("admin.users"))
    if user.role == "admin" and User.query.filter_by(role="admin").count() <= 1:
        flash("The last administrator account cannot be removed.", "error")
        return redirect(url_for("admin.users"))

    username = user.username
    team = user.team
    try:
        BankTransaction.query.filter_by(created_by_id=user.id).update(
            {BankTransaction.created_by_id: None},
            synchronize_session=False,
        )
        Order.query.filter_by(completed_by_id=user.id).update(
            {Order.completed_by_id: None},
            synchronize_session=False,
        )
        if team is not None:
            BankTransaction.query.filter_by(team_id=team.id).delete(
                synchronize_session=False
            )
            for order in Order.query.filter_by(team_id=team.id).all():
                db.session.delete(order)
            score_result_ids = db.session.query(ScoreResult.id).filter_by(
                team_id=team.id
            )
            BankTransaction.query.filter(
                BankTransaction.score_result_id.in_(score_result_ids)
            ).delete(synchronize_session=False)
            TeamServiceConfig.query.filter_by(team_id=team.id).delete(
                synchronize_session=False
            )
            Host.query.filter_by(team_id=team.id).delete(
                synchronize_session=False
            )
            ScoreResult.query.filter_by(team_id=team.id).delete(
                synchronize_session=False
            )
            team.competitions.clear()
            user.team = None

        db.session.delete(user)
        db.session.flush()
        if team is not None:
            db.session.delete(team)
        db.session.commit()
        flash(
            f"Account '{username}' and its linked team data were permanently removed.",
            "success",
        )
    except IntegrityError:
        db.session.rollback()
        current_app.logger.exception(
            "Could not remove account %s and related data.", user_id
        )
        flash(
            "Account removal failed because related records could not be safely removed. Nothing was changed.",
            "error",
        )
    return redirect(url_for("admin.users"))


@admin_bp.route("/competitions", methods=["GET", "POST"])
@roles_required("admin", "organizer")
def competitions():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        try:
            interval = int(request.form.get("interval_seconds", "60"))
        except ValueError:
            interval = 0
        payout = parse_amount(
            request.form.get(
                "payout_per_point",
                str(current_app.config["DEFAULT_PAYOUT_PER_POINT"]),
            )
        )
        if not name or len(name) > 120:
            flash("Competition name must contain between 1 and 120 characters.", "error")
        elif interval < 15 or interval > 3600:
            flash("Scoring interval must be between 15 and 3,600 seconds.", "error")
        elif payout is None:
            flash("Payout per point must be a positive amount no greater than 1,000,000.00.", "error")
        else:
            competition = Competition(
                name=name,
                description=description[:2000],
                interval_seconds=interval,
                payout_per_point=payout,
            )
            db.session.add(competition)
            db.session.commit()
            flash(f"Competition '{name}' created in draft mode.", "success")
            return redirect(
                url_for(
                    "admin.competition_builder",
                    competition_id=competition.id,
                    step="groups",
                )
            )

    return render_template(
        "competitions.html",
        competitions=Competition.query.filter_by(is_archived=False)
        .order_by(Competition.created_at.desc())
        .all(),
        config=current_app.config,
    )


@admin_bp.post("/competitions/<int:competition_id>/archive")
@roles_required("admin", "organizer")
def archive_competition(competition_id):
    competition = db.session.get(Competition, competition_id)
    if competition is None:
        flash("That scoreboard no longer exists.", "error")
    elif competition.is_archived:
        flash("That scoreboard is already archived.", "info")
    else:
        competition.archived_status = competition.status
        if competition.status == "active":
            competition.status = "paused"
        competition.is_archived = True
        competition.next_round_at = None
        db.session.commit()
        flash(
            f"Scoreboard '{competition.name}' archived. Restore it from Management Controls → Archived boards.",
            "success",
        )
    return redirect(url_for("admin.competitions"))


@admin_bp.post("/competitions/<int:competition_id>/unarchive")
@roles_required("admin", "organizer")
def unarchive_competition(competition_id):
    competition = db.session.get(Competition, competition_id)
    if competition is None:
        flash("That archived scoreboard no longer exists.", "error")
    elif not competition.is_archived:
        flash("That scoreboard is not archived.", "info")
    else:
        competition.status = competition.archived_status or competition.status
        competition.archived_status = None
        competition.is_archived = False
        competition.next_round_at = (
            datetime.now(timezone.utc)
            if competition.status == "active"
            else None
        )
        db.session.commit()
        flash(f"Scoreboard '{competition.name}' restored.", "success")
    return redirect(url_for("admin.management_controls"))


@admin_bp.route(
    "/competitions/<int:competition_id>/builder",
    methods=["GET", "POST"],
)
@roles_required("admin", "organizer")
def competition_builder(competition_id):
    competition = _get_visible_competition(competition_id)
    if competition is None:
        flash("That competition does not exist or is archived.", "error")
        return redirect(url_for("admin.competitions"))

    step_names = ("details", "groups", "templates", "teams", "checks", "review")
    if request.method == "POST":
        step = request.form.get("step", "details")
        action = request.form.get("action", "")
        if action == "save-details":
            _save_competition_details(competition)
        elif action == "save-host-group":
            _save_host_group(competition)
        elif action == "delete-host-group":
            _delete_host_group(competition)
        elif action == "save-group-template":
            _save_group_template(competition)
        elif action == "enroll-team":
            _enroll_builder_team(competition)
        elif action == "save-host-address":
            _save_group_host_address(competition)
        elif action == "add-service":
            _add_service(competition)
        elif action == "update-service":
            service = Service.query.filter_by(
                id=request.form.get("service_id", type=int),
                competition_id=competition.id,
            ).first()
            if service is None:
                flash("That service check no longer exists.", "error")
            else:
                _update_service(competition, service)
        elif action == "activate":
            progress = _builder_progress(competition)
            if not all(progress.values()):
                flash("Complete every setup tab before activating the scoreboard.", "error")
            else:
                _update_status(competition, "active")
        else:
            flash("Unknown scoreboard builder action.", "error")
        return redirect(
            url_for(
                "admin.competition_builder",
                competition_id=competition.id,
                step=step if step in step_names else "details",
            )
        )

    progress = _builder_progress(competition)
    requested_step = request.args.get("step", "details")
    if requested_step not in step_names:
        requested_step = "details"
    requested_index = step_names.index(requested_step)
    highest_open_index = 0
    for index, name in enumerate(step_names[:-1]):
        if progress[name]:
            highest_open_index = index + 1
        else:
            break
    if requested_index > highest_open_index:
        requested_step = step_names[highest_open_index]

    edit_host_group = None
    edit_host_group_id = request.args.get("edit_host_group", type=int)
    if edit_host_group_id:
        edit_host_group = HostGroup.query.filter_by(
            id=edit_host_group_id,
            competition_id=competition.id,
        ).first()
        if edit_host_group is None:
            flash("That device group does not belong to this scoreboard.", "error")
            return redirect(
                url_for(
                    "admin.competition_builder",
                    competition_id=competition.id,
                    step="groups",
                )
            )

    editing_service = None
    edit_service_id = request.args.get("edit_service", type=int)
    if edit_service_id:
        editing_service = Service.query.filter_by(
            id=edit_service_id,
            competition_id=competition.id,
        ).first()
        if editing_service is None:
            flash("That score check does not belong to this scoreboard.", "error")
            return redirect(
                url_for(
                    "admin.competition_builder",
                    competition_id=competition.id,
                    step="checks",
                )
            )

    enrolled_ids = [team.id for team in competition.teams_with_accounts]
    available_teams = (
        Team.query.filter(
            Team.user.has(),
            ~Team.id.in_(enrolled_ids),
        ).order_by(Team.name).all()
        if enrolled_ids
        else Team.query.filter(Team.user.has()).order_by(Team.name).all()
    )
    services = Service.query.filter_by(
        competition_id=competition.id
    ).order_by(Service.name).all()
    host_groups = HostGroup.query.filter_by(
        competition_id=competition.id
    ).order_by(HostGroup.name).all()
    missing_check_templates = [
        (group, check_type)
        for group in host_groups
        for check_type in (group.check_types or [])
        if not any(
            service.host_group_id == group.id
            and service.check_type == check_type
            for service in services
        )
    ]
    group_check_progress = []
    for group in host_groups:
        group_services = [
            service for service in services if service.host_group_id == group.id
        ]
        check_items = []
        for check_type in group.check_types or []:
            matching_services = [
                service
                for service in group_services
                if service.check_type == check_type
            ]
            active_service = next(
                (
                    service
                    for service in matching_services
                    if service.enabled and not service.paused
                ),
                None,
            )
            service = active_service or (
                matching_services[0] if matching_services else None
            )
            check_items.append(
                {
                    "check_type": check_type,
                    "label": CHECK_CATALOG.get(check_type, {}).get(
                        "label", check_type.upper()
                    ),
                    "status": (
                        "complete"
                        if active_service
                        else "warning"
                        if service
                        else "missing"
                    ),
                    "service": service,
                }
            )
        unexpected_services = [
            service
            for service in group_services
            if service.check_type not in (group.check_types or [])
        ]
        for service in unexpected_services:
            check_items.append(
                {
                    "check_type": service.check_type,
                    "label": CHECK_CATALOG.get(service.check_type, {}).get(
                        "label", service.check_type.upper()
                    ),
                    "status": "warning",
                    "service": service,
                    "unexpected": True,
                }
            )
        completed_count = sum(
            item["status"] == "complete" for item in check_items
        )
        group_check_progress.append(
            {
                "group": group,
                "items": check_items,
                "completed_count": completed_count,
                "total_count": len(group.check_types or []),
                "complete": bool(group.check_types)
                and completed_count == len(group.check_types)
                and not unexpected_services,
            }
        )
    return render_template(
        "competition_builder.html",
        competition=competition,
        steps=step_names,
        active_step=requested_step,
        progress=progress,
        max_step_index=highest_open_index,
        templates=IPTemplate.query.filter_by(
            competition_id=competition.id
        ).order_by(IPTemplate.name).all(),
        host_groups=host_groups,
        available_teams=available_teams,
        teams=sorted(competition.teams_with_accounts, key=lambda team: team.id),
        host_matrix={
            team.id: {
                group.id: Host.query.filter_by(
                    competition_id=competition.id,
                    team_id=team.id,
                    host_group_id=group.id,
                ).first()
                for group in competition.host_groups
            }
            for team in competition.teams_with_accounts
        },
        edit_host_group=edit_host_group,
        services=services,
        missing_check_templates=missing_check_templates,
        group_check_progress=group_check_progress,
        editing_service=editing_service,
        selected_host_group=(
            editing_service.host_group
            if editing_service is not None
            else HostGroup.query.filter_by(
                id=request.args.get("host_group_id", type=int),
                competition_id=competition.id,
            ).first()
        ),
        check_catalog=CHECK_CATALOG,
        team_credential_check_types=TEAM_CREDENTIAL_CHECK_TYPES,
        config=current_app.config,
        builder_ready=all(progress.values()),
    )


@admin_bp.route("/dns/expected-answer")
@roles_required("admin", "organizer")
def dns_expected_answer():
    lookup = request.args.get("lookup", "").strip()
    record_type = request.args.get("record_type", "").upper()
    if not valid_host(lookup) or record_type not in {"A", "AAAA", "CNAME", "MX", "TXT"}:
        return jsonify(error="Enter a valid DNS hostname and supported record type."), 400
    try:
        ipaddress.ip_address(lookup.strip("[]"))
    except ValueError:
        pass
    else:
        return jsonify(error="DNS expected answers require a hostname, not an IP address."), 400
    try:
        expected_output = _resolve_dns_expected_output(lookup, record_type)
    except dns.resolver.NXDOMAIN:
        return jsonify(error="The DNS name does not exist."), 422
    except dns.resolver.NoAnswer:
        return jsonify(error=f"No {record_type} record was found for this name."), 422
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as error:
        current_app.logger.warning("DNS expected-answer lookup failed: %s", error)
        return jsonify(error="The current DNS answer could not be resolved. Try again or enter it manually."), 502
    except dns.exception.DNSException as error:
        current_app.logger.warning("DNS expected-answer lookup failed: %s", error)
        return jsonify(error="The current DNS answer could not be resolved. Try again or enter it manually."), 502
    return jsonify(expected_output=expected_output)


def _resolve_dns_expected_output(lookup, record_type):
    resolver = dns.resolver.Resolver(configure=True)
    answer = resolver.resolve(
        lookup.rstrip("."),
        record_type,
        lifetime=2,
        search=False,
    )
    return sorted(record.to_text() for record in answer)[0]


def _builder_progress(competition):
    teams = sorted(competition.teams_with_accounts, key=lambda team: team.id)
    host_groups = list(competition.host_groups)
    generated_hosts_complete = all(
        Host.query.filter_by(
            competition_id=competition.id,
            team_id=team.id,
            host_group_id=group.id,
        ).first()
        is not None
        for team in teams
        for group in host_groups
    )
    progress = {
        "details": bool(
            competition.name
            and len(competition.name) <= 120
            and 15 <= competition.interval_seconds <= 3600
            and competition.payout_per_point is not None
        ),
        "groups": bool(host_groups)
        and all(
            group.check_types
            and all(
                check_type in CHECK_CATALOG and CHECK_CATALOG[check_type]["available"]
                for check_type in group.check_types
            )
            for group in host_groups
        ),
        "templates": bool(host_groups)
        and all(
            group.ip_template is not None
            and _template_is_valid(group.ip_template, len(teams))
            for group in host_groups
        ),
        "teams": bool(teams)
        and (not host_groups or generated_hosts_complete),
        "checks": bool(
            (active_services := Service.query.filter_by(
                competition_id=competition.id,
                enabled=True,
                paused=False,
            ).all())
        )
        and bool(host_groups)
        and all(
            any(
                service.host_group_id == group.id
                and service.check_type == check_type
                for service in active_services
            )
            for group in host_groups
            for check_type in (group.check_types or [])
        )
        and all(
            service.host_group_id in {group.id for group in host_groups}
            and service.check_type in (service.host_group.check_types or [])
            for service in active_services
        ),
    }
    progress["review"] = all(progress.values())
    return progress


def _template_addresses(ip_template, team_count):
    if not _template_is_valid(ip_template, team_count):
        raise ValueError(
            "The IP template must contain one {index} placeholder, use a valid IPv4 "
            "address, and have enough indexes for every enrolled team."
        )
    return [
        ipaddress.IPv4Address(
            ip_template.address_template.replace("{index}", str(index))
        ).compressed
        for index in range(
            ip_template.index_start,
            ip_template.index_start + team_count,
        )
    ]


def _template_is_valid(ip_template, team_count):
    template = ip_template.address_template
    if (
        template.count("{index}") != 1
        or template.replace("{index}", "").count("{")
        or template.replace("{index}", "").count("}")
        or ip_template.index_start < 0
        or ip_template.index_end > 255
        or ip_template.index_start > ip_template.index_end
        or ip_template.index_end - ip_template.index_start + 1 < team_count
    ):
        return False
    try:
        for index in range(ip_template.index_start, ip_template.index_end + 1):
            ipaddress.IPv4Address(template.replace("{index}", str(index)))
    except ipaddress.AddressValueError:
        return False
    return True


def _sync_generated_hosts(competition, host_groups=None, replace_existing=False):
    teams = sorted(competition.teams_with_accounts, key=lambda team: team.id)
    all_groups = list(competition.host_groups)
    groups_to_sync = all_groups if host_groups is None else list(host_groups)
    group_ids_to_sync = {group.id for group in groups_to_sync}
    addresses = {}
    generated = set()
    for group in all_groups:
        if group.ip_template is None:
            if group.id in group_ids_to_sync:
                raise ValueError(
                    f"Configure an IP template for device group '{group.name}' first."
                )
            continue
        group_addresses = _template_addresses(group.ip_template, len(teams))
        for team, address in zip(teams, group_addresses):
            if address in generated:
                raise ValueError(
                    f"Generated address {address} is used by more than one device group."
                )
            generated.add(address)
            if group.id in group_ids_to_sync:
                addresses[(team.id, group.id)] = address
    for (team_id, group_id), address in addresses.items():
        host = Host.query.filter_by(
            competition_id=competition.id,
            team_id=team_id,
            host_group_id=group_id,
        ).first()
        if host is None:
            db.session.add(
                Host(
                    competition_id=competition.id,
                    team_id=team_id,
                    host_group_id=group_id,
                    address=address,
                )
            )
        elif replace_existing:
            host.address = address


def _save_competition_details(competition):
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    try:
        interval = int(request.form.get("interval_seconds", ""))
    except ValueError:
        interval = 0
    payout = parse_amount(request.form.get("payout_per_point", ""))
    if not name or len(name) > 120:
        flash("Scoreboard name must contain between 1 and 120 characters.", "error")
    elif len(description) > 2000:
        flash("Description cannot exceed 2,000 characters.", "error")
    elif not 15 <= interval <= 3600:
        flash("Scoring interval must be between 15 and 3,600 seconds.", "error")
    elif payout is None:
        flash("Credits per point must be a positive amount no greater than 1,000,000.00.", "error")
    else:
        competition.name = name
        competition.description = description
        competition.interval_seconds = interval
        competition.payout_per_point = payout
        db.session.commit()
        flash("Scoreboard details saved.", "success")


def _save_group_template(competition):
    group = HostGroup.query.filter_by(
        id=request.form.get("host_group_id", type=int),
        competition_id=competition.id,
    ).first()
    if group is None:
        flash("That device group does not belong to this scoreboard.", "error")
        return
    address_prefix = request.form.get("address_prefix", "").strip().rstrip(".")
    try:
        ending_octet = int(request.form.get("ending_octet", ""))
        index_start = int(request.form.get("index_start", ""))
        index_end = int(request.form.get("index_end", ""))
    except ValueError:
        ending_octet = -1
        index_start = -1
        index_end = -1

    ip_template = group.ip_template
    if ip_template and any(
        linked_group.id != group.id for linked_group in ip_template.host_groups
    ):
        ip_template = None
    candidate = IPTemplate(
        competition_id=competition.id,
        name=ip_template.name if ip_template else f"{group.name} IPs",
        address_template=f"{address_prefix}.{ending_octet}",
        index_start=index_start,
        index_end=index_end,
    )
    if not 0 <= ending_octet <= 255:
        flash("The ending IP octet must be between 0 and 255.", "error")
    elif IPTemplate.query.filter(
        IPTemplate.competition_id == competition.id,
        IPTemplate.name == candidate.name,
        IPTemplate.id != (ip_template.id if ip_template else 0),
    ).first():
        candidate.name = f"{group.name} IPs ({group.id})"
    if not _template_is_valid(candidate, len(competition.teams_with_accounts)):
        flash(
            "Use one {index} placeholder in a valid IPv4 template and provide an index range with enough addresses for every enrolled team (0–255).",
            "error",
        )
    else:
        if ip_template is None:
            ip_template = candidate
            db.session.add(ip_template)
        else:
            ip_template.address_template = candidate.address_template
            ip_template.index_start = candidate.index_start
            ip_template.index_end = candidate.index_end
        group.ip_template = ip_template
        try:
            db.session.flush()
            _sync_generated_hosts(
                competition,
                host_groups=[group],
                replace_existing=True,
            )
            db.session.commit()
            flash(
                f"IP template for '{group.name}' saved; its host addresses were regenerated.",
                "success",
            )
        except ValueError as error:
            db.session.rollback()
            flash(str(error), "error")


def _save_host_group(competition):
    group_id = request.form.get("host_group_id", type=int)
    group = (
        HostGroup.query.filter_by(
            id=group_id,
            competition_id=competition.id,
        ).first()
        if group_id
        else None
    )
    if group_id and group is None:
        flash("That device group does not belong to this scoreboard.", "error")
        return
    name = request.form.get("host_group_name", "").strip()
    check_types = list(dict.fromkeys(request.form.getlist("check_types")))
    if not name or len(name) > 120:
        flash("Device group name must contain between 1 and 120 characters.", "error")
    elif not check_types:
        flash("Select at least one supported check type for this device group.", "error")
    elif any(
        check_type not in CHECK_CATALOG
        or not CHECK_CATALOG[check_type]["available"]
        for check_type in check_types
    ):
        flash("Choose only check types currently supported by this platform.", "error")
    elif any(
        service.enabled and service.check_type not in check_types
        for service in (group.services if group else [])
    ):
        flash("Remove or reassign checks before removing their type from this device group.", "error")
    elif HostGroup.query.filter(
        HostGroup.competition_id == competition.id,
        HostGroup.name == name,
        HostGroup.id != (group.id if group else 0),
    ).first():
        flash("That device group name is already in use.", "error")
    else:
        if group is None:
            group = HostGroup(competition_id=competition.id)
            db.session.add(group)
        group.name = name
        group.check_types = check_types
        db.session.commit()
        flash(f"Device group '{name}' saved with {len(check_types)} check type(s).", "success")


def _delete_host_group(competition):
    group = HostGroup.query.filter_by(
        id=request.form.get("host_group_id", type=int),
        competition_id=competition.id,
    ).first()
    if group is None:
        flash("That device group no longer exists.", "error")
    elif group.services:
        flash("Reassign or remove its score checks before deleting this device group.", "error")
    else:
        name = group.name
        ip_template = group.ip_template
        db.session.delete(group)
        db.session.flush()
        if ip_template is not None and not ip_template.host_groups:
            db.session.delete(ip_template)
        db.session.commit()
        flash(f"Device group '{name}' and its generated host addresses were deleted.", "success")


def _enroll_builder_team(competition):
    team = db.session.get(Team, request.form.get("team_id", type=int))
    if team is None:
        flash("Choose an existing team.", "error")
        return
    if team.user is None:
        flash("That team has no account and cannot be enrolled in a scoreboard.", "error")
        return
    if team in competition.teams:
        flash("That team is already enrolled.", "info")
        return
    for group in competition.host_groups:
        if group.ip_template is None:
            flash(
                f"Configure an IP template for device group '{group.name}' before enrolling teams.",
                "error",
            )
            return
        if not _template_is_valid(
            group.ip_template,
            len(competition.teams_with_accounts) + 1,
        ):
            flash(
                f"Template '{group.ip_template.name}' needs a larger index range before another team can be enrolled.",
                "error",
            )
            return
    competition.teams.append(team)
    try:
        db.session.flush()
        _sync_generated_hosts(competition)
        db.session.commit()
        flash(f"Team '{team.name}' enrolled and generated hosts assigned.", "success")
    except ValueError as error:
        db.session.rollback()
        flash(str(error), "error")


def _save_group_host_address(competition):
    host = Host.query.filter_by(
        id=request.form.get("host_id", type=int),
        competition_id=competition.id,
    ).first()
    address = request.form.get("address", "").strip()
    team_can_edit_address = request.form.get("team_can_edit_address") == "on"
    allowed_address_ranges = valid_address_ranges(
        request.form.get("allowed_address_ranges", "")
    )
    valid_address = valid_host(address)
    try:
        parsed_address = ipaddress.ip_address(valid_address) if valid_address else None
    except ValueError:
        parsed_address = None
    if host is None or host.host_group_id is None:
        flash("That generated host no longer exists.", "error")
    elif parsed_address is None or parsed_address.version != 4:
        flash("Enter a valid IPv4 address for this device host.", "error")
    elif allowed_address_ranges is None:
        flash("Enter valid comma-separated CIDR ranges or hostnames.", "error")
    elif team_can_edit_address and not allowed_address_ranges:
        flash("Add an allowed address range or hostname before enabling team edits.", "error")
    elif Host.query.filter(
        Host.competition_id == competition.id,
        Host.host_group_id == host.host_group_id,
        Host.address == parsed_address.compressed,
        Host.id != host.id,
    ).first():
        flash("That IPv4 address is already assigned within this device group.", "error")
    else:
        host.address = parsed_address.compressed
        host.team_can_edit_address = team_can_edit_address
        host.allowed_address_ranges = allowed_address_ranges
        db.session.commit()
        flash(f"Host settings saved for '{host.team.name}'.", "success")


@admin_bp.route("/competitions/<int:competition_id>", methods=["GET", "POST"])
@roles_required("admin", "organizer")
def competition_detail(competition_id):
    competition = _get_visible_competition(competition_id)
    if competition is None:
        flash("That competition does not exist or is archived.", "error")
        return redirect(url_for("admin.competitions"))

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add-team":
            _add_competition_team(competition)
        elif action == "add-service":
            _add_service(competition)
        elif action == "add-service-group":
            _add_service_group(competition)
        elif action == "update-service":
            service = Service.query.filter_by(
                id=request.form.get("service_id", type=int),
                competition_id=competition.id,
            ).first()
            if service is None:
                flash("That service check no longer exists.", "error")
            else:
                _update_service(competition, service)
        elif action == "status":
            _update_status(competition)
        else:
            flash("Unknown competition action.", "error")
        if request.form.get("return_to_builder") == "1":
            return redirect(
                url_for(
                    "admin.competition_builder",
                    competition_id=competition.id,
                    step="checks",
                )
            )
        return redirect(url_for("admin.competition_detail", competition_id=competition.id))

    editing_service = None
    if "edit_service" in request.args:
        editing_service = Service.query.filter_by(
            id=request.args.get("edit_service", type=int),
            competition_id=competition.id,
        ).first()
        if editing_service is None:
            flash("That service check does not exist in this competition.", "error")
            return redirect(url_for("admin.competition_detail", competition_id=competition.id))

    teams = competition.teams_with_accounts
    linked_team_ids = [team.id for team in teams]
    available_teams = (
        Team.query.filter(
            Team.user.has(),
            ~Team.id.in_(linked_team_ids),
        ).order_by(Team.name).all()
        if linked_team_ids
        else Team.query.filter(Team.user.has()).order_by(Team.name).all()
    )
    rounds = ScoreRound.query.filter_by(competition_id=competition.id).order_by(ScoreRound.number.desc()).limit(20).all()
    team_hosts = {
        host.team_id: host
        for host in competition.hosts
        if host.host_group_id is None
    }
    standings = (
        db.session.query(
            Team.name,
            func.coalesce(func.sum(ScoreResult.points), 0).label("score"),
        )
        .join(ScoreResult, ScoreResult.team_id == Team.id)
        .join(ScoreRound, ScoreResult.round_id == ScoreRound.id)
        .filter(
            ScoreRound.competition_id == competition.id,
            Team.user.has(),
        )
        .group_by(Team.id, Team.name)
        .order_by(func.sum(ScoreResult.points).desc(), Team.name.asc())
        .all()
    )
    return render_template(
        "competition_detail.html",
        competition=competition,
        teams=teams,
        available_teams=available_teams,
        rounds=rounds,
        standings=standings,
        team_hosts=team_hosts,
        check_catalog=CHECK_CATALOG,
        team_credential_check_types=TEAM_CREDENTIAL_CHECK_TYPES,
        editing_service=editing_service,
        service_groups=ServiceGroup.query.filter_by(
            competition_id=competition.id
        ).order_by(ServiceGroup.name).all(),
        host_groups=HostGroup.query.filter_by(
            competition_id=competition.id
        ).order_by(HostGroup.name).all(),
    )


def _add_competition_team(competition):
    try:
        team_id = int(request.form.get("team_id", ""))
    except ValueError:
        team_id = 0
    team = db.session.get(Team, team_id)
    if team is None:
        flash("Choose an existing team.", "error")
        return
    if team.user is None:
        flash("That team has no account and cannot be enrolled in a scoreboard.", "error")
        return
    if team in competition.teams:
        flash("That team is already enrolled.", "info")
        return
    for group in competition.host_groups:
        if not _template_is_valid(
            group.ip_template,
            len(competition.teams_with_accounts) + 1,
        ):
            flash(
                f"Template '{group.ip_template.name}' needs a larger index range before another team can be enrolled.",
                "error",
            )
            return
    address = request.form.get("address", "").strip()
    if address and not valid_host(address):
        flash("Enter a hostname or IP address without a scheme, port, or path.", "error")
        return

    competition.teams.append(team)
    if address:
        competition.hosts.append(Host(team=team, address=valid_host(address)))
    try:
        db.session.flush()
        _sync_generated_hosts(competition)
        db.session.commit()
    except ValueError as error:
        db.session.rollback()
        flash(str(error), "error")
        return
    flash(f"Team '{team.name}' enrolled.", "success")


@admin_bp.post("/competitions/<int:competition_id>/teams/<int:team_id>/host")
@roles_required("admin", "organizer")
def set_host(competition_id, team_id):
    competition = _get_visible_competition(competition_id)
    team = db.session.get(Team, team_id)
    address = request.form.get("address", "").strip()
    if competition is None or team is None or team.user is None:
        flash("The competition or team no longer exists.", "error")
    elif team not in competition.teams_with_accounts:
        flash("Enroll this team before assigning its host.", "error")
    elif not valid_host(address):
        flash("Enter a hostname or IP address without a scheme, port, or path.", "error")
    else:
        host = Host.query.filter_by(
            competition_id=competition.id,
            team_id=team.id,
            host_group_id=None,
        ).first()
        if host is None:
            host = Host(competition_id=competition.id, team_id=team.id, address=valid_host(address))
            db.session.add(host)
        else:
            host.address = valid_host(address)
        db.session.commit()
        flash(f"Scoring host saved for '{team.name}'.", "success")
    return redirect(url_for("admin.competition_detail", competition_id=competition_id))


def _add_service(competition):
    _save_service(competition)


def _add_service_group(competition):
    name = request.form.get("service_group_name", "").strip()
    display_name = request.form.get("service_group_display_name", "").strip() or name
    if not name or len(name) > 120:
        flash("Service group name must contain between 1 and 120 characters.", "error")
        return
    if len(display_name) > 120:
        flash("Service group display name cannot exceed 120 characters.", "error")
        return
    if ServiceGroup.query.filter_by(
        competition_id=competition.id,
        name=name,
    ).first():
        flash("That service group already exists in this competition.", "error")
        return
    db.session.add(
        ServiceGroup(
            competition_id=competition.id,
            name=name,
            display_name=display_name,
        )
    )
    db.session.commit()
    flash(f"Service group '{display_name}' created.", "success")


def _update_service(competition, service):
    if request.form.get("check_type") != service.check_type:
        flash("A check's type cannot be changed while editing it.", "error")
        return
    _save_service(competition, service)


def _save_service(competition, service=None):
    is_new = service is None
    name = request.form.get("service_name", "").strip()
    check_type = request.form.get("check_type", "")
    try:
        points = int(request.form.get("points", ""))
        points_boost = int(request.form.get("points_boost", "0"))
        timeout = int(request.form.get("timeout_seconds", ""))
    except ValueError:
        flash("Points, points boost, and timeout must be whole numbers.", "error")
        return

    group_id_value = request.form.get("service_group_id", "").strip()
    service_group = None
    if group_id_value:
        try:
            service_group_id = int(group_id_value)
        except ValueError:
            flash("Choose a valid service group.", "error")
            return
        service_group = ServiceGroup.query.filter_by(
            id=service_group_id,
            competition_id=competition.id,
        ).first()
        if service_group is None:
            flash("Choose a service group from this competition.", "error")
            return

    host_group_id_value = request.form.get("host_group_id", "").strip()
    host_group = None
    if host_group_id_value:
        try:
            host_group_id = int(host_group_id_value)
        except ValueError:
            flash("Choose a valid device group.", "error")
            return
        host_group = HostGroup.query.filter_by(
            id=host_group_id,
            competition_id=competition.id,
        ).first()
        if host_group is None:
            flash("Choose a device group from this scoreboard.", "error")
            return

    if not name or len(name) > 120:
        flash("Service name must contain between 1 and 120 characters.", "error")
    elif check_type not in CHECK_CATALOG and not (
        service is not None and check_type == "tcp"
    ):
        flash("Choose a check type from the catalog.", "error")
    elif check_type in CHECK_CATALOG and not CHECK_CATALOG[check_type]["available"]:
        flash(f"{CHECK_CATALOG[check_type]['label']} is cataloged but not enabled.", "error")
    elif not 1 <= points <= 10000:
        flash("Points must be between 1 and 10,000.", "error")
    elif not 0 <= points_boost <= 10000:
        flash("Points Boost must be between 0 and 10,000.", "error")
    elif not 1 <= timeout <= 30:
        flash("Timeout must be between 1 and 30 seconds.", "error")
    elif (
        request.form.get("builder_mode") == "1"
        or request.form.get("return_to_builder") == "1"
    ) and host_group is None:
        flash("Choose the device group this score-check template belongs to.", "error")
    elif (
        (
            request.form.get("builder_mode") == "1"
            or request.form.get("return_to_builder") == "1"
        )
        and check_type not in (host_group.check_types or [])
    ):
        flash("Select a check type enabled for this device group.", "error")
    else:
        requested_team_fields = set(request.form.getlist("team_editable_fields"))
        if (
            requested_team_fields - TEAM_EDITABLE_CREDENTIAL_FIELDS
            or (
                requested_team_fields
                and check_type not in TEAM_CREDENTIAL_CHECK_TYPES
            )
        ):
            flash(
                "Only username and password fields on credential-based checks can be unlocked.",
                "error",
            )
            return
        config, port, protocol, path, expected_status, expected_text = _parse_check_config(
            check_type, service
        )
        if config is None:
            return
        if service is None:
            service = Service(competition_id=competition.id)
            db.session.add(service)
        service.name = name
        service.check_type = check_type
        service.check_config = config
        service.protocol = protocol
        service.port = port
        service.path = path
        service.expected_status = expected_status
        service.expected_text = expected_text[:255] if expected_text else None
        service.points = points
        service.points_boost = points_boost
        service.timeout_seconds = timeout
        service.team_editable_fields = sorted(requested_team_fields)
        if "enabled" in request.form:
            service.enabled = "on" in request.form.getlist("enabled")
        service.paused = request.form.get("paused") == "on"
        service.hidden_from_scoreboard = (
            request.form.get("hidden_from_scoreboard") == "on"
        )
        service.service_group = service_group
        service.host_group = host_group
        db.session.commit()
        verb = "added" if is_new else "updated"
        flash(f"Service check '{name}' {verb}.", "success")


def _legacy_command_fields():
    command = request.form.get("legacy_command", "").strip()
    expected = request.form.get("legacy_expected_output", "").strip()
    if not command or len(command) > 200 or any(ord(char) < 32 for char in command):
        flash("Enter a single-line legacy command of at most 200 characters.", "error")
        return None
    if not expected or len(expected) > 500:
        flash("Enter the expected command output (1–500 characters).", "error")
        return None
    return command, expected


def _parse_check_config(check_type, existing_service=None):
    config = {}
    port = 0
    protocol = check_type
    path = "/"
    expected_status = 200
    expected_text = None
    legacy = request.form.get("legacy_mode") == "on"

    if check_type == "http":
        scheme = request.form.get("scheme", "")
        subdomain = request.form.get("subdomain", "").strip()
        path = request.form.get("path", "/").strip() or "/"
        expected_text = request.form.get("expected_text", "").strip() or None
        try:
            port = int(request.form.get("port", ""))
            expected_status = int(request.form.get("expected_status", "200"))
        except ValueError:
            flash("Port and expected HTTP status must be whole numbers.", "error")
            return None, 0, protocol, path, expected_status, expected_text
        if scheme not in {"http", "https"}:
            flash("Choose HTTP or HTTPS.", "error")
            return None, port, protocol, path, expected_status, expected_text
        if subdomain and not valid_host(subdomain):
            flash("Enter a valid subdomain or leave the field blank.", "error")
            return None, port, protocol, path, expected_status, expected_text
        if not 1 <= port <= 65535:
            flash("Port must be between 1 and 65,535.", "error")
            return None, port, protocol, path, expected_status, expected_text
        if not 100 <= expected_status <= 599:
            flash("Expected HTTP status must be between 100 and 599.", "error")
            return None, port, protocol, path, expected_status, expected_text
        if len(path) > 255 or not path.startswith("/"):
            flash("HTTP path must begin with / and be no longer than 255 characters.", "error")
            return None, port, protocol, path, expected_status, expected_text
        if expected_text and len(expected_text) > 255:
            flash("Expected response text cannot exceed 255 characters.", "error")
            return None, port, protocol, path, expected_status, expected_text
        protocol = scheme
        config = {"scheme": scheme, "subdomain": subdomain}
    elif check_type == "tcp":
        try:
            port = int(request.form.get("port", ""))
        except ValueError:
            flash("TCP port must be a whole number.", "error")
            return None, port, protocol, path, expected_status, expected_text
        if not 1 <= port <= 65535:
            flash("TCP port must be between 1 and 65,535.", "error")
            return None, port, protocol, path, expected_status, expected_text
    elif check_type == "dns":
        lookup = request.form.get("lookup", "").strip()
        record_type = request.form.get("record_type", "").upper()
        expected_output = request.form.get("expected_output", "").strip()
        if legacy and not expected_output:
            expected_output = "{address}"
        if not valid_host(lookup):
            flash("Enter a valid DNS hostname to look up.", "error")
            return None, port, protocol, path, expected_status, expected_text
        try:
            ipaddress.ip_address(lookup.strip("[]"))
        except ValueError:
            pass
        else:
            flash("Enter a DNS hostname, not an IP address.", "error")
            return None, port, protocol, path, expected_status, expected_text
        if record_type not in {"A", "AAAA", "CNAME", "MX", "TXT"}:
            flash("Choose a supported DNS record type.", "error")
            return None, port, protocol, path, expected_status, expected_text
        if not expected_output and not legacy:
            try:
                expected_output = _resolve_dns_expected_output(lookup, record_type)
            except dns.resolver.NXDOMAIN:
                flash("DNS name does not exist; enter an expected answer manually.", "error")
                return None, port, protocol, path, expected_status, expected_text
            except dns.resolver.NoAnswer:
                flash(
                    f"No {record_type} record was found; enter an expected answer manually.",
                    "error",
                )
                return None, port, protocol, path, expected_status, expected_text
            except dns.exception.DNSException as error:
                current_app.logger.warning("DNS expected-answer lookup failed: %s", error)
                flash(
                    "The current DNS answer could not be resolved. Enter the expected answer manually.",
                    "error",
                )
                return None, port, protocol, path, expected_status, expected_text
        if len(expected_output) > 500:
            flash("Expected DNS output cannot exceed 500 characters.", "error")
            return None, port, protocol, path, expected_status, expected_text
        config = {
            "lookup": lookup.rstrip("."),
            "record_type": record_type,
            "expected_output": expected_output,
        }
        if legacy:
            config["query_host"] = True
    elif check_type == "ping":
        try:
            attempts = int(request.form.get("attempts", ""))
        except ValueError:
            flash("PING attempts must be a whole number.", "error")
            return None, port, protocol, path, expected_status, expected_text
        if attempts < 1 or attempts > 4:
            flash("Choose between 1 and 4 PING attempts.", "error")
            return None, port, protocol, path, expected_status, expected_text
        protocol = "icmp"
        config = {"attempts": attempts}
    elif check_type in {"ssh", "winrm", "ldap", "sql", "ftp"}:
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        old_credentials = (
            (existing_service.check_config or {}).get("credentials")
            if existing_service is not None
            else None
        )
        if old_credentials and (username or password):
            try:
                old_username, old_password = decrypt_credentials(old_credentials)
            except Exception:
                old_username, old_password = "", ""
            username = username or old_username
            password = password or old_password
        if username or password:
            if not username or len(username) > 120 or not password or len(password) > 1024:
                flash("Enter a username (up to 120 characters) and password (up to 1,024 characters).", "error")
                return None, port, protocol, path, expected_status, expected_text
            config["credentials"] = encrypt_credentials(username, password)
        elif old_credentials:
            config["credentials"] = old_credentials
        else:
            flash("Enter a username and password for this check.", "error")
            return None, port, protocol, path, expected_status, expected_text

        if check_type == "ssh":
            fingerprint = request.form.get("host_key_fingerprint", "").strip()
            skip_host_key_verification = (
                request.form.get("skip_host_key_verification") == "on"
            )
            try:
                port = int(request.form.get("port", "22"))
            except ValueError:
                port = 0
            if not 1 <= port <= 65535:
                flash("SSH port must be between 1 and 65,535.", "error")
                return None, port, protocol, path, expected_status, expected_text
            if skip_host_key_verification:
                config["host_key_verification"] = "skip"
            elif not fingerprint.startswith("SHA256:") or len(fingerprint) > 60:
                flash(
                    "Enter the SSH server's SHA256 host-key fingerprint or explicitly skip host-key verification.",
                    "error",
                )
                return None, port, protocol, path, expected_status, expected_text
            else:
                config["host_key_fingerprint"] = fingerprint
            if legacy:
                command_fields = _legacy_command_fields()
                if command_fields is None:
                    return None, port, protocol, path, expected_status, expected_text
                config.update(
                    legacy=True,
                    command=command_fields[0],
                    expected_output=command_fields[1],
                )
        elif check_type == "winrm":
            try:
                port = int(request.form.get("port", "5986"))
            except ValueError:
                port = 0
            auth_transport = request.form.get("auth_transport", "")
            if not 1 <= port <= 65535:
                flash("WinRM port must be between 1 and 65,535.", "error")
                return None, port, protocol, path, expected_status, expected_text
            if auth_transport not in {"ntlm", "basic"}:
                flash("Choose NTLM or Basic authentication for WinRM.", "error")
                return None, port, protocol, path, expected_status, expected_text
            protocol = "https"
            config["auth_transport"] = auth_transport
            if legacy:
                scheme = request.form.get("legacy_scheme", "http")
                command_fields = _legacy_command_fields()
                if scheme not in {"http", "https"}:
                    flash("Choose HTTP or HTTPS for the legacy WinRM endpoint.", "error")
                    return None, port, protocol, path, expected_status, expected_text
                if command_fields is None:
                    return None, port, protocol, path, expected_status, expected_text
                protocol = scheme
                config.update(
                    legacy=True,
                    scheme=scheme,
                    command=command_fields[0],
                    expected_output=command_fields[1],
                )
        elif check_type == "ldap":
            try:
                port = int(request.form.get("port", "636"))
            except ValueError:
                port = 0
            base_dn = request.form.get("base_dn", "").strip()
            search_filter = request.form.get("search_filter", "").strip()
            attributes = [
                value.strip()
                for value in request.form.get("attributes", "").split(",")
                if value.strip()
            ]
            expected_output = request.form.get("expected_output", "").strip()
            domain = request.form.get("legacy_domain", "").strip() if legacy else ""
            if legacy:
                if not domain or not valid_host(domain):
                    flash("Enter the Active Directory domain name, such as example.local.", "error")
                    return None, port, protocol, path, expected_status, expected_text
                if not base_dn:
                    base_dn = ",".join(f"dc={part}" for part in domain.split("."))
            if not 1 <= port <= 65535 or not base_dn or len(base_dn) > 500:
                flash("Enter a valid LDAP port and base DN.", "error")
                return None, port, protocol, path, expected_status, expected_text
            if not search_filter.startswith("(") or not search_filter.endswith(")") or len(search_filter) > 500:
                flash("Enter a valid LDAP search filter, such as (objectClass=person).", "error")
                return None, port, protocol, path, expected_status, expected_text
            if not attributes or any(not value.replace("-", "").replace("_", "").isalnum() for value in attributes):
                flash("Enter one or more comma-separated LDAP attribute names.", "error")
                return None, port, protocol, path, expected_status, expected_text
            if (not expected_output and not legacy) or len(expected_output) > 500:
                flash("Expected LDAP output must contain between 1 and 500 characters.", "error")
                return None, port, protocol, path, expected_status, expected_text
            config.update(
                base_dn=base_dn,
                search_filter=search_filter,
                attributes=attributes,
                expected_output=expected_output,
            )
            if legacy:
                config.update(legacy=True, use_ssl=False, domain=domain)
        elif check_type == "sql":
            try:
                port = int(request.form.get("port", "3306"))
            except ValueError:
                port = 0
            database_name = request.form.get("database_name", "").strip()
            if not 1 <= port <= 65535:
                flash("MySQL port must be between 1 and 65,535.", "error")
                return None, port, protocol, path, expected_status, expected_text
            if not database_name or len(database_name) > 64 or not database_name.replace("_", "").isalnum():
                flash("Enter a valid MySQL database name.", "error")
                return None, port, protocol, path, expected_status, expected_text
            config["database_name"] = database_name
            if legacy:
                sql_command = request.form.get("legacy_sql_command", "").strip().rstrip(";").strip()
                try:
                    min_rows = int(request.form.get("legacy_min_rows", ""))
                    max_rows = int(request.form.get("legacy_max_rows", ""))
                except ValueError:
                    min_rows, max_rows = -1, -1
                if (
                    not re.fullmatch(r"(?is)select\s[^;]{1,500}", sql_command)
                    or any(ord(char) < 32 and char not in "\t\n" for char in sql_command)
                ):
                    flash("Legacy SQL must be a single SELECT statement of at most 500 characters.", "error")
                    return None, port, protocol, path, expected_status, expected_text
                if not 0 <= min_rows <= max_rows <= 10000:
                    flash("Enter minimum and maximum expected rows (0–10,000), with minimum not above maximum.", "error")
                    return None, port, protocol, path, expected_status, expected_text
                config.update(
                    legacy=True,
                    use_tls=False,
                    command=sql_command,
                    min_rows=min_rows,
                    max_rows=max_rows,
                )
        elif check_type == "ftp":
            try:
                port = int(request.form.get("port", "21"))
            except ValueError:
                port = 0
            filename = request.form.get("read_filename", "").strip()
            expected_output = request.form.get("expected_output", "").strip()
            if not 1 <= port <= 65535:
                flash("FTPS port must be between 1 and 65,535.", "error")
                return None, port, protocol, path, expected_status, expected_text
            if (
                not filename
                or len(filename) > 255
                or "\x00" in filename
                or ".." in filename.split("/")
                or any(ord(char) < 32 or ord(char) == 127 for char in filename)
            ):
                flash("Enter a safe remote file path of at most 255 characters.", "error")
                return None, port, protocol, path, expected_status, expected_text
            if not expected_output or len(expected_output) > 500:
                flash("Expected file text must contain between 1 and 500 characters.", "error")
                return None, port, protocol, path, expected_status, expected_text
            config.update(read_filename=filename, expected_output=expected_output)
            if legacy:
                write_filename = request.form.get("legacy_write_filename", "").strip()
                write_text = request.form.get("legacy_write_text", "")
                if write_filename and (
                    len(write_filename) > 255
                    or "\x00" in write_filename
                    or ".." in write_filename.split("/")
                    or any(ord(char) < 32 or ord(char) == 127 for char in write_filename)
                    or len(write_text) > 500
                ):
                    flash("Enter a safe remote write path (255 characters) and text up to 500 characters.", "error")
                    return None, port, protocol, path, expected_status, expected_text
                config.update(legacy=True, use_tls=False)
                if write_filename:
                    config.update(write_filename=write_filename, write_text=write_text)

    return config, port, protocol, path, expected_status, expected_text


def _update_status(competition, status=None):
    status = status or request.form.get("status", "")
    if status not in {"draft", "active", "paused", "completed"}:
        flash("Choose a valid competition status.", "error")
        return
    if status == "active" and not competition.teams_with_accounts:
        flash("Enroll at least one team before activating the competition.", "error")
        return
    if status == "active" and not competition.services:
        flash("Add at least one service check before activating the competition.", "error")
        return

    competition.status = status
    if status == "active":
        competition.next_round_at = datetime.now(timezone.utc)
    else:
        competition.next_round_at = None
    db.session.commit()
    flash(f"Competition status changed to {status}.", "success")


def _is_admin():
    from flask_login import current_user

    return current_user.role == "admin"


def _get_visible_competition(competition_id):
    competition = db.session.get(Competition, competition_id)
    if competition is None or competition.is_archived:
        return None
    return competition


def current_user_role_is_limited():
    return not _is_admin()
