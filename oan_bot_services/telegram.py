"""The Telegram webhook receiver, reduced runtime only.

``POST /telegram/webhook``: the secret header is checked by the API layer before the body
is read. Here the update is reduced to the few fields the conversation needs and stored,
sealed, **before** the 200 answer:

* only ``message`` updates from **private** chats are kept; everything else (groups,
  channels, edited messages, button callbacks, member changes) is answered 200 and
  dropped without storing anything;
* kept: the chat id, the sender id, the text (if any) and a shared contact's user id and
  phone; nothing else (no names, usernames, dates, photos);
* a repeated ``update_id`` changes nothing: ``seen_updates`` keeps only the id for 7 days.

The worker (``conversation.py``) handles stored updates in order per chat and deletes each
one when handled. Nothing here is logged except ``contact_ref`` and fixed words.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oan_bot_services import logs
from oan_bot_services.deps import Deps
from oan_bot_services.schema import seen_updates, telegram_updates

log = logs.get(__name__)

SEAL_PURPOSE = "telegram_update"
MAX_TEXT = 4096
Outcome = Literal["stored", "repeat", "ignored", "invalid"]


def minimal(update: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    """(update_id, the minimal fields) for a private-chat message, else None."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or chat.get("type") != "private" or not isinstance(sender, dict):
        return None
    chat_id, from_id = chat.get("id"), sender.get("id")
    if not isinstance(chat_id, int) or not isinstance(from_id, int) or sender.get("is_bot"):
        return None
    out: dict[str, Any] = {"chat_id": chat_id, "from_id": from_id}
    text = message.get("text")
    if isinstance(text, str):
        out["text"] = text[:MAX_TEXT]
    contact = message.get("contact")
    if isinstance(contact, dict) and isinstance(contact.get("phone_number"), str):
        user_id = contact.get("user_id")
        out["contact"] = {
            "user_id": user_id if isinstance(user_id, int) else None,
            "phone_number": contact["phone_number"][:32],
        }
    return int(update["update_id"]), out


async def receive(deps: Deps, update: Any) -> Outcome:
    if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
        return "invalid"
    found = minimal(update)
    if found is None:
        return "ignored"
    update_id, fields = found
    ref = deps.keys.contact_ref(fields["chat_id"])
    now = await deps.clock()
    sealed = deps.keys.seal(json.dumps(fields, separators=(",", ":")), SEAL_PURPOSE)
    async with deps.sf() as s, s.begin():
        new = (
            await s.execute(
                pg_insert(seen_updates).values(update_id=update_id, seen_at=now)
                .on_conflict_do_nothing(index_elements=["update_id"])
                .returning(seen_updates.c.update_id)
            )
        ).first()
        if new is None:
            return "repeat"
        await s.execute(
            insert(telegram_updates).values(
                update_id=update_id, contact_ref=ref, body=sealed, attempts=0, received_at=now
            )
        )
    log.info("update stored contact_ref=%s", ref)
    return "stored"


def open_body(deps: Deps, sealed: bytes) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(deps.keys.open(sealed, SEAL_PURPOSE))
    return body
