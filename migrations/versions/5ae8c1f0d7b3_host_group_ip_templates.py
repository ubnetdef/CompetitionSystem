"""Add host groups, IPv4 templates, and per-group scoring targets."""

from alembic import op
import sqlalchemy as sa


revision = "5ae8c1f0d7b3"
down_revision = "3c7f1a9d2b54"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ip_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("competition_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("address_template", sa.String(length=253), nullable=False),
        sa.Column("index_start", sa.Integer(), nullable=False),
        sa.Column("index_end", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["competition_id"], ["competitions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "competition_id",
            "name",
            name="uq_ip_template_competition_name",
        ),
    )
    op.create_index(
        op.f("ix_ip_templates_competition_id"),
        "ip_templates",
        ["competition_id"],
        unique=False,
    )
    op.create_table(
        "host_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("competition_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("ip_template_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["competition_id"], ["competitions.id"]),
        sa.ForeignKeyConstraint(["ip_template_id"], ["ip_templates.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "competition_id",
            "name",
            name="uq_host_group_competition_name",
        ),
    )
    op.create_index(
        op.f("ix_host_groups_competition_id"),
        "host_groups",
        ["competition_id"],
        unique=False,
    )
    op.drop_constraint(
        "uq_host_competition_team",
        "hosts",
        type_="unique",
    )
    op.add_column(
        "hosts",
        sa.Column("host_group_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_hosts_host_group_id",
        "hosts",
        "host_groups",
        ["host_group_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_host_competition_team_group",
        "hosts",
        ["competition_id", "team_id", "host_group_id"],
    )
    op.add_column(
        "services",
        sa.Column("host_group_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_services_host_group_id",
        "services",
        "host_groups",
        ["host_group_id"],
        ["id"],
    )


def downgrade():
    op.drop_constraint(
        "fk_services_host_group_id",
        "services",
        type_="foreignkey",
    )
    op.drop_column("services", "host_group_id")
    op.drop_constraint(
        "uq_host_competition_team_group",
        "hosts",
        type_="unique",
    )
    op.drop_constraint(
        "fk_hosts_host_group_id",
        "hosts",
        type_="foreignkey",
    )
    op.drop_column("hosts", "host_group_id")
    op.create_unique_constraint(
        "uq_host_competition_team",
        "hosts",
        ["competition_id", "team_id"],
    )
    op.drop_index(
        op.f("ix_host_groups_competition_id"),
        table_name="host_groups",
    )
    op.drop_table("host_groups")
    op.drop_index(
        op.f("ix_ip_templates_competition_id"),
        table_name="ip_templates",
    )
    op.drop_table("ip_templates")
