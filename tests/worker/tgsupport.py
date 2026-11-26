"""Helpers for the contract 3.4.0 tests (Telegram broadcast switch, reply buttons, cancel).

Test data only: phones 0900000xxx, made-up chat ids, no real tokens.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from wsupport import T0

from oan_messaging.contracts import CallerContext, StoredMessage, TelegramSubmission
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.storage.schema import message_content, messages

SHARE = {"keyboard": [[{"text": "Share my phone number", "request_contact": True}]]}


async def submit_tg(
    repo: PostgresMessageRepository,
    ctx: CallerContext,
    *,
    purpose: str = "broadcast",
    at: datetime = T0,
    chat_id: int = 555000111,
    text: str = "Advisory for your kebele.",
    reply_markup: dict[str, Any] | None = None,
    key: str | None = None,
    expires_in: timedelta | None = None,
) -> StoredMessage:
    body: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "source_reference": "bc:BC-1:1" if purpose == "broadcast" else "bot:c-1:1",
        "purpose": purpose,
    }
    if reply_markup is not None:
        body["reply_markup"] = reply_markup
    if expires_in is not None:
        body["expires_at"] = (at + expires_in).isoformat()
    msg, _ = await repo.create_or_get(
        ctx, key or str(uuid.uuid4()), TelegramSubmission.model_validate(body), at
    )
    return msg


async def row(sf: async_sessionmaker[AsyncSession], mid: uuid.UUID) -> dict[str, Any]:
    async with sf() as s:
        r = (await s.execute(select(messages).where(messages.c.id == mid))).mappings().one()
    return dict(r)


async def has_content(sf: async_sessionmaker[AsyncSession], mid: uuid.UUID) -> bool:
    async with sf() as s:
        got = await s.execute(
            select(message_content.c.message_id).where(message_content.c.message_id == mid)
        )
    return got.first() is not None


async def raw_content(sf: async_sessionmaker[AsyncSession], mid: uuid.UUID) -> bytes:
    async with sf() as s:
        got = await s.execute(
            select(message_content.c.ciphertext).where(message_content.c.message_id == mid)
        )
    return bytes(got.scalar_one())
