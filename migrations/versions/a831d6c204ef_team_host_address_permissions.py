"""Add team-controlled host address permissions."""

from alembic import op
import sqlalchemy as sa


revision = "a831d6c204ef"
down_revision = "2c0f1a9d8e6b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "hosts",
        sa.Column(
            "team_can_edit_address",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "hosts",
        sa.Column(
            "allowed_address_ranges",
            sa.String(length=1000),
            nullable=False,
            server_default="",
        ),
    )


def downgrade():
    op.drop_column("hosts", "allowed_address_ranges")
    op.drop_column("hosts", "team_can_edit_address")
