"""Statistics: label each campaign row with its channel (contract 3.3.0).

The Telegram copy of a DA broadcast (``bc:<broadcast_id>:<n>``) and its SMS copy show as
two labelled rows. ``channel`` joins the primary key, so one caller's campaign on two
channels gives two rows.

Expand-only for the running version: the new column has a constant default (``sms``), so
the table is not rewritten and an older refresh still inserts. Rows written before this
migration are labelled ``telegram`` only when their caller cannot send SMS; the next
refresh rebuilds the recent days anyway.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stats_campaigns",
        sa.Column("channel", sa.Text(), nullable=False, server_default=sa.text("'sms'")),
    )
    op.execute(
        "UPDATE stats_campaigns s SET channel = 'telegram' FROM callers c"
        " WHERE c.caller_id = s.caller_id AND NOT ('sms' = ANY (c.allowed_channels))"
        " AND 'telegram' = ANY (c.allowed_channels)"
    )
    op.drop_constraint("pk_stats_campaigns", "stats_campaigns", type_="primary")
    op.create_primary_key(
        "pk_stats_campaigns", "stats_campaigns", ["caller_id", "campaign", "purpose", "channel"]
    )


def downgrade() -> None:
    op.execute("DELETE FROM stats_campaigns WHERE channel <> 'sms'")
    op.drop_constraint("pk_stats_campaigns", "stats_campaigns", type_="primary")
    op.create_primary_key(
        "pk_stats_campaigns", "stats_campaigns", ["caller_id", "campaign", "purpose"]
    )
    op.drop_column("stats_campaigns", "channel")
