"""Bot Services as an OAN Messaging Telegram caller (``oan-messaging-caller.md``).

``POST /api/v1/messages/telegram`` with ``Idempotency-Key`` (stored with the recipient
before the call, so a retry reuses it) and ``GET /api/v1/messages/{id}``. The chat id is
sent here and nowhere else; it is never logged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from oan_bot_services.clients.http import ServiceClient, Unavailable, json_object

Purpose = Literal["alert", "broadcast"]


@dataclass(frozen=True)
class Submitted:
    message_id: uuid.UUID
    status: str


@dataclass(frozen=True)
class Rejected:
    """OAN Messaging refused the request (4xx); not retried."""

    status_code: int
    code: str


@dataclass(frozen=True)
class MessageState:
    status: str
    failure_code: str | None


class OanMessaging:
    def __init__(self, http: ServiceClient) -> None:
        self.http = http

    async def submit(
        self,
        *,
        idempotency_key: uuid.UUID,
        chat_id: int,
        text: str,
        purpose: Purpose,
        expires_at: datetime,
        source_reference: str,
    ) -> Submitted | Rejected:
        r = await self.http.request(
            "POST", "/api/v1/messages/telegram",
            headers={"Idempotency-Key": str(idempotency_key)},
            json={
                "chat_id": chat_id,
                "text": text,
                "purpose": purpose,
                "expires_at": expires_at.isoformat(),
                "source_reference": source_reference,
            },
            ok=(200, 202),
        )
        if r.status_code in (200, 202):
            body = json_object(r, "oan_messaging")
            return Submitted(uuid.UUID(str(body["id"])), str(body.get("status", "accepted")))
        if r.status_code in (401, 403):
            raise Unavailable("oan_messaging", f"http_{r.status_code}")
        code = "rejected"
        try:
            code = str(r.json().get("code") or code)
        except ValueError:
            pass
        return Rejected(r.status_code, code)

    async def message(self, message_id: uuid.UUID) -> MessageState | None:
        r = await self.http.request("GET", f"/api/v1/messages/{message_id}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise Unavailable("oan_messaging", f"http_{r.status_code}")
        body = json_object(r, "oan_messaging")
        return MessageState(str(body["status"]), body.get("failure_code"))
