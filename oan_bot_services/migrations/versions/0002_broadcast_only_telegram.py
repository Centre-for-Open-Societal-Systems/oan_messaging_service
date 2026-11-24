"""Broadcast-only Telegram without RapidPro.

* RapidPro contact UUID columns nullable (the reduced runtime has no RapidPro id).
* ``contacts.subscriber_id``: an OAN-owned UUID per chat; ``contacts.stopped_at``.
* ``subscriptions``: state ``stopped`` (Stop) and ``stopped_at`` (retention).
  Existing ``unreachable`` rows start their 90 days at the upgrade.
* ``broadcast_recipients.cancel_requested``.
* New tables: ``telegram_updates``, ``seen_updates``, ``conversations``, ``outbox``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID_TABLES = (
    "contacts", "farmer_links", "subscriptions", "staff_links", "keyed_queue", "stop_requests",
)
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    for table in UUID_TABLES:
        op.alter_column(table, "contact_uuid", existing_type=sa.UUID(), nullable=True)

    op.add_column(
        "contacts",
        sa.Column("subscriber_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
    )
    op.create_unique_constraint(op.f("uq_contacts_subscriber_id"), "contacts", ["subscriber_id"])
    op.add_column("contacts", sa.Column("stopped_at", TZ, nullable=True))

    op.add_column("subscriptions", sa.Column("stopped_at", TZ, nullable=True))
    op.drop_constraint(op.f("ck_subscriptions_state"), "subscriptions", type_="check")
    op.create_check_constraint(
        op.f("ck_subscriptions_state"), "subscriptions",
        "state IN ('active', 'unreachable', 'stopped')",
    )
    op.execute("UPDATE subscriptions SET stopped_at = now() WHERE state = 'unreachable'")

    op.add_column(
        "broadcast_recipients",
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False),
    )

    op.create_table(
        "telegram_updates",
        sa.Column("update_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("contact_ref", sa.Text(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("received_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("update_id", name=op.f("pk_telegram_updates")),
    )
    op.create_index("ix_telegram_updates_contact_ref", "telegram_updates",
                    ["contact_ref", "update_id"], unique=False)
    op.create_table(
        "seen_updates",
        sa.Column("update_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("seen_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("update_id", name=op.f("pk_seen_updates")),
    )
    op.create_index("ix_seen_updates_seen_at", "seen_updates", ["seen_at"], unique=False)
    op.create_table(
        "conversations",
        sa.Column("contact_ref", sa.Text(), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=True),
        sa.Column("expires_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("contact_ref", name=op.f("pk_conversations")),
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("contact_ref", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("text_key", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reply_markup", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("idempotency_key", sa.UUID(), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("expires_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_outbox_idempotency_key")),
    )


def downgrade() -> None:
    op.drop_table("outbox")
    op.drop_table("conversations")
    op.drop_index("ix_seen_updates_seen_at", table_name="seen_updates")
    op.drop_table("seen_updates")
    op.drop_index("ix_telegram_updates_contact_ref", table_name="telegram_updates")
    op.drop_table("telegram_updates")
    op.drop_column("broadcast_recipients", "cancel_requested")
    # Rows that exist only in the reduced runtime cannot go back: a stopped subscription
    # becomes updates-off, and rows without a RapidPro contact are removed.
    op.execute("UPDATE subscriptions SET state = 'active', updates_on = false "
               "WHERE state = 'stopped'")
    op.drop_constraint(op.f("ck_subscriptions_state"), "subscriptions", type_="check")
    op.create_check_constraint(
        op.f("ck_subscriptions_state"), "subscriptions", "state IN ('active', 'unreachable')"
    )
    op.drop_column("subscriptions", "stopped_at")
    op.drop_column("contacts", "stopped_at")
    op.drop_constraint(op.f("uq_contacts_subscriber_id"), "contacts", type_="unique")
    op.drop_column("contacts", "subscriber_id")
    for table in UUID_TABLES:
        op.execute(f"DELETE FROM {table} WHERE contact_uuid IS NULL")  # noqa: S608
        op.alter_column(table, "contact_uuid", existing_type=sa.UUID(), nullable=False)
