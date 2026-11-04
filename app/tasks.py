import logging
from datetime import datetime, timedelta, timezone

from . import create_app
from .extensions import db
from .models import Competition, ScoreRound
from .scoring import process_scoring_round

logger = logging.getLogger(__name__)
ROUND_HEARTBEAT_SECONDS = 10
STALE_ROUND_SECONDS = 90


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
        scheduled = 0
        for competition in due:
            pending_round = ScoreRound.query.filter(
                ScoreRound.competition_id == competition.id,
                ScoreRound.status.in_(("queued", "running")),
            ).first()
            if pending_round is not None:
                competition.next_round_at = now + timedelta(
                    seconds=competition.interval_seconds
                )
                continue

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
            scheduled += 1
        db.session.commit()
        return scheduled


def recover_stale_rounds(flask_app=None, stale_after_seconds=STALE_ROUND_SECONDS):
    app = flask_app or create_app()
    with app.app_context():
        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(seconds=stale_after_seconds)
        stale_rounds = (
            ScoreRound.query.filter_by(status="running")
            .filter(
                (db.func.coalesce(ScoreRound.heartbeat_at, ScoreRound.started_at) < stale_before)
                | (ScoreRound.started_at.is_(None))
            )
            .with_for_update(skip_locked=True)
            .all()
        )
        for score_round in stale_rounds:
            score_round.status = "failed"
            score_round.error_message = (
                "Scoring worker stopped reporting progress; the abandoned round "
                "was marked failed."
            )
            score_round.completed_at = now
            competition = score_round.competition
            if competition is not None and competition.status == "active":
                next_due = now + timedelta(seconds=competition.interval_seconds)
                if competition.next_round_at is None or competition.next_round_at < next_due:
                    competition.next_round_at = next_due

        if stale_rounds:
            db.session.commit()
            logger.warning(
                "Marked %s abandoned scoring round(s) failed: %s",
                len(stale_rounds),
                ", ".join(str(score_round.id) for score_round in stale_rounds),
            )
        return len(stale_rounds)


def claim_next_round(flask_app=None):
    app = flask_app or create_app()
    with app.app_context():
        queued_rounds = (
            db.session.query(ScoreRound.id, ScoreRound.competition_id)
            .join(Competition)
            .filter(
                ScoreRound.status == "queued",
                Competition.is_archived.is_(False),
            )
            .order_by(ScoreRound.id)
            .all()
        )

        for round_id, competition_id in queued_rounds:
            competition = (
                Competition.query.filter_by(id=competition_id)
                .with_for_update(skip_locked=True)
                .first()
            )
            if competition is None:
                continue
            if ScoreRound.query.filter_by(
                competition_id=competition_id,
                status="running",
            ).first():
                continue

            score_round = (
                ScoreRound.query.filter_by(id=round_id, status="queued")
                .with_for_update(skip_locked=True)
                .first()
            )
            if score_round is None:
                continue

            now = datetime.now(timezone.utc)
            score_round.status = "running"
            score_round.started_at = now
            score_round.heartbeat_at = now
            db.session.commit()
            return round_id
        return None


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
