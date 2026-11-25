"""Read-only message listing for the staff console (GET /api/v1/messages).

Metadata only: status, masked recipient, who sent it, the callbacks (provider delivery
receipts) matched to it and the provider attempts made for it. Message text, the full
recipient, fingerprints and provider response bodies are never selected.

Pages are ordered newest first and walked with a keyset cursor over
``(created_at, id)``, so a page costs the same however deep it is and rows added meanwhile
never shift it.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oan_messaging.contracts import (
    CHANNEL_DELIVERY_RECEIPTS,
    FINAL_STATUSES,
    AttemptView,
    Channel,
    MessageListItem,
    MessageListPage,
    MessageStatus,
    ReceiptView,
)
from oan_messaging.storage.schema import message_attempts, messages, receipts

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


class ListQueryError(ValueError):
    """A filter or cursor the caller sent is not valid (HTTP 422)."""

    def __init__(self, field: str, code: str) -> None:
        super().__init__(f"{field}: {code}")
        self.field = field
        self.code = code


def encode_cursor(created_at: datetime, message_id: uuid.UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(message_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        created, mid = json.loads(raw)
        at = datetime.fromisoformat(created)
        if at.tzinfo is None:
            raise ValueError("naive")
        return at, uuid.UUID(mid)
    except (ValueError, TypeError, binascii.Error) as exc:  # JSONDecodeError is a ValueError
        raise ListQueryError("cursor", "invalid") from exc


def parse_limit(value: str | None) -> int:
    if value is None or value == "":
        return DEFAULT_LIMIT
    try:
        limit = int(value)
    except ValueError as exc:
        raise ListQueryError("limit", "invalid") from exc
    if not 1 <= limit <= MAX_LIMIT:
        raise ListQueryError("limit", "out_of_range")
    return limit


def _enum(params: Mapping[str, str], name: str, allowed: set[str]) -> str | None:
    value = params.get(name, "").strip()
    if not value:
        return None
    if value not in allowed:
        raise ListQueryError(name, "invalid")
    return value


def _like_prefix(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class MessageListing:
    def __init__(self, sf: async_sessionmaker[AsyncSession]) -> None:
        self._sf = sf

    async def page(self, params: Mapping[str, str]) -> MessageListPage:
        limit = parse_limit(params.get("limit"))
        channel = _enum(params, "channel", {c.value for c in Channel})
        status = _enum(params, "status", {s.value for s in MessageStatus})
        caller = params.get("caller", "").strip()
        purpose = params.get("purpose", "").strip()
        source = params.get("source_reference", "").strip()
        if len(caller) > 64 or len(purpose) > 32 or len(source) > 256:
            raise ListQueryError("filter", "too_long")
        cursor = params.get("cursor", "").strip()

        where: list[Any] = []
        if channel:
            where.append(messages.c.channel == channel)
        if status:
            where.append(messages.c.status == status)
        if caller:
            where.append(messages.c.caller_id == caller)
        if purpose:
            where.append(messages.c.purpose == purpose)
        if source:
            where.append(messages.c.source_reference.like(_like_prefix(source), escape="\\"))
        if cursor:
            at, mid = decode_cursor(cursor)
            where.append(
                or_(
                    messages.c.created_at < at,
                    and_(messages.c.created_at == at, messages.c.id < mid),
                )
            )

        m = messages.c
        cols = [
            m.id,
            m.caller_id,
            m.channel,
            m.idempotency_key,
            m.purpose,
            m.source_reference,
            m.status,
            m.failure_code,
            m.provider_status,
            m.recipient_masked,
            m.attempts,
            m.created_at,
            m.updated_at,
            m.expires_at,
            m.sent_at,
            m.delivered_at,
        ]
        async with self._sf() as s:
            found = (
                (
                    await s.execute(
                        select(*cols)
                        .where(*where)
                        .order_by(m.created_at.desc(), m.id.desc())
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            more = len(found) > limit
            rows = found[:limit]
            ids = [r["id"] for r in rows]
            callbacks: dict[uuid.UUID, list[ReceiptView]] = {}
            attempts: dict[uuid.UUID, list[AttemptView]] = {}
            if ids:
                rc = receipts.c
                for r in (
                    await s.execute(
                        select(
                            rc.id,
                            rc.message_id,
                            rc.provider,
                            rc.raw_status,
                            rc.disposition,
                            rc.event_time,
                            rc.received_at,
                        )
                        .where(rc.message_id.in_(ids))
                        .order_by(rc.received_at, rc.id)
                    )
                ).mappings():
                    callbacks.setdefault(r["message_id"], []).append(
                        ReceiptView(
                            id=r["id"],
                            provider=r["provider"],
                            raw_status=r["raw_status"],
                            disposition=r["disposition"],
                            event_time=r["event_time"],
                            received_at=r["received_at"],
                        )
                    )
                ac = message_attempts.c
                for r in (
                    await s.execute(
                        select(
                            ac.message_id,
                            ac.attempt_no,
                            ac.started_at,
                            ac.finished_at,
                            ac.outcome,
                            ac.provider_status,
                            ac.failure_code,
                            ac.units,
                        )
                        .where(ac.message_id.in_(ids))
                        .order_by(ac.message_id, ac.attempt_no)
                    )
                ).mappings():
                    attempts.setdefault(r["message_id"], []).append(
                        AttemptView(
                            attempt_no=r["attempt_no"],
                            started_at=r["started_at"],
                            finished_at=r["finished_at"],
                            outcome=r["outcome"],
                            provider_status=r["provider_status"],
                            failure_code=r["failure_code"],
                            units=r["units"],
                        )
                    )

        items = []
        for r in rows:
            ch = Channel(r["channel"])
            st = MessageStatus(r["status"])
            items.append(
                MessageListItem(
                    id=r["id"],
                    caller_id=r["caller_id"],
                    channel=ch,
                    idempotency_key=r["idempotency_key"],
                    purpose=r["purpose"],
                    source_reference=r["source_reference"],
                    status=st,
                    final=st in FINAL_STATUSES,
                    failure_code=r["failure_code"],
                    provider_status=r["provider_status"],
                    recipient_masked=r["recipient_masked"],
                    delivery_receipts_supported=CHANNEL_DELIVERY_RECEIPTS[ch],
                    attempts=r["attempts"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                    expires_at=r["expires_at"],
                    sent_at=r["sent_at"],
                    delivered_at=r["delivered_at"],
                    callbacks=callbacks.get(r["id"], []),
                    attempt_log=attempts.get(r["id"], []),
                )
            )
        nxt = encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if more and rows else None
        return MessageListPage(items=items, next_cursor=nxt)
