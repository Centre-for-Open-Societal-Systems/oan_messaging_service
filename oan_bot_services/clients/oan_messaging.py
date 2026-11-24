"""Bot Services as an OAN Messaging Telegram caller (``oan-messaging-caller.md``).

``POST /api/v1/messages/telegram`` with ``Idempotency-Key`` (stored with the recipient
before the call, so a retry reuses it) and ``GET /api/v1/messages/{id}``. The chat id is
sent here and nowhere else; it is never logged.

Contract 3.4.0: optional ``reply_markup`` (bot replies with buttons) and
``POST /api/v1/messages/{id}/cancel`` for a copy still waiting.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from oan_bot_services.clients.http import ServiceClient, Unavailable, json_object

Purpose = Literal["alert", "broadcast", "transactional"]
#: Answer of the cancel route: cancelled (now or before), not cancellable (claimed or
#: final: it may still arrive), or not found (not ours / no such message).
CancelResult = Literal["cancelled", "not_cancellable", "not_found"]


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
        reply_markup: dict[str, Any] | None = None,
    ) -> Submitted | Rejected:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "purpose": purpose,
            "expires_at": expires_at.isoformat(),
            "source_reference": source_reference,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        r = await self.http.request(
            "POST", "/api/v1/messages/telegram",
            headers={"Idempotency-Key": str(idempotency_key)},
            json=body,
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

    async def cancel(self, message_id: uuid.UUID) -> CancelResult:
        """Contract 3.4.0 cancel route. A repeat of a cancelled message answers 200."""
        r = await self.http.request("POST", f"/api/v1/messages/{message_id}/cancel")
        if r.status_code == 200:
            return "cancelled"
        if r.status_code == 409:
            return "not_cancellable"
        if r.status_code == 404:
            return "not_found"
        if r.status_code in (401, 403) or r.status_code >= 500:
            raise Unavailable("oan_messaging", f"http_{r.status_code}")
        return "not_cancellable"  # any other refusal (e.g. 422): leave it to polling
