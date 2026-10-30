"""Initialize migrated device groups from their existing score checks."""

from alembic import op
import sqlalchemy as sa


revision = "2c0f1a9d8e6b"
down_revision = "7f3b2d8a1c09"
branch_labels = None
depends_on = None


SUPPORTED_CHECK_TYPES = {
    "http",
    "dns",
    "ping",
    "ssh",
    "ldap",
    "sql",
    "ftp",
    "winrm",
}


def upgrade():
    bind = op.get_bind()
    host_groups = sa.table(
        "host_groups",
        sa.column("id", sa.Integer()),
        sa.column("check_types", sa.JSON()),
    )
    services = sa.table(
        "services",
        sa.column("host_group_id", sa.Integer()),
        sa.column("check_type", sa.String()),
        sa.column("enabled", sa.Boolean()),
    )
    group_ids = bind.execute(sa.select(host_groups.c.id)).scalars().all()
    for group_id in group_ids:
        check_types = bind.execute(
            sa.select(services.c.check_type)
            .where(
                services.c.host_group_id == group_id,
                services.c.enabled.is_(True),
            )
            .distinct()
        ).scalars()
        configured_types = sorted(
            check_type
            for check_type in check_types
            if check_type in SUPPORTED_CHECK_TYPES
        )
        if configured_types:
            bind.execute(
                sa.update(host_groups)
                .where(host_groups.c.id == group_id)
                .values(check_types=configured_types)
            )


def downgrade():
    pass
