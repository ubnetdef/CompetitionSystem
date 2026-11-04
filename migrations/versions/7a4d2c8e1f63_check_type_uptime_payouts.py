"""Add global per-check-type uptime reward settings."""

from alembic import op
import sqlalchemy as sa


revision = "7a4d2c8e1f63"
down_revision = "2e9b4c7d1a63"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "platform_settings",
        sa.Column(
            "check_type_payouts",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.alter_column(
        "platform_settings",
        "check_type_payouts",
        server_default=None,
    )


def downgrade():
    op.drop_column("platform_settings", "check_type_payouts")
