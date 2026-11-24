"""The bot's short conversation without RapidPro, run by the worker.

Each stored update is handled in **one database transaction**: the conversation step, any
link or subscription change, the replies (``outbox`` rows) and the deletion of the update
commit together. A crash before the commit leaves the update stored, so it is handled
again from the start; nothing is half done and no reply is queued twice. Updates of one
chat are handled in ``update_id`` order; if one fails, that chat's later updates wait.

What the bot does (all replies are text keys from ``text-keys.yaml``, sent through OAN
Messaging with purpose ``transactional``):

* ``/start``: language choice (English, Amharic), then the privacy notice with the main
  menu: Link my Farmer ID, I am OAN staff, Updates on/off, Stop.
* Farmer linking (§6.6): Farmer ID, then the "Share my phone number" button (the shared
  contact must be the sender's own), then the code Auth sent by SMS to the **registered**
  phone. Bot Services never sends SMS and never uses the shared phone as a destination.
* Staff linking (§6.5): the 8-character code from the DA app, checked through R2.
* Updates on/off. Stop: a farmer's updates off (deleted after the retention period
  unless turned on again); staff: work messages stay on, the supervisor or the DA
  app can unlink.
* Anything else: a fixed reply that messages here are not read or answered.

A step waits 30 minutes (configurable). No inbound text is kept after the update is
handled; codes are never stored or logged.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oan_bot_services import broadcasts, linking, logs, telegram, texts
from oan_bot_services import text_keys as tk
from oan_bot_services.clients.http import Unavailable
from oan_bot_services.clients.oan_messaging import Rejected
from oan_bot_services.deps import Deps
from oan_bot_services.schema import (
    contacts,
    conversations,
    outbox,
    staff_links,
    subscriptions,
    telegram_updates,
)

log = logs.get(__name__)

#: An update that fails this many times (a bug, not an outage) is dropped.
MAX_ATTEMPTS = 5
FARMER_ID = re.compile(r"^[A-Za-z0-9-]{4,32}$")
OTP_CODE = re.compile(r"^[0-9]{4,8}$")
STAFF_CODE = re.compile(r"^[A-Za-z0-9]{8}$")
#: Namespace for link request ids derived from an update (a redo asks Auth again with
#: the same id, and Auth answers ``sent`` once).
LINK_NS = uuid.UUID("6f1d8c3e-4b2a-4e0f-9c71-0a3b5d7e9f11")
CONVERSATION_SEAL = "conversation"

# Steps
LANGUAGE = "language"
FARMER_ID_STEP = "farmer_id"
CONTACT_STEP = "contact"
CODE_STEP = "code"
STAFF_STEP = "staff_code"

Keyboard = dict[str, Any]


class Turn:
    """One update being handled: the chat, its language and the replies to queue."""

    def __init__(self, deps: Deps, s: AsyncSession, ref: str, chat_id: int, now: datetime,
                 language: str | None) -> None:
        self.deps, self.s, self.ref, self.chat_id, self.now = deps, s, ref, chat_id, now
        self.language = language if language in texts.LANGUAGES else texts.DEFAULT_LANGUAGE
        self.cat = texts.load(deps.settings.text_keys_file)

    def labels(self, key: str) -> set[str]:
        return self.cat.labels(key)

    def keyboard(self, *rows: list[str], contact_button: str | None = None) -> Keyboard:
        out: list[list[dict[str, Any]]] = []
        for row in rows:
            buttons: list[dict[str, Any]] = []
            for key in row:
                button: dict[str, Any] = {"text": self.cat.render(key, self.language)}
                if key == contact_button:
                    button["request_contact"] = True
                buttons.append(button)
            out.append(buttons)
        return {"keyboard": out, "one_time": True, "resize": True}

    def main_menu(self) -> Keyboard:
        return self.keyboard([tk.BTN_LINK_FARMER_ID, tk.BTN_STAFF],
                             [tk.BTN_UPDATES_TOGGLE, tk.BTN_STOP_UPDATES])

    def share_contact(self) -> Keyboard:
        return self.keyboard([tk.BTN_SHARE_PHONE], [tk.BTN_MAIN_MENU],
                             contact_button=tk.BTN_SHARE_PHONE)

    def back(self) -> Keyboard:
        return self.keyboard([tk.BTN_MAIN_MENU])

    async def reply(self, key: str, params: dict[str, Any] | None = None,
                    markup: Keyboard | None = None) -> None:
        self.cat.render(key, self.language, params)  # unknown key or wrong params: a bug
        await self.s.execute(
            insert(outbox).values(
                contact_ref=self.ref, chat_id=self.chat_id, language=self.language,
                text_key=key, params=params or {}, reply_markup=markup,
                idempotency_key=uuid.uuid4(), created_at=self.now,
                expires_at=self.now + timedelta(minutes=self.deps.settings.reply_minutes),
            )
        )

    async def set_step(self, step: str, data: dict[str, Any] | None = None) -> None:
        sealed = (
            self.deps.keys.seal(json.dumps(data), CONVERSATION_SEAL) if data is not None else None
        )
        expires = self.now + timedelta(minutes=self.deps.settings.conversation_minutes)
        stmt = pg_insert(conversations).values(
            contact_ref=self.ref, step=step, data=sealed, expires_at=expires
        )
        await self.s.execute(
            stmt.on_conflict_do_update(
                index_elements=["contact_ref"],
                set_={"step": step, "data": sealed, "expires_at": expires},
            )
        )

    async def clear_step(self) -> None:
        await self.s.execute(delete(conversations).where(conversations.c.contact_ref == self.ref))


# ------------------------------------------------------------------- the worker


async def process_updates(deps: Deps) -> int:
    """Handle stored updates, oldest first, in order per chat. Returns how many."""
    async with deps.sf() as s:
        rows = (
            await s.execute(
                select(telegram_updates.c.update_id, telegram_updates.c.contact_ref)
                .order_by(telegram_updates.c.update_id)
                .limit(deps.settings.batch_size)
            )
        ).all()
    waiting: set[str] = set()
    handled = 0
    for update_id, ref in rows:
        if ref in waiting:
            continue
        try:
            await _handle_one(deps, update_id, ref)
            handled += 1
        except Exception as exc:  # a bug or a database error: retried, then dropped
            waiting.add(ref)
            await _failed(deps, update_id, ref, exc)
    return handled


async def _handle_one(deps: Deps, update_id: int, ref: str) -> None:
    async with deps.sf() as s, s.begin():
        row = (
            await s.execute(
                select(telegram_updates.c.body).where(telegram_updates.c.update_id == update_id)
                .with_for_update()
            )
        ).first()
        if row is None:
            return
        body = telegram.open_body(deps, row.body)
        await handle(deps, s, ref, update_id, body)
        await s.execute(delete(telegram_updates).where(telegram_updates.c.update_id == update_id))


async def _failed(deps: Deps, update_id: int, ref: str, exc: Exception) -> None:
    async with deps.sf() as s, s.begin():
        attempts = (
            await s.execute(
                update(telegram_updates).where(telegram_updates.c.update_id == update_id)
                .values(attempts=telegram_updates.c.attempts + 1)
                .returning(telegram_updates.c.attempts)
            )
        ).scalar_one_or_none()
        if attempts is not None and attempts >= MAX_ATTEMPTS:
            await s.execute(
                delete(telegram_updates).where(telegram_updates.c.update_id == update_id)
            )
            log.error("update dropped contact_ref=%s error_type=%s", ref, type(exc).__name__)
            return
    log.error("update failed contact_ref=%s error_type=%s", ref, type(exc).__name__)


# --------------------------------------------------------------- one message


async def handle(deps: Deps, s: AsyncSession, ref: str, update_id: int,
                 msg: dict[str, Any]) -> None:
    now = await deps.clock()
    chat_id = int(msg["chat_id"])
    contact = (
        await s.execute(select(contacts.c.language).where(contacts.c.contact_ref == ref))
    ).first()
    t = Turn(deps, s, ref, chat_id, now, contact.language if contact else None)
    conv = (
        await s.execute(select(conversations).where(conversations.c.contact_ref == ref))
    ).mappings().first()
    expired = conv is not None and conv["expires_at"] <= now
    if expired:
        await t.clear_step()
        conv = None
    await _seen_alive(s, ref, now)

    text = (msg.get("text") or "").strip()
    command = text.split()[0].lower().split("@")[0] if text.startswith("/") else None

    if command == "/start":
        await _start(t)
    elif command == "/stop" or text in t.labels(tk.BTN_STOP_UPDATES):
        await _stop(t)
    elif command == "/menu" or text in t.labels(tk.BTN_MAIN_MENU):
        await t.clear_step()
        await t.reply(tk.MENU_MAIN, markup=t.main_menu())
    elif conv is not None and conv["step"] == LANGUAGE and text in (
        t.labels(tk.BTN_LANG_EN) | t.labels(tk.BTN_LANG_AM)
    ):
        await _language(t, "en" if text in t.labels(tk.BTN_LANG_EN) else "am")
    elif text in t.labels(tk.BTN_LINK_FARMER_ID):
        await t.set_step(FARMER_ID_STEP)
        await t.reply(tk.LINK_ASK_FARMER_ID, markup=t.back())
    elif text in t.labels(tk.BTN_STAFF):
        await t.set_step(STAFF_STEP)
        await t.reply(tk.STAFF_ASK_CODE, markup=t.back())
    elif text in t.labels(tk.BTN_UPDATES_TOGGLE):
        await _toggle_updates(t)
    elif conv is not None and conv["step"] == LANGUAGE:
        await t.reply(tk.ONBOARDING_CHOOSE_LANGUAGE, markup=_language_keyboard(t))
    elif conv is not None and conv["step"] in (FARMER_ID_STEP, CONTACT_STEP, CODE_STEP):
        data = (
            json.loads(deps.keys.open(conv["data"], CONVERSATION_SEAL)) if conv["data"] else {}
        )
        await _farmer_step(t, conv["step"], data, msg, text, update_id)
    elif conv is not None and conv["step"] == STAFF_STEP:
        await _staff_step(t, text)
    elif expired:
        await t.reply(tk.COMMON_SESSION_EXPIRED, markup=t.main_menu())
    else:
        await t.reply(tk.COMMON_NOT_READ, markup=t.main_menu())


async def _seen_alive(s: AsyncSession, ref: str, now: datetime) -> None:
    """A message from the chat shows it works again: an ``unreachable`` subscription
    (blocked, then unblocked) is active again. Updates on or off is not changed."""
    await s.execute(
        update(subscriptions).where(subscriptions.c.contact_ref == ref,
                                    subscriptions.c.state == "unreachable")
        .values(state="active", stopped_at=None)
    )
    await s.execute(
        update(subscriptions).where(subscriptions.c.contact_ref == ref)
        .values(last_activity_at=now)
    )


def _language_keyboard(t: Turn) -> Keyboard:
    return t.keyboard([tk.BTN_LANG_EN, tk.BTN_LANG_AM])


async def _start(t: Turn) -> None:
    stmt = pg_insert(contacts).values(contact_ref=t.ref, updated_at=t.now, stopped_at=None)
    await t.s.execute(
        stmt.on_conflict_do_update(index_elements=["contact_ref"],
                                   set_={"updated_at": t.now, "stopped_at": None})
    )
    await t.set_step(LANGUAGE)
    await t.reply(tk.ONBOARDING_CHOOSE_LANGUAGE, markup=_language_keyboard(t))


async def _language(t: Turn, language: str) -> None:
    await t.s.execute(
        update(contacts).where(contacts.c.contact_ref == t.ref)
        .values(language=language, updated_at=t.now)
    )
    await t.s.execute(
        update(subscriptions).where(subscriptions.c.contact_ref == t.ref).values(language=language)
    )
    t.language = language
    await t.clear_step()
    st = t.deps.settings
    await t.reply(
        tk.ONBOARDING_PRIVACY_NOTICE,
        {"retention_days": st.stopped_retention_days, "log_days": st.update_id_days},
        markup=t.main_menu(),
    )


async def _toggle_updates(t: Turn) -> None:
    await t.clear_step()
    sub = (
        await t.s.execute(
            select(subscriptions.c.updates_on, subscriptions.c.state)
            .where(subscriptions.c.contact_ref == t.ref)
        )
    ).first()
    if sub is None:
        await t.reply(tk.LINK_INVITE, markup=t.main_menu())
        return
    if sub.updates_on and sub.state == "active":
        await t.s.execute(
            update(subscriptions).where(subscriptions.c.contact_ref == t.ref)
            .values(updates_on=False)
        )
        await broadcasts.withdraw_copies(t.s, t.ref, t.now)
        log.info("updates set contact_ref=%s on=False", t.ref)
        await t.reply(tk.UPDATES_OFF, markup=t.main_menu())
        return
    # On within the retention period restores the subscription without linking.
    await t.s.execute(
        update(subscriptions).where(subscriptions.c.contact_ref == t.ref)
        .values(updates_on=True, state="active", stopped_at=None, last_activity_at=t.now)
    )
    await t.s.execute(
        update(contacts).where(contacts.c.contact_ref == t.ref).values(stopped_at=None)
    )
    log.info("updates set contact_ref=%s on=True", t.ref)
    await t.reply(tk.UPDATES_ON, markup=t.main_menu())


async def _stop(t: Turn) -> None:
    """Farmer: updates off now, data deleted after the retention period unless the
    farmer turns updates on again. Staff: work messages stay on."""
    await t.clear_step()
    sub = (
        await t.s.execute(
            select(subscriptions.c.state).where(subscriptions.c.contact_ref == t.ref)
        )
    ).first()
    staff = (
        await t.s.execute(select(staff_links.c.da_ref).where(staff_links.c.contact_ref == t.ref))
    ).first()
    if sub is not None:
        values: dict[str, Any] = {"updates_on": False}
        if sub.state != "stopped":
            values.update(state="stopped", stopped_at=t.now)
        await t.s.execute(
            update(subscriptions).where(subscriptions.c.contact_ref == t.ref).values(**values)
        )
        await broadcasts.withdraw_copies(t.s, t.ref, t.now)
    if sub is not None or staff is None:
        if sub is None:
            await t.s.execute(
                update(contacts).where(contacts.c.contact_ref == t.ref,
                                       contacts.c.stopped_at.is_(None))
                .values(stopped_at=t.now)
            )
        log.info("stop contact_ref=%s linked=%s", t.ref, sub is not None)
        await t.reply(tk.STOP_UPDATES_STOPPED,
                      {"retention_days": t.deps.settings.stopped_retention_days},
                      markup={"remove_keyboard": True} if staff is None else None)
    if staff is not None:
        log.info("stop staff contact_ref=%s", t.ref)
        await t.reply(tk.STOP_STAFF, markup=t.main_menu())


async def _farmer_step(t: Turn, step: str, data: dict[str, Any], msg: dict[str, Any],
                       text: str, update_id: int) -> None:
    deps, s = t.deps, t.s
    if step == FARMER_ID_STEP:
        if not FARMER_ID.match(text):
            await t.clear_step()
            await t.reply(tk.LINK_NOT_FOUND, markup=t.main_menu())
            return
        await t.set_step(CONTACT_STEP, {"farmer_id": text})
        await t.reply(tk.LINK_ASK_CONTACT, markup=t.share_contact())
        return
    if step == CONTACT_STEP:
        shared = msg.get("contact")
        if not isinstance(shared, dict):
            await t.reply(tk.LINK_ASK_CONTACT, markup=t.share_contact())
            return
        try:
            out = await linking.farmer_start(
                deps, s, t.ref, t.now, sender_id=int(msg["from_id"]),
                farmer_id=str(data["farmer_id"]),
                shared_user_id=int(shared["user_id"]) if shared.get("user_id") is not None else -1,
                shared_phone=str(shared["phone_number"]),
                link_request_id=uuid.uuid5(LINK_NS, f"{t.ref}:{update_id}"),
            )
        except Unavailable:
            await t.reply(tk.COMMON_UNAVAILABLE, markup=t.share_contact())
            return
        if out["result"] == "code_sent":
            await t.set_step(CODE_STEP, {"link_request_id": out["link_request_id"]})
            await t.reply(out["text_key"], out["params"], markup=t.back())
        elif out["result"] == "not_own_contact":
            await t.reply(out["text_key"], markup=t.share_contact())
        else:  # not_found, rate_limited, locked
            await t.clear_step()
            await t.reply(out["text_key"], markup=t.main_menu())
        return
    # CODE_STEP
    if not OTP_CODE.match(text):
        await t.reply(tk.LINK_CODE_INVALID, markup=t.back())
        return
    try:
        v = await linking.farmer_verify(
            deps, s, t.ref, t.now, chat_id=t.chat_id, contact_uuid=None,
            link_request_id=uuid.UUID(str(data["link_request_id"])), code=text,
        )
    except Unavailable:
        await t.reply(tk.COMMON_UNAVAILABLE, markup=t.back())
        return
    result = v.out["result"]
    if result == "invalid":
        await t.reply(v.out["text_key"], markup=t.back())
        return
    await t.clear_step()
    if result == "linked":
        await s.execute(
            update(contacts).where(contacts.c.contact_ref == t.ref).values(stopped_at=None)
        )
    await t.reply(v.out["text_key"], markup=t.main_menu())


async def _staff_step(t: Turn, text: str) -> None:
    if not STAFF_CODE.match(text):
        await t.reply(tk.STAFF_CODE_INVALID, markup=t.back())
        return
    try:
        out = await linking.staff(t.deps, t.s, t.ref, t.now, code=text, chat_id=t.chat_id,
                                  contact_uuid=None)
    except Unavailable:
        await t.reply(tk.COMMON_UNAVAILABLE, markup=t.back())
        return
    if out["result"] in ("invalid", "expired"):
        await t.reply(out["text_key"], markup=t.back())
        return
    await t.clear_step()
    await t.reply(out["text_key"], out.get("params"), markup=t.main_menu())


# ------------------------------------------------------------------- replies


async def send_outbox(deps: Deps) -> int:
    """Hand queued replies to OAN Messaging, oldest first, with the stored key. A row is
    deleted once OAN Messaging has it (or refused it); an outage leaves the rest."""
    now = await deps.clock()
    cat = texts.load(deps.settings.text_keys_file)
    async with deps.sf() as s:
        rows = (
            await s.execute(select(outbox).order_by(outbox.c.id).limit(deps.settings.batch_size))
        ).mappings().all()
    sent = 0
    for row in rows:
        if row["expires_at"] > now:
            try:
                out = await deps.oan.submit(
                    idempotency_key=row["idempotency_key"], chat_id=int(row["chat_id"]),
                    text=cat.render(row["text_key"], row["language"], row["params"]),
                    purpose="transactional", expires_at=row["expires_at"],
                    source_reference=f"bot:{row['contact_ref']}:{row['id']}",
                    reply_markup=row["reply_markup"],
                )
            except Unavailable:
                log.warning("replies paused")
                break
            if isinstance(out, Rejected):
                log.warning("reply refused contact_ref=%s code=%s", row["contact_ref"], out.code)
            else:
                sent += 1
        else:
            log.info("reply expired contact_ref=%s", row["contact_ref"])
        async with deps.sf() as s, s.begin():
            await s.execute(delete(outbox).where(outbox.c.id == row["id"]))
    return sent
