"""Bot Services migrations: from empty to head, check against the models, down and up."""

from __future__ import annotations

import os
import secrets

import psycopg
from alembic import command
from alembic.config import Config
from botenv import ROOT, _libpq


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


def test_0002_keeps_rows_and_starts_the_clock_for_unreachable_ones() -> None:
    """Upgrading a wave 1 database: RapidPro columns become optional, every contact gets a
    subscriber_id, and an unreachable subscription starts its 90 days at the upgrade."""
    admin = os.environ["OAN_MSG_TEST_DATABASE_URL"]
    name = f"oan_bot_mig_{secrets.token_hex(4)}"
    with psycopg.connect(_libpq(admin), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + "/" + name
    try:
        cfg = Config(str(ROOT / "oan_bot_services" / "migrations" / "alembic.ini"))
        cfg.attributes["database_url"] = url
        cfg.attributes["skip_logging_config"] = True
        command.upgrade(cfg, "0001")
        with psycopg.connect(_libpq(url)) as conn:
            conn.execute(
                "INSERT INTO contacts (contact_ref, contact_uuid, updated_at) "
                "VALUES ('ref-a', gen_random_uuid(), now())"
            )
            for ref, fid, state in (("ref-a", "FR-TEST-M1", "active"),
                                    ("ref-b", "FR-TEST-M2", "unreachable")):
                conn.execute(
                    "INSERT INTO subscriptions (contact_ref, contact_uuid, farmer_id, kebele_code,"
                    " woreda_code, zone_code, region_code, updates_on, chat_id, state,"
                    " last_activity_at) VALUES (%s, gen_random_uuid(), %s, 'K', 'W', 'Z', 'R',"
                    " true, 1, %s, now())",
                    (ref, fid, state),
                )
        command.upgrade(cfg, "head")
        with psycopg.connect(_libpq(url)) as conn:
            assert conn.execute("SELECT subscriber_id IS NOT NULL FROM contacts").fetchall() == [
                (True,)]
            rows = conn.execute(
                "SELECT contact_ref, stopped_at IS NOT NULL FROM subscriptions ORDER BY 1"
            ).fetchall()
            assert rows == [("ref-a", False), ("ref-b", True)]
            conn.execute(
                "INSERT INTO subscriptions (contact_ref, farmer_id, kebele_code, woreda_code,"
                " zone_code, region_code, updates_on, chat_id, state, stopped_at,"
                " last_activity_at) VALUES ('ref-c', 'FR-TEST-M3', 'K', 'W', 'Z', 'R', false, 2,"
                " 'stopped', now(), now())"
            )
        # Back to wave 1: rows without a RapidPro contact cannot stay.
        command.downgrade(cfg, "0001")
        with psycopg.connect(_libpq(url)) as conn:
            refs = [r[0] for r in conn.execute("SELECT contact_ref FROM subscriptions ORDER BY 1")]
            assert refs == ["ref-a", "ref-b"]
    finally:
        with psycopg.connect(_libpq(admin), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
