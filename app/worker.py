import logging
import time

from . import create_app
from .tasks import claim_next_round, run_scoring_round, schedule_due_rounds


def run_worker(flask_app=None):
    app = flask_app or create_app()
    tick_seconds = app.config["SCORING_TICK_SECONDS"]
    app.logger.info("Database-backed scoring worker started.")

    while True:
        try:
            schedule_due_rounds(app)
        except Exception:
            app.logger.exception("Could not schedule due scoring rounds.")
            with app.app_context():
                from .extensions import db

                db.session.rollback()

        while True:
            try:
                round_id = claim_next_round(app)
            except Exception:
                app.logger.exception("Could not claim a queued scoring round.")
                with app.app_context():
                    from .extensions import db

                    db.session.rollback()
                break

            if round_id is None:
                break
            run_scoring_round(round_id, app)

        time.sleep(tick_seconds)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_worker()
