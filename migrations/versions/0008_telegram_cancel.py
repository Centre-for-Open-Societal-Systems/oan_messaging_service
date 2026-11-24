"""Contract 3.4.0: failure code ``cancelled`` and the cancel transition (Telegram only).

Two changes, both on ``messages`` and both safe while the service runs:

1. The status trigger function is replaced (``CREATE OR REPLACE FUNCTION``). It keeps
   every transition of 0001 and adds one more: ``accepted -> failed`` only when the new
   failure code is ``cancelled`` and the channel is ``telegram``. Replacing a function
   takes no lock on the table; the next row update uses the new body.
2. The ``failure_code`` check gains ``cancelled``. The new check is added ``NOT VALID``
   (a short exclusive lock, no table scan; new and updated rows are checked at once),
   the old one is dropped in the same short step, and ``VALIDATE CONSTRAINT`` then checks
   the existing rows under a SHARE UPDATE EXCLUSIVE lock, which does not block reads or
   writes. ``lock_timeout`` makes the migration fail fast instead of queueing traffic
   behind a long transaction; it is safe to rerun.

No column is added and no row is rewritten. Reply buttons (``reply_markup``) need no
schema change: they are stored inside the encrypted message content and purged with it.

Downgrade refuses while cancelled messages exist: the old check would reject any later
update of those rows (for example the binding release of the purge sweep).
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Legal (from, to) status pairs; must equal contracts.ALLOWED_TRANSITIONS (as in 0001).
TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("accepted", "sending"), ("accepted", "expired"),
    ("sending", "sent"), ("sending", "failed"), ("sending", "unknown"),
    ("sending", "accepted"), ("sending", "expired"),
    ("sent", "delivered"), ("sent", "failed"),
    ("unknown", "sent"), ("unknown", "delivered"), ("unknown", "failed"),
)
#: contracts.CANCEL_TRANSITION, allowed only for a Telegram message with failure code
#: ``cancelled``.
CANCEL = ("accepted", "failed")


def _function(cancel: bool) -> str:
    pairs = ", ".join(f"('{a}', '{b}')" for a, b in TRANSITIONS)
    extra = (
        f"  IF OLD.status = '{CANCEL[0]}' AND NEW.status = '{CANCEL[1]}'"
        " AND NEW.failure_code = 'cancelled' AND NEW.channel = 'telegram'"
        " THEN RETURN NEW; END IF;\n"
        if cancel
        else ""
    )
    return (
        "CREATE OR REPLACE FUNCTION oan_msg_check_status_transition() RETURNS trigger AS $$\n"
        "BEGIN\n"
        "  IF NEW.status = OLD.status THEN RETURN NEW; END IF;\n"
        f"  IF (OLD.status, NEW.status) IN ({pairs}) THEN RETURN NEW; END IF;\n"
        f"{extra}"
        "  RAISE EXCEPTION 'illegal message status transition' USING ERRCODE = 'check_violation';\n"
        "END\n"
        "$$ LANGUAGE plpgsql"
    )


#: contracts.FailureCode before and after 3.4.0.
CODES_0001 = (
    "chat_not_found", "expired_before_send", "interrupted", "invalid_recipient",
    "malformed_response", "provider_auth", "provider_config", "provider_delivery_expired",
    "provider_rejected", "provider_server_error", "provider_unavailable", "rate_limited",
    "recipient_blocked_bot", "text_rejected", "timeout",
)
CODES_0008 = tuple(sorted((*CODES_0001, "cancelled")))


def _check(codes: Sequence[str]) -> str:
    return f"failure_code IS NULL OR failure_code IN ({', '.join(repr(c) for c in codes)})"


def _swap_check(codes: Sequence[str]) -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE messages DROP CONSTRAINT ck_messages_failure_code")
    op.execute(
        f"ALTER TABLE messages ADD CONSTRAINT ck_messages_failure_code CHECK ({_check(codes)})"
        " NOT VALID"
    )
    # Commit the short exclusive step first, so the scan below holds only the weak lock.
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE messages VALIDATE CONSTRAINT ck_messages_failure_code")


def upgrade() -> None:
    op.execute(_function(cancel=True))
    _swap_check(CODES_0008)


def downgrade() -> None:
    found = op.get_bind().execute(
        text("SELECT count(*) FROM messages WHERE failure_code = 'cancelled'")
    ).scalar_one()
    if found:
        raise RuntimeError("cancelled messages exist; downgrade below 0008 refused")
    op.execute(_function(cancel=False))
    _swap_check(CODES_0001)
