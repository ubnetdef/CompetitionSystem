"""Require whole-dollar values for bank, store, and scoring amounts."""

from alembic import op
import sqlalchemy as sa


revision = "6c2a9e3f1b74"
down_revision = "4c7a2e9b1d6f"
branch_labels = None
depends_on = None


WHOLE_DOLLAR_COLUMNS = (
    ("competitions", "payout_per_point", "ck_competitions_payout_whole_dollars"),
    ("bank_transactions", "amount", "ck_bank_transactions_amount_whole_dollars"),
    ("products", "price", "ck_products_price_whole_dollars"),
    ("orders", "unit_price", "ck_orders_unit_price_whole_dollars"),
)


def upgrade():
    connection = op.get_bind()
    for table, column, _ in WHOLE_DOLLAR_COLUMNS:
        fractional_count = connection.execute(
            sa.text(
                f"SELECT count(*) FROM {table} "
                f"WHERE {column} != CAST({column} AS BIGINT)"
            )
        ).scalar_one()
        if fractional_count:
            raise RuntimeError(
                f"Cannot enforce whole dollars: {table}.{column} contains "
                f"{fractional_count} fractional value(s). No amounts were changed."
            )

    for table, column, constraint in WHOLE_DOLLAR_COLUMNS:
        with op.batch_alter_table(table) as batch_op:
            batch_op.create_check_constraint(
                constraint,
                f"{column} = CAST({column} AS BIGINT)",
            )


def downgrade():
    for table, _, constraint in reversed(WHOLE_DOLLAR_COLUMNS):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(constraint, type_="check")
