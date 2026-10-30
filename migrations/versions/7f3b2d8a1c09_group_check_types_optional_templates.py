"""Allow group-first setup and store its configured check types."""

from alembic import op
import sqlalchemy as sa


revision = "7f3b2d8a1c09"
down_revision = "5ae8c1f0d7b3"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "host_groups",
        "ip_template_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.add_column(
        "host_groups",
        sa.Column(
            "check_types",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade():
    op.drop_column("host_groups", "check_types")
    op.alter_column(
        "host_groups",
        "ip_template_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
