"""Fixtures for Bot Services tests against a REAL local PostgreSQL.

``OAN_MSG_TEST_DATABASE_URL`` (the same admin URL the OAN Messaging tests use) must point
at a throwaway local server. One fresh database per session, migrated with Bot Services'
own Alembic configuration; tables truncated before every test. Every other service is a
fake in ``botsupport.py``; nothing leaves the process.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import AsyncIterator, Iterator

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from botenv import ROOT, TABLES, Bot, _Capture, _libpq, make_settings
from botsupport import T0, Upstreams
from sqlalchemy import text

from oan_bot_services import logs
from oan_bot_services.api.app import create_app
from oan_bot_services.clock import fixed_clock
from oan_bot_services.deps import build


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


@pytest.fixture(autouse=True)
def bot_process_logging() -> Iterator[None]:
    """Each Bot Services test runs with the logging of a Bot Services process
    (``logs.install_process``), and every log line is captured for ``test_zz_logs``.

    The capture handler is added after ``install_process`` so it gets no redaction filter:
    it sees what the code and libraries wrote. Everything is undone afterwards, so the OAN
    Messaging tests in the same process see their own logging unchanged."""
    root = logging.getLogger()
    quiet = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    had_filter = {h: logs.FILTER in h.filters for h in root.handlers}
    library = ("uvicorn.error", "sqlalchemy", "alembic")
    lib_had = {n: logs.FILTER in logging.getLogger(n).filters for n in library}
    old_level = root.level
    logs.install_process()
    handler = _Capture()
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        for name, level in quiet.items():
            logging.getLogger(name).setLevel(level)
        for h in root.handlers:
            if not had_filter.get(h, False):
                h.removeFilter(logs.FILTER)
        for n, had in lib_had.items():
            if not had:
                logging.getLogger(n).removeFilter(logs.FILTER)


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
