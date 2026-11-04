"""Add global score-check hint control."""

from alembic import op
import sqlalchemy as sa


revision = "4c7a2e9b1d6f"
down_revision = "1f0a8c9d6e42"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "platform_settings",
        sa.Column(
            "score_check_hints_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column(
        "platform_settings",
        "score_check_hints_enabled",
        server_default=None,
    )


def downgrade():
    op.drop_column("platform_settings", "score_check_hints_enabled")
