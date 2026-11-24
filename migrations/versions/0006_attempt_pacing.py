"""Pace provider calls per SMS part: record each attempt's units and pace ledger.

``units`` is how many SMS parts the attempt sent (Telegram: 1). ``pace_total_until`` and
``pace_bulk_until`` are the pace debt a batch leaves (see worker/rate_limit.py): nothing
starts before the first, and no non-OTP message before the second.

Expand-only: three new columns on message_attempts, a constant default and two nullable
columns, so the table is not rewritten and no index is built. Rows written before this
migration have no ledger values and are read as "no pace debt".
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "message_attempts",
        sa.Column("units", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column("message_attempts", sa.Column("pace_total_until", TZ, nullable=True))
    op.add_column("message_attempts", sa.Column("pace_bulk_until", TZ, nullable=True))
    op.create_check_constraint(
        op.f("ck_message_attempts_units_positive"), "message_attempts", "units >= 1"
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_message_attempts_units_positive"), "message_attempts", type_="check"
    )
    op.drop_column("message_attempts", "pace_bulk_until")
    op.drop_column("message_attempts", "pace_total_until")
    op.drop_column("message_attempts", "units")
