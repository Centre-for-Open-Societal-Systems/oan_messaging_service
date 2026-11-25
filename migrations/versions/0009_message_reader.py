"""Staff console: callers that may list messages (read-only).

One additive column, ``callers.can_list_messages`` (default false), the same shape as
``can_read_stats`` (0005). A reader is created with no channels and no purposes, so it
can never send. Adding a column with a constant default rewrites no rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "callers",
        sa.Column(
            "can_list_messages", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("callers", "can_list_messages")
