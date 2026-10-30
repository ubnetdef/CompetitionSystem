from flask import Blueprint, jsonify
from flask_login import current_user, login_required
from sqlalchemy import func

from .bank import balance_for
from .extensions import db
from .models import Competition, ScoreResult, ScoreRound, Team
from .security import team_required

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")


@api_bp.get("/competitions")
def competitions():
    active = Competition.query.filter_by(status="active").order_by(Competition.name).all()
    return jsonify(
        competitions=[
            {
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "status": item.status,
                "round_interval_seconds": item.interval_seconds,
                "round_number": item.round_number,
            }
            for item in active
        ]
    )


@api_bp.get("/competitions/<int:competition_id>/scoreboard")
def competition_scoreboard(competition_id):
    competition = db.session.get(Competition, competition_id)
    if competition is None or competition.status not in {"active", "completed"}:
        return jsonify(error="Competition not found or not publicly available."), 404

    standings = (
        db.session.query(
            Team.id,
            Team.name,
            func.coalesce(func.sum(ScoreResult.points), 0).label("points"),
        )
        .join(ScoreResult, ScoreResult.team_id == Team.id)
        .join(ScoreRound, ScoreResult.round_id == ScoreRound.id)
        .filter(ScoreRound.competition_id == competition.id)
        .group_by(Team.id, Team.name)
        .order_by(func.sum(ScoreResult.points).desc(), Team.name.asc())
        .all()
    )
    return jsonify(
        competition={"id": competition.id, "name": competition.name},
        standings=[
            {"rank": rank, "team": name, "points": points}
            for rank, (_, name, points) in enumerate(standings, start=1)
        ],
    )


@api_bp.get("/me")
@login_required
def me():
    team = db.session.get(Team, current_user.team_id) if current_user.team_id else None
    return jsonify(
        username=current_user.username,
        role=current_user.role,
        team={"id": team.id, "name": team.name} if team else None,
    )


@api_bp.get("/me/bank")
@team_required
def my_bank():
    return jsonify(
        team_id=current_user.team_id,
        balance=str(balance_for(current_user.team_id)),
    )
