"""Add archiving for competition scoreboards."""

from alembic import op
import sqlalchemy as sa


revision = "1f0a8c9d6e42"
down_revision = "9d8b3828dd16"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "competitions",
        sa.Column(
            "is_archived",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "competitions",
        sa.Column("archived_status", sa.String(length=20), nullable=True),
    )
    with op.batch_alter_table("competitions") as batch_op:
        batch_op.create_index(
            "ix_competitions_is_archived",
            ["is_archived"],
            unique=False,
        )
        batch_op.alter_column("is_archived", server_default=None)


def downgrade():
    with op.batch_alter_table("competitions") as batch_op:
        batch_op.drop_index("ix_competitions_is_archived")
    op.drop_column("competitions", "archived_status")
    op.drop_column("competitions", "is_archived")
