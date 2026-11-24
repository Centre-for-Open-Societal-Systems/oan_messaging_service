"""Migration 0008 (contract 3.4.0): the cancel transition and failure code ``cancelled``,
from a 0007 database that already holds messages, and back."""

from __future__ import annotations

import importlib.util
import secrets
import uuid
from pathlib import Path
from types import ModuleType

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from oan_messaging.contracts import ALLOWED_TRANSITIONS, CANCEL_TRANSITION, FailureCode

# Own helpers, never ``from conftest import`` (several conftest.py exist under tests/).
ROOT = Path(__file__).resolve().parents[2]
FP = "a" * 64


def _libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "migrations" / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["skip_logging_config"] = True
    return cfg


def create_database(admin: str) -> str:
    name = f"oan_msg_m8_{secrets.token_hex(4)}"
    with psycopg.connect(_libpq(admin), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    return admin.rsplit("/", 1)[0] + "/" + name


def drop_database(admin: str, url: str) -> None:
    name = url.rsplit("/", 1)[1]
    with psycopg.connect(_libpq(admin), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def migration() -> ModuleType:
    path = ROOT / "migrations" / "versions" / "0008_telegram_cancel.py"
    spec = importlib.util.spec_from_file_location("m0008", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def insert_message(conn: psycopg.Connection, channel: str) -> uuid.UUID:
    mid = uuid.uuid4()
    conn.execute(
        "INSERT INTO messages (id, caller_id, channel, idempotency_key, purpose, priority,"
        " source_reference, status, payload_fp_version, payload_fp_digest,"
        " recipient_fp_version, recipient_fp_digest, recipient_masked, authorized_bot,"
        " attempts, created_at, updated_at)"
        " VALUES (%s, 'svc', %s, %s, 'broadcast', 30, 'bc:B1:1', 'accepted', 'f1', %s,"
        " %s, %s, 'masked', %s, 0, now(), now())",
        (mid, channel, str(uuid.uuid4()), FP,
         "f1" if channel == "sms" else None, FP if channel == "sms" else None,
         "oan" if channel == "telegram" else None),
    )
    return mid


CANCEL = (
    "UPDATE messages SET status = 'failed', failure_code = %s, content_purged_at = now()"
    " WHERE id = %s"
)


def test_migration_lists_match_the_contract() -> None:
    mod = migration()
    expected = {(a.value, b.value) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets}
    assert set(mod.TRANSITIONS) == expected
    assert mod.CANCEL == tuple(s.value for s in CANCEL_TRANSITION)
    assert set(mod.CODES_0008) == {c.value for c in FailureCode}
    assert set(mod.CODES_0008) - set(mod.CODES_0001) == {"cancelled"}


def test_upgrade_allows_only_the_telegram_cancel_and_downgrade_guards_it(
    admin_url: str,
) -> None:
    url = create_database(admin_url)
    try:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0007")
        with psycopg.connect(_libpq(url), autocommit=True) as conn:
            conn.execute(
                "INSERT INTO callers (caller_id, allowed_channels, allowed_purposes,"
                " telegram_bot) VALUES ('svc', '{sms,telegram}', '{broadcast}', 'oan')"
            )
            before_sms = insert_message(conn, "sms")
            before_tg = insert_message(conn, "telegram")
            # Before 0008: the cancel is refused (code and transition).
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(CANCEL, ("cancelled", before_tg))
        command.upgrade(cfg, "0008")
        with psycopg.connect(_libpq(url), autocommit=True) as conn:
            valid = conn.execute(
                "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_messages_failure_code'"
            ).fetchone()
            assert valid == (True,)
            # Existing rows untouched; the Telegram cancel now works.
            conn.execute(CANCEL, ("cancelled", before_tg))
            # SMS can never be cancelled; Telegram accepted -> failed needs 'cancelled'.
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(CANCEL, ("cancelled", before_sms))
            other_tg = insert_message(conn, "telegram")
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(CANCEL, ("provider_rejected", other_tg))
            # An unknown code is still refused by the check.
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "UPDATE messages SET failure_code = 'made_up' WHERE id = %s", (other_tg,)
                )
        # Downgrade refuses while a cancelled message exists.
        with pytest.raises(RuntimeError, match="cancelled messages exist"):
            command.downgrade(cfg, "0007")
        with psycopg.connect(_libpq(url), autocommit=True) as conn:
            conn.execute("DELETE FROM messages WHERE failure_code = 'cancelled'")
        command.downgrade(cfg, "0007")
        with psycopg.connect(_libpq(url), autocommit=True) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(CANCEL, ("cancelled", other_tg))
        command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        drop_database(admin_url, url)
