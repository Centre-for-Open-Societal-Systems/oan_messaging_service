"""Shared Bot Services test helpers: a normal module, so tests never import ``conftest``
or another test file by name (the repository has several ``conftest.py``)."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from botsupport import OAN, RAPIDPRO, STUBS, Farmer, Upstreams, rid, sensitive
from contract import check_error, check_response
from sqlalchemy import text

from oan_bot_services.deps import Deps
from oan_bot_services.settings import BotSettings

ROOT = Path(__file__).resolve().parents[2]
TABLES = (
    "broadcast_recipients, broadcasts, broadcast_bindings, request_replays, contacts, "
    "farmer_links, subscriptions, link_requests, link_failures, link_locks, staff_links, "
    "keyed_queue, stop_requests, area_cache"
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
sensitive(BROADCAST_TOKEN, FLOW_SECRET)


def make_settings(db: str, up: Upstreams, **over: Any) -> BotSettings:
    values: dict[str, Any] = {
        "env": "local",
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
