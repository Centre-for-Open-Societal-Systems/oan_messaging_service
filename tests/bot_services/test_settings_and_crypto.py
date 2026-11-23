"""Settings, the clock rule, contact_ref, the data box and redaction. No database."""

from __future__ import annotations

import base64
import logging
import secrets
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from oan_bot_services import logs
from oan_bot_services.clock import make_clock, system_now
from oan_bot_services.crypto import Keys, masked_last_two, normalise_phone
from oan_bot_services.settings import BotSettings, same_secret


def b64(n: int = 32) -> str:
    return base64.b64encode(secrets.token_bytes(n)).decode()


BASE = {
    "database_url": "postgresql+psycopg://u:p@127.0.0.1:5432/bot",
    "flow_secret": "f" * 32,
    "contact_ref_key": b64(),
    "fingerprint_key": b64(),
    "data_key": b64(),
}


def test_fake_clock_is_refused_outside_local() -> None:
    with pytest.raises(ValidationError, match="only with OAN_BOT_ENV=local"):
        BotSettings(**BASE, env="production", clock_url="http://stubs:8200/clock")
    with pytest.raises(ValidationError):
        BotSettings(**BASE, env="staging", clock_url="http://stubs:8200/clock")
    assert BotSettings(**BASE, env="local", clock_url="http://stubs:8200/clock").clock_url


def test_settings_refuse_weak_or_malformed_secrets() -> None:
    for over in (
        {"data_key": b64(16)},
        {"contact_ref_key": "not base64!"},
        {"flow_secret": "short"},
        {"database_url": "sqlite:///x"},
        {"broadcast_token_sha256": "abc"},
        {"fingerprint_key": b64(8)},
    ):
        with pytest.raises(ValidationError):
            BotSettings(**{**BASE, **over})


def test_settings_defaults_and_secret_list() -> None:
    s = BotSettings(**BASE)
    assert s.env == "production" and s.client_timeout == 5.0
    assert s.poll_interval_seconds == 10 and s.broadcast_binding_days == 30
    assert s.flow_secret.get_secret_value() in s.secrets()
    assert "f" * 32 not in repr(s)
    with pytest.raises(ValidationError):
        BotSettings(**BASE, poll_interval_seconds=5)


def test_flow_secret_comparison() -> None:
    from pydantic import SecretStr

    assert same_secret("abc", SecretStr("abc"))
    assert not same_secret("abd", SecretStr("abc"))
    assert not same_secret(None, SecretStr("abc"))
    assert not same_secret("", SecretStr(""))


async def test_clock_reads_the_fake_clock_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/clock"
        return httpx.Response(200, json={"now": "2026-12-01T08:30:00+03:00", "mode": "frozen"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    now = await make_clock("http://stubs.example.invalid/clock", http)()
    assert now == datetime(2026, 12, 1, 5, 30, tzinfo=UTC)
    assert make_clock(None) is system_now
    assert (await system_now()).tzinfo is not None


async def test_clock_refuses_a_time_without_offset() -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"now": "2026-12-01T08:30:00"})
        )
    )
    with pytest.raises(ValueError):
        await make_clock("http://stubs.example.invalid/clock", http)()


def test_contact_ref_is_keyed_and_stable() -> None:
    k1 = Keys(secrets.token_bytes(32), secrets.token_bytes(32), secrets.token_bytes(32))
    k2 = Keys(secrets.token_bytes(32), secrets.token_bytes(32), secrets.token_bytes(32))
    ref = k1.contact_ref(700000001)
    assert ref == k1.contact_ref(700000001) and len(ref) == 64
    assert ref != k2.contact_ref(700000001) and "700000001" not in ref
    assert k1.fingerprint("a", "b") != k1.fingerprint("ab")
    assert k1.body_fingerprint({"a": 1, "b": 2}) == k1.body_fingerprint({"b": 2, "a": 1})
    sealed = k1.seal("+251900000101", "linked_phone")
    assert b"900000101" not in sealed and k1.open(sealed, "linked_phone") == "+251900000101"
    with pytest.raises(InvalidTag):
        k1.open(sealed, "broadcast")  # bound to its purpose
    assert "redacted" in repr(k1)
    with pytest.raises(ValueError):
        Keys(b"x" * 32, b"x" * 32, b"short")


@pytest.mark.parametrize(
    ("raw", "e164"),
    [
        ("+251900000101", "+251900000101"),
        ("251900000101", "+251900000101"),
        ("0900000101", "+251900000101"),
        ("900000101", "+251900000101"),
        ("+251 700 000 101", "+251700000101"),
        ("+1 555 0100", None),
        ("0800000101", None),
    ],
)
def test_phone_normalising(raw: str, e164: str | None) -> None:
    assert normalise_phone(raw) == e164


def test_masked_phone_is_the_last_two_digits() -> None:
    assert masked_last_two("+251900000121") == "21"


def test_redaction_filter_scrubs_identifiers_but_keeps_refs_and_ids() -> None:
    ref = "a" * 64
    rid = "2d1c2c55-6b3c-4d1e-9a7b-000000000123"
    logs.install("s3cret-value-xyz")
    line = logs.clean(
        f"chat 700000123 phone +251900000101 farmer FR-TEST-0001 code 482913 "
        f"Bearer abc.def token 123456789:AAFakeTokenForTestsOnly_abcdefghijk "
        f"secret s3cret-value-xyz ref {ref} id {rid}"
    )
    for bad in (
        "700000123",
        "900000101",
        "FR-TEST",
        "482913",
        "abc.def",
        "AAFake",
        "s3cret-value-xyz",
    ):
        assert bad not in line, bad
    assert ref in line and rid in line


def test_redaction_filter_on_a_logger() -> None:
    logger = logs.get("oan_bot_services.test")
    records: list[logging.LogRecord] = []

    class H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    h = H()
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    try:
        logger.info("phone=%s", "+251900000101")
        try:
            raise ValueError("chat 700000999")
        except ValueError:
            logger.exception("failed")
    finally:
        logger.removeHandler(h)
    assert "900000101" not in records[0].getMessage()
    assert records[1].exc_text is not None and "700000999" not in records[1].exc_text
    assert logging.getLogger("httpx").level >= logging.WARNING
