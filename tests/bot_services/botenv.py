"""Shared Bot Services test helpers: a normal module, so tests never import ``conftest``
or another test file by name (the repository has several ``conftest.py``)."""

from __future__ import annotations

import base64
import hashlib
import itertools
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from botsupport import OAN, RAPIDPRO, STUBS, Farmer, Upstreams, rid, sensitive
from contract import check_error, check_response
from sqlalchemy import text

from oan_bot_services import conversation
from oan_bot_services import text_keys as tk
from oan_bot_services.deps import Deps
from oan_bot_services.settings import BotSettings
from oan_bot_services.texts import Catalogue, load

ROOT = Path(__file__).resolve().parents[2]
TABLES = (
    "broadcast_recipients, broadcasts, broadcast_bindings, request_replays, contacts, "
    "farmer_links, subscriptions, link_requests, link_failures, link_locks, staff_links, "
    "keyed_queue, stop_requests, area_cache, telegram_updates, seen_updates, conversations, "
    "outbox"
)
SPEC_PROBLEM_CODES = {
    "unauthenticated",
    "forbidden",
    "not_found",
    "validation_failed",
    "idempotency_conflict",
    "payload_too_large",
    "unsupported_media_type",
    "service_unavailable",
}
#: Every log line written during the session (formatted, before any handler filter).
LOG_LINES: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_LINES.append(self.format(record))
        except Exception:
            LOG_LINES.append(str(record.msg))


def _libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def b64(n: int = 32) -> str:
    return base64.b64encode(secrets.token_bytes(n)).decode()


BROADCAST_TOKEN = "da-backend-" + secrets.token_hex(16)
FLOW_SECRET = "flow-" + secrets.token_hex(16)
WEBHOOK_SECRET = "hook-" + secrets.token_hex(16)
sensitive(BROADCAST_TOKEN, FLOW_SECRET, WEBHOOK_SECRET)


def make_settings(db: str, up: Upstreams, **over: Any) -> BotSettings:
    """The RapidPro runtime (switch on), as the wave 1 tests were written."""
    values: dict[str, Any] = {
        "env": "local",
        "rapidpro_enabled": True,
        "database_url": db,
        "flow_secret": FLOW_SECRET,
        "contact_ref_key": b64(),
        "fingerprint_key": b64(),
        "data_key": b64(),
        "broadcast_token_sha256": hashlib.sha256(BROADCAST_TOKEN.encode()).hexdigest(),
        "client_id": "bot-services",
        "client_secret": "client-" + secrets.token_hex(8),
        "token_url": f"{STUBS}/auth/token",
        "da_url": f"{STUBS}/da",
        "auth_url": f"{STUBS}/auth",
        "beneficiary_url": f"{STUBS}/beneficiary",
        "master_data_url": f"{STUBS}/master-data",
        "marketplace_url": f"{STUBS}/marketplace",
        "rapidpro_url": RAPIDPRO,
        "rapidpro_token": "rp-" + secrets.token_hex(8),
        "rapidpro_notify_flow": "2d1c2c55-6b3c-4d1e-9a7b-000000000001",
        "oan_msg_url": OAN,
        "oan_msg_token": up.oan_auth,
        "db_pool_size": 2,
        "db_max_overflow": 2,
    }
    values.update(over)
    return BotSettings(**values)


def make_reduced_settings(db: str, up: Upstreams, **over: Any) -> BotSettings:
    """Broadcast-only Telegram: RapidPro off and no RapidPro setting or flow secret."""
    values: dict[str, Any] = {
        "env": "local",
        "database_url": db,
        "telegram_webhook_secret": WEBHOOK_SECRET,
        "contact_ref_key": b64(),
        "fingerprint_key": b64(),
        "data_key": b64(),
        "broadcast_token_sha256": hashlib.sha256(BROADCAST_TOKEN.encode()).hexdigest(),
        "client_id": "bot-services",
        "client_secret": "client-" + secrets.token_hex(8),
        "token_url": f"{STUBS}/auth/token",
        "da_url": f"{STUBS}/da",
        "auth_url": f"{STUBS}/auth",
        "beneficiary_url": f"{STUBS}/beneficiary",
        "master_data_url": f"{STUBS}/master-data",
        "oan_msg_url": OAN,
        "oan_msg_token": up.oan_auth,
        "db_pool_size": 2,
        "db_max_overflow": 2,
    }
    values.update(over)
    return BotSettings(**values)


@dataclass
class Bot:
    deps: Deps
    up: Upstreams
    api: httpx.AsyncClient

    @property
    def clock(self) -> Any:
        return self.deps.clock

    async def flow(
        self, method: str, path: str, body: dict[str, Any], *, secret: str | None = FLOW_SECRET
    ) -> httpx.Response:
        headers = {"X-OAN-Flow-Secret": secret} if secret is not None else {}
        r = await self.api.request(method, path, json=body, headers=headers)
        if r.status_code in (200, 422, 503):
            problems = check_response("bot", method, path, r.status_code, r.json())
            if problems and "not in the contract" in problems[0]:
                # A route that lists no error answer still answers bad input with the
                # contract's own Error shape ({error, text_key}).
                problems = check_error(r.json())
            assert not problems, problems
        return r

    async def bc(
        self,
        method: str,
        path: str,
        *,
        key: str | None = None,
        body: dict[str, Any] | None = None,
        token: str | None = BROADCAST_TOKEN,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if key is not None:
            headers["Idempotency-Key"] = key
        r = await self.api.request(method, path, json=body, headers=headers, params=params)
        problems = check_response("broadcasts", method, path, r.status_code, r.json())
        if problems and "not in the contract" in problems[0]:
            # Spec §6.8.2 error codes the OpenAPI file does not list (401, 413, 415, 503,
            # a bad cursor): still problem+json with a value-free code.
            assert r.headers["content-type"].startswith("application/problem+json")
            assert r.json()["code"] in SPEC_PROBLEM_CODES
            problems = []
        assert not problems, problems
        return r

    async def q(self, sql: str, **params: Any) -> list[Any]:
        async with self.deps.engine.connect() as conn:
            return list((await conn.execute(text(sql), params)).all())


async def link_farmer(
    bot: Bot,
    contact: dict,
    farmer_id: str = "FR-TEST-0001",
    phone: str = "+251900000101",
    kebele: str = "ET01010101",
) -> None:
    sensitive(farmer_id, phone, phone[-9:])
    bot.up.farmers[farmer_id] = Farmer(farmer_id, phone, kebele)
    r = await bot.flow(
        "POST",
        "/farmer-link/start",
        {
            "request_id": rid(),
            "contact": contact,
            "farmer_id": farmer_id,
            "shared_contact": {"user_id": contact["telegram_id"], "phone_number": phone},
        },
    )
    assert r.json()["result"] == "code_sent", r.json()
    r = await bot.flow(
        "POST",
        "/farmer-link/verify",
        {
            "request_id": rid(),
            "contact": contact,
            "link_request_id": r.json()["link_request_id"],
            "code": bot.up.otp_code,
        },
    )
    assert r.json()["result"] == "linked", r.json()


BODY = {
    "sender_ref": "COMMS-TEST-001",
    "sender_role": "communications_officer",
    "source": "Weather",
    "source_reference": "da_broadcast:BC-1",
    "title": "Heavy rain",
    "body": "Expect heavy rain on Friday. Cover the harvest.",
}


async def farmers_broadcast(
    bot: Bot, level: str, code: str, key: str | None = None, **over: Any
) -> Any:
    body = {"recipients": "farmers", "scope": {"level": level, "code": code}, **BODY, **over}
    return await bot.bc("POST", "/broadcasts", key=key or str(uuid.uuid4()), body=body)


_UPDATE_IDS = itertools.count(5_000_000)


@dataclass
class Tg(Bot):
    """The reduced runtime, driven the way Telegram drives it: updates to the webhook,
    then the worker's conversation job; replies are read from the fake OAN Messaging."""

    sent: list[int] = field(default_factory=list)

    @property
    def cat(self) -> Catalogue:
        return load(self.deps.settings.text_keys_file)

    def text(self, key: str, lang: str = "en", **params: Any) -> str:
        return self.cat.render(key, lang, params)

    def label(self, key: str, lang: str = "en") -> str:
        return self.cat.render(key, lang)

    async def send(
        self,
        chat_id: int,
        text: str | None = None,
        *,
        contact: dict[str, Any] | None = None,
        chat_type: str = "private",
        update_id: int | None = None,
        secret: str | None = WEBHOOK_SECRET,
        from_id: int | None = None,
    ) -> httpx.Response:
        # No message text may reach a log line (commands like /start are not personal).
        sensitive(chat_id, None if text is None or text.startswith("/") else text)
        if contact is not None:
            sensitive(contact.get("phone_number"))
        message: dict[str, Any] = {
            "message_id": 1,
            "date": 1764567000,
            "chat": {"id": chat_id, "type": chat_type, "first_name": "Test"},
            "from": {"id": from_id or chat_id, "is_bot": False, "first_name": "Test",
                     "language_code": "en"},
        }
        if text is not None:
            message["text"] = text
        if contact is not None:
            message["contact"] = {"first_name": "Test", **contact}
        uid = update_id if update_id is not None else next(_UPDATE_IDS)
        self.sent.append(uid)
        headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret is not None else {}
        r = await self.api.post("/telegram/webhook", json={"update_id": uid, "message": message},
                                headers=headers)
        problems = check_response("bot", "POST", "/telegram/webhook", r.status_code, r.json())
        assert not problems, problems
        return r

    async def run(self) -> None:
        await conversation.process_updates(self.deps)
        await conversation.send_outbox(self.deps)

    async def say(self, chat_id: int, text: str | None = None, **kw: Any) -> dict[str, Any]:
        """Send one message, run the worker, return the last reply to that chat."""
        before = len(self.up.replies(chat_id))
        r = await self.send(chat_id, text, **kw)
        assert r.status_code == 200, r.text
        await self.run()
        replies = self.up.replies(chat_id)
        assert len(replies) > before, "no reply"
        return replies[-1]

    def ref(self, chat_id: int) -> str:
        return self.deps.keys.contact_ref(chat_id)

    async def onboard(self, chat_id: int, lang_key: str = tk.BTN_LANG_EN) -> dict[str, Any]:
        await self.say(chat_id, "/start")
        return await self.say(chat_id, self.label(lang_key))

    async def link(
        self,
        chat_id: int,
        farmer_id: str = "FR-TEST-0001",
        phone: str = "+251900000101",
        kebele: str = "ET01010101",
        *,
        onboard: bool = True,
    ) -> dict[str, Any]:
        sensitive(farmer_id, phone, phone[-9:], "0" + phone[-9:])
        self.up.farmers[farmer_id] = Farmer(farmer_id, phone, kebele)
        if onboard:
            await self.onboard(chat_id)
        r = await self.say(chat_id, self.label(tk.BTN_LINK_FARMER_ID))
        assert r["text"] == self.text(tk.LINK_ASK_FARMER_ID)
        r = await self.say(chat_id, farmer_id)
        assert r["text"] == self.text(tk.LINK_ASK_CONTACT)
        r = await self.say(chat_id, contact={"user_id": chat_id, "phone_number": phone})
        assert r["text"] == self.text(tk.LINK_CODE_SENT, masked_phone=phone[-2:]), r["text"]
        r = await self.say(chat_id, self.up.otp_code)
        assert r["text"] == self.text(tk.LINK_DONE), r["text"]
        return r
