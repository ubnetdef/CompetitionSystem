"""Add typed service check configuration."""

from alembic import op
import sqlalchemy as sa


revision = "8d2f3a5c7e91"
down_revision = "41a684496f90"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "services",
        sa.Column("check_type", sa.String(length=20), nullable=False, server_default="http"),
    )
    op.add_column(
        "services",
        sa.Column(
            "check_config",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE services SET check_type = "
            "CASE WHEN protocol IN ('http', 'https') THEN 'http' ELSE protocol END"
        )
    )


def downgrade():
    op.drop_column("services", "check_config")
    op.drop_column("services", "check_type")
