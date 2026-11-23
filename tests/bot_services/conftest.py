"""Fixtures for Bot Services tests against a REAL local PostgreSQL.

``OAN_MSG_TEST_DATABASE_URL`` (the same admin URL the OAN Messaging tests use) must point
at a throwaway local server. One fresh database per session, migrated with Bot Services'
own Alembic configuration; tables truncated before every test. Every other service is a
fake in ``botsupport.py``; nothing leaves the process.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from botsupport import OAN, RAPIDPRO, STUBS, T0, Upstreams, sensitive
from contract import check_error, check_response
from sqlalchemy import text

from oan_bot_services import logs
from oan_bot_services.api.app import create_app
from oan_bot_services.clock import fixed_clock
from oan_bot_services.deps import Deps, build
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


@pytest.fixture(scope="session")
def bot_db() -> Iterator[str]:
    admin = os.environ.get("OAN_MSG_TEST_DATABASE_URL")
    if not admin:
        pytest.fail("OAN_MSG_TEST_DATABASE_URL is required (a throwaway local PostgreSQL)")
    host = admin.split("@", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if host not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("OAN_MSG_TEST_DATABASE_URL must point to a local database")
    name = f"oan_bot_test_{secrets.token_hex(4)}"
    with psycopg.connect(_libpq(admin), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + "/" + name
    cfg = Config(str(ROOT / "oan_bot_services" / "migrations" / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["skip_logging_config"] = True
    command.upgrade(cfg, "head")
    try:
        yield url
    finally:
        with psycopg.connect(_libpq(admin), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session", autouse=True)
def capture_logs() -> Iterator[None]:
    handler = _Capture()
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(old)


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


@pytest.fixture
async def bot(bot_db: str) -> AsyncIterator[Bot]:
    up = Upstreams()
    http = httpx.AsyncClient(transport=httpx.MockTransport(up.handle))
    deps = build(make_settings(bot_db, up), http=http)
    deps.clock = fixed_clock(T0)
    async with deps.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    app = create_app(deps)
    api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot.test")
    try:
        yield Bot(deps, up, api)
        assert not up.violations, up.violations
    finally:
        await api.aclose()
        await deps.aclose()
        logs.install()
