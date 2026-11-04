"""Track scoring worker progress for round recovery."""

from alembic import op
import sqlalchemy as sa


revision = "2e9b4c7d1a63"
down_revision = "6c2a9e3f1b74"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "score_rounds",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("score_rounds", "heartbeat_at")
