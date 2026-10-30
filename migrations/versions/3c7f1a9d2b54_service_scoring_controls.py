"""Add legacy service scoring controls and metadata groups."""

from alembic import op
import sqlalchemy as sa


revision = "3c7f1a9d2b54"
down_revision = "91bc7d4a2e60"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "service_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("competition_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.ForeignKeyConstraint(["competition_id"], ["competitions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "competition_id",
            "name",
            name="uq_service_group_competition_name",
        ),
    )
    op.create_index(
        op.f("ix_service_groups_competition_id"),
        "service_groups",
        ["competition_id"],
        unique=False,
    )
    op.add_column(
        "services",
        sa.Column(
            "points_boost",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "services",
        sa.Column(
            "paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "services",
        sa.Column(
            "hidden_from_scoreboard",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "services",
        sa.Column("service_group_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_services_service_group_id",
        "services",
        "service_groups",
        ["service_group_id"],
        ["id"],
    )


def downgrade():
    op.drop_constraint(
        "fk_services_service_group_id",
        "services",
        type_="foreignkey",
    )
    op.drop_column("services", "service_group_id")
    op.drop_column("services", "hidden_from_scoreboard")
    op.drop_column("services", "paused")
    op.drop_column("services", "points_boost")
    op.drop_index(
        op.f("ix_service_groups_competition_id"),
        table_name="service_groups",
    )
    op.drop_table("service_groups")
