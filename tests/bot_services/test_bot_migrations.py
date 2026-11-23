"""Bot Services migrations: from empty to head, check against the models, down and up."""

from __future__ import annotations

import os
import secrets

import psycopg
from alembic import command
from alembic.config import Config
from conftest import ROOT, _libpq


def test_upgrade_check_downgrade_upgrade() -> None:
    admin = os.environ["OAN_MSG_TEST_DATABASE_URL"]
    name = f"oan_bot_mig_{secrets.token_hex(4)}"
    with psycopg.connect(_libpq(admin), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + "/" + name
    try:
        cfg = Config(str(ROOT / "oan_bot_services" / "migrations" / "alembic.ini"))
        cfg.attributes["database_url"] = url
        cfg.attributes["skip_logging_config"] = True
        command.upgrade(cfg, "head")
        command.check(cfg)
        with psycopg.connect(_libpq(url)) as conn:
            tables = {
                r[0]
                for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            }
        assert "bot_alembic_version" in tables and "subscriptions" in tables
        assert "alembic_version" not in tables  # never collides with OAN Messaging's
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
    finally:
        with psycopg.connect(_libpq(admin), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
