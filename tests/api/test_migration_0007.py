"""Migration 0007: ``stats_campaigns.channel`` (contract 3.3.0), from a 0006 database
that already holds campaign rows, and back."""

from __future__ import annotations

import secrets
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config

# Own helpers, never ``from conftest import`` (several conftest.py exist under tests/).
ROOT = Path(__file__).resolve().parents[2]


def _libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "migrations" / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["skip_logging_config"] = True
    return cfg


def create_database(admin: str) -> str:
    name = f"oan_msg_m7_{secrets.token_hex(4)}"
    with psycopg.connect(_libpq(admin), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    return admin.rsplit("/", 1)[0] + "/" + name


def drop_database(admin: str, url: str) -> None:
    name = url.rsplit("/", 1)[1]
    with psycopg.connect(_libpq(admin), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

ROW = (
    "INSERT INTO stats_campaigns (campaign, caller_id, purpose, first_at, last_at, recipients,"
    " open, sent, delivered, failed, unknown, expired)"
    " VALUES (%s, %s, 'broadcast', now(), now(), 2, 0, 2, 0, 0, 0, 0)"
)


def test_campaign_channel_upgrade_labels_existing_rows_and_downgrades(admin_url: str) -> None:
    url = create_database(admin_url)
    try:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0006")
        with psycopg.connect(_libpq(url), autocommit=True) as conn:
            conn.execute(
                "INSERT INTO callers (caller_id, allowed_channels, allowed_purposes)"
                " VALUES ('da', '{sms}', '{broadcast}'),"
                " ('tg-only', '{telegram}', '{broadcast}'),"
                " ('both', '{sms,telegram}', '{broadcast}')"
            )
            for campaign, caller in (("da_broadcast:B1", "da"), ("bc:B1", "tg-only"),
                                     ("x:B2", "both")):
                conn.execute(ROW, (campaign, caller))
        command.upgrade(cfg, "head")
        command.check(cfg)
        with psycopg.connect(_libpq(url), autocommit=True) as conn:
            rows = dict(conn.execute("SELECT caller_id, channel FROM stats_campaigns").fetchall())
            assert rows == {"da": "sms", "tg-only": "sms", "both": "sms"}  # existing rows: sms
            # One caller's campaign on two channels is two rows now.
            conn.execute(
                "INSERT INTO stats_campaigns (campaign, caller_id, purpose, channel, first_at,"
                " last_at, recipients, open, sent, delivered, failed, unknown, expired)"
                " VALUES ('x:B2', 'both', 'broadcast', 'telegram', now(), now(), 2,"
                " 0, 2, 0, 0, 0, 0)"
            )
            pk = conn.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname = 'pk_stats_campaigns'"
            ).fetchone()
            assert pk is not None and "channel" in pk[0]
        command.downgrade(cfg, "0006")
        with psycopg.connect(_libpq(url), autocommit=True) as conn:
            left = conn.execute("SELECT caller_id FROM stats_campaigns ORDER BY 1").fetchall()
            assert [r[0] for r in left] == ["both", "da", "tg-only"]
        command.upgrade(cfg, "head")
    finally:
        drop_database(admin_url, url)
