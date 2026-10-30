"""Normalize pre-existing HTTPS service check types."""

from alembic import op
import sqlalchemy as sa


revision = "91bc7d4a2e60"
down_revision = "8d2f3a5c7e91"
branch_labels = None
depends_on = None


def upgrade():
    services = sa.table(
        "services",
        sa.column("id", sa.Integer),
        sa.column("check_type", sa.String),
        sa.column("check_config", sa.JSON),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(services.c.id, services.c.check_config).where(
            services.c.check_type == "https"
        )
    )
    for row in rows:
        config = dict(row.check_config or {})
        config["scheme"] = "https"
        bind.execute(
            services.update()
            .where(services.c.id == row.id)
            .values(check_type="http", check_config=config)
        )


def downgrade():
    services = sa.table(
        "services",
        sa.column("id", sa.Integer),
        sa.column("check_type", sa.String),
        sa.column("protocol", sa.String),
        sa.column("check_config", sa.JSON),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(services.c.id, services.c.check_config).where(
            services.c.check_type == "http",
            services.c.protocol == "https",
        )
    )
    for row in rows:
        config = dict(row.check_config or {})
        config.pop("scheme", None)
        bind.execute(
            services.update()
            .where(services.c.id == row.id)
            .values(check_type="https", check_config=config)
        )
