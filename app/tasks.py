import logging
from datetime import datetime, timedelta, timezone

from . import create_app
from .extensions import db
from .models import Competition, ScoreRound
from .scoring import process_scoring_round

logger = logging.getLogger(__name__)


def schedule_due_rounds(flask_app=None):
    app = flask_app or create_app()
    with app.app_context():
        now = datetime.now(timezone.utc)
        due = (
            Competition.query.filter(
                Competition.status == "active",
                Competition.is_archived.is_(False),
                (Competition.next_round_at.is_(None))
                | (Competition.next_round_at <= now),
            )
            .with_for_update(skip_locked=True)
            .all()
        )
        for competition in due:
            competition.round_number += 1
            competition.next_round_at = now + timedelta(
                seconds=competition.interval_seconds
            )
            db.session.add(
                ScoreRound(
                    competition_id=competition.id,
                    number=competition.round_number,
                    status="queued",
                )
            )
        db.session.commit()
        return len(due)


def claim_next_round(flask_app=None):
    app = flask_app or create_app()
    with app.app_context():
        score_round = (
            ScoreRound.query.filter_by(status="queued")
            .filter(ScoreRound.competition.has(Competition.is_archived.is_(False)))
            .order_by(ScoreRound.id)
            .with_for_update(skip_locked=True)
            .first()
        )
        if score_round is None:
            return None

        score_round.status = "running"
        score_round.started_at = datetime.now(timezone.utc)
        round_id = score_round.id
        db.session.commit()
        return round_id


def run_scoring_round(round_id, flask_app=None):
    app = flask_app or create_app()
    try:
        process_scoring_round(round_id, app, claimed=True)
    except Exception as error:
        logger.exception("Scoring round %s failed unexpectedly.", round_id)
        with app.app_context():
            db.session.rollback()
            score_round = db.session.get(ScoreRound, round_id)
            if score_round is not None and score_round.status == "running":
                score_round.status = "failed"
                score_round.error_message = (
                    f"Scoring could not complete: {str(error)[:350]}"
                )
                score_round.completed_at = datetime.now(timezone.utc)
                db.session.commit()
