"""Bot Services tables (its own PostgreSQL database; migrations in ``migrations/``).

What is stored where (§2.4, §6.6, §6.8.1):

* ``contacts``: RapidPro contact UUID, ``contact_ref``, chosen kebele and language. No chat id.
* ``farmer_links`` / ``subscriptions``: only for a linked farmer. The subscription row holds
  the chat id (TG-33), needed to send the broadcast copy; never logged.
* ``broadcast_recipients.chat_id`` is cleared once the message is submitted.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

TZ = DateTime(timezone=True)

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

#: One answer per flow ``request_id`` (§5.8): a repeat returns it and changes nothing.
request_replays = Table(
    "request_replays",
    metadata,
    Column("request_id", UUID(as_uuid=True), primary_key=True),
    Column("route", Text, nullable=False),
    Column("contact_ref", Text, nullable=False),
    Column("status_code", Integer, nullable=False),
    Column("response", JSONB, nullable=False),
    Column("created_at", TZ, nullable=False),
)
Index("ix_request_replays_created_at", request_replays.c.created_at)

contacts = Table(
    "contacts",
    metadata,
    Column("contact_ref", Text, primary_key=True),
    Column("contact_uuid", UUID(as_uuid=True), nullable=False, unique=True),
    Column("language", Text, nullable=True),
    Column("kebele_code", Text, nullable=True),
    Column("woreda_code", Text, nullable=True),
    Column("zone_code", Text, nullable=True),
    Column("region_code", Text, nullable=True),
    Column("updated_at", TZ, nullable=False),
)

farmer_links = Table(
    "farmer_links",
    metadata,
    Column("contact_ref", Text, primary_key=True),
    Column("contact_uuid", UUID(as_uuid=True), nullable=False),
    Column("farmer_id", Text, nullable=False, unique=True),
    #: The Telegram account's number, sealed (alternate number rule, §6.6). Never logged.
    Column("linked_phone", LargeBinary, nullable=True),
    Column("registered_phone_fp", Text, nullable=False),
    Column("same_number", Boolean, nullable=False),
    Column("kebele_code", Text, nullable=False),
    Column("woreda_code", Text, nullable=False),
    Column("zone_code", Text, nullable=False),
    Column("region_code", Text, nullable=False),
    #: RapidPro contact field ``linked`` set (retried by the worker when RapidPro is down).
    Column("rapidpro_synced", Boolean, nullable=False, server_default=text("false")),
    Column("linked_at", TZ, nullable=False),
)

subscriptions = Table(
    "subscriptions",
    metadata,
    Column("contact_ref", Text, primary_key=True),
    Column("contact_uuid", UUID(as_uuid=True), nullable=False),
    Column("farmer_id", Text, nullable=False, unique=True),
    Column("kebele_code", Text, nullable=False),
    Column("woreda_code", Text, nullable=False),
    Column("zone_code", Text, nullable=False),
    Column("region_code", Text, nullable=False),
    Column("language", Text, nullable=True),
    Column("updates_on", Boolean, nullable=False),
    #: TG-33. Never logged, never sent anywhere but OAN Messaging.
    Column("chat_id", BigInteger, nullable=False),
    #: ``active`` or ``unreachable`` (blocked or deleted, TG-09): broadcasts skip it.
    Column("state", Text, nullable=False, server_default=text("'active'")),
    Column("last_activity_at", TZ, nullable=False),
    CheckConstraint("state IN ('active', 'unreachable')", name="state"),
)
Index("ix_subscriptions_woreda_code", subscriptions.c.woreda_code)
Index("ix_subscriptions_zone_code", subscriptions.c.zone_code)

link_requests = Table(
    "link_requests",
    metadata,
    Column("link_request_id", UUID(as_uuid=True), primary_key=True),
    Column("contact_ref", Text, nullable=False),
    Column("farmer_id", Text, nullable=False),
    Column("registered_phone_fp", Text, nullable=False),
    Column("linked_phone", LargeBinary, nullable=True),
    Column("same_number", Boolean, nullable=False),
    Column("kebele_code", Text, nullable=False),
    Column("woreda_code", Text, nullable=False),
    Column("zone_code", Text, nullable=False),
    Column("region_code", Text, nullable=False),
    Column("kebele_name_en", Text, nullable=True),
    Column("kebele_name_am", Text, nullable=True),
    Column("created_at", TZ, nullable=False),
    Column("expires_at", TZ, nullable=False),
    Column("used", Boolean, nullable=False, server_default=text("false")),
)

#: Wrong codes per chat (staff and farmer linking, §6.5, §6.6).
link_failures = Table(
    "link_failures",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("scope", Text, nullable=False),
    Column("contact_ref", Text, nullable=False),
    Column("at", TZ, nullable=False),
    CheckConstraint("scope IN ('staff', 'farmer')", name="scope"),
)
Index("ix_link_failures_lookup", link_failures.c.scope, link_failures.c.contact_ref)

link_locks = Table(
    "link_locks",
    metadata,
    Column("scope", Text, nullable=False),
    Column("contact_ref", Text, nullable=False),
    Column("locked_until", TZ, nullable=False),
    PrimaryKeyConstraint("scope", "contact_ref"),
)

staff_links = Table(
    "staff_links",
    metadata,
    Column("contact_ref", Text, primary_key=True),
    Column("contact_uuid", UUID(as_uuid=True), nullable=False),
    Column("da_ref", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("group_added", Boolean, nullable=False, server_default=text("false")),
    Column("linked_at", TZ, nullable=False),
)

#: Keyed messages for the notify flow, released when the contact is idle (TG-10).
keyed_queue = Table(
    "keyed_queue",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("contact_ref", Text, nullable=False),
    Column("contact_uuid", UUID(as_uuid=True), nullable=False),
    Column("text_key", Text, nullable=False),
    Column("params", JSONB, nullable=False),
    Column("keyboard", JSONB, nullable=False),
    Column("created_at", TZ, nullable=False),
)
Index("ix_keyed_queue_contact_ref", keyed_queue.c.contact_ref, keyed_queue.c.id)

#: Stop and delete my data (§5.4): each step recorded, so a repeat finishes the rest.
stop_requests = Table(
    "stop_requests",
    metadata,
    Column("contact_ref", Text, primary_key=True),
    Column("contact_uuid", UUID(as_uuid=True), nullable=False),
    #: step -> ``done``, ``skipped:<reason>`` or absent (still to do).
    Column("steps", JSONB, nullable=False),
    Column("state", Text, nullable=False),
    Column("requested_at", TZ, nullable=False),
    Column("finished_at", TZ, nullable=True),
    CheckConstraint("state IN ('pending', 'done')", name="state"),
)

broadcast_bindings = Table(
    "broadcast_bindings",
    metadata,
    #: Keyed HMAC of the Idempotency-Key (§6.8.2).
    Column("key_fp", Text, primary_key=True),
    Column("body_fp", Text, nullable=False),
    Column("broadcast_id", UUID(as_uuid=True), nullable=False),
    Column("created_at", TZ, nullable=False),
)

broadcasts = Table(
    "broadcasts",
    metadata,
    Column("broadcast_id", UUID(as_uuid=True), primary_key=True),
    Column("recipients_kind", Text, nullable=False),
    Column("scope_level", Text, nullable=False),
    Column("scope_code", Text, nullable=False),
    Column("sender_ref", Text, nullable=False),
    Column("sender_role", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("source_reference", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    #: title + blank line + body, sealed; cleared when every recipient is submitted.
    Column("text", LargeBinary, nullable=True),
    Column("expires_at", TZ, nullable=False),
    Column("recipients", Integer, nullable=False),
    Column("not_on_telegram", JSONB, nullable=False),
    Column("state", Text, nullable=False),
    Column("created_at", TZ, nullable=False),
    Column("finished_at", TZ, nullable=True),
    Column("next_poll_at", TZ, nullable=True),
    CheckConstraint("state IN ('running', 'finished')", name="state"),
    CheckConstraint("recipients_kind IN ('farmers', 'das')", name="kind"),
)

broadcast_recipients = Table(
    "broadcast_recipients",
    metadata,
    Column("broadcast_id", UUID(as_uuid=True), ForeignKey("broadcasts.broadcast_id"),
           nullable=False),
    #: The recipient's number in the broadcast (``bc:<id>:<n>``), in round-robin order.
    Column("n", Integer, nullable=False),
    Column("woreda_code", Text, nullable=False),
    Column("contact_ref", Text, nullable=True),
    Column("farmer_id", Text, nullable=True),
    Column("da_ref", Text, nullable=True),
    #: Cleared once OAN Messaging has the message.
    Column("chat_id", BigInteger, nullable=True),
    #: Stored before the call, so a retry reuses it.
    Column("idempotency_key", UUID(as_uuid=True), nullable=False),
    Column("oan_message_id", UUID(as_uuid=True), nullable=True),
    #: Bot Services' view, with OAN Messaging's names: pending (not yet submitted),
    #: accepted, sending, sent, failed, expired, unknown.
    Column("status", Text, nullable=False),
    Column("failure_code", Text, nullable=True),
    #: TG-09 handled: subscription marked unreachable (farmer) or R3 called (staff).
    Column("unreachable_done", Boolean, nullable=False, server_default=text("false")),
    Column("updated_at", TZ, nullable=False),
    PrimaryKeyConstraint("broadcast_id", "n"),
    UniqueConstraint("idempotency_key"),
)
Index("ix_broadcast_recipients_status", broadcast_recipients.c.broadcast_id,
      broadcast_recipients.c.status)

#: Area lookups (§5.6: cached 24 hours; on failure the last copy is served).
area_cache = Table(
    "area_cache",
    metadata,
    Column("area_code", Text, primary_key=True),
    Column("body", JSONB, nullable=False),
    Column("fetched_at", TZ, nullable=False),
)
