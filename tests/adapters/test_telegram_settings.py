"""Telegram as a broadcast channel (SMS-02 revised): pace and API address from
configuration, link previews off. SMS settings and pacing stay as they were.

Every Bot API call is mocked (httpx.MockTransport). Tokens below are fake."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from oan_messaging.adapters.telegram import (
    DEFAULT_API_BASE_URL,
    LINK_PREVIEW_OFF,
    TelegramAdapter,
    TelegramBotRegistry,
)
from oan_messaging.contracts import ProviderOutcome, TelegramSendRequest
from oan_messaging.runtime.__main__ import telegram_limit
from oan_messaging.storage.config import StorageSettings
from oan_messaging.worker.rate_limit import SMS_8028_DEFAULT, TELEGRAM_DEFAULT, RateLimit

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijk"  # noqa: S105 (fake test token)
NOW = datetime(2026, 12, 1, 5, 30, tzinfo=UTC)
CHAT = 555000222

BASE_ENV = {
    "OAN_MSG_DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:5432/db",
    "OAN_MSG_CONTENT_KEYS": "v1:" + "A" * 44,
    "OAN_MSG_CONTENT_KEY_CURRENT": "v1",
    "OAN_MSG_FINGERPRINT_KEYS": "k1:" + "B" * 44,
    "OAN_MSG_FINGERPRINT_KEY_CURRENT": "k1",
    "OAN_MSG_CREDENTIAL_PEPPER": "C" * 44,
}


def settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> StorageSettings:
    for name in list(os.environ):
        if name.startswith("OAN_MSG_"):
            monkeypatch.delenv(name, raising=False)
    for k, v in {**BASE_ENV, **env}.items():
        monkeypatch.setenv(k, v)
    return StorageSettings()


# ------------------------------------------------------------------- 1. Telegram pace


def test_pace_defaults_to_20_with_no_otp_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(monkeypatch)
    assert s.telegram_rate_per_second == 20 and s.telegram_otp_reserve == 0
    assert telegram_limit(s) == RateLimit(requests=20, otp_reserve=0)
    assert TELEGRAM_DEFAULT == RateLimit(requests=20, window=timedelta(seconds=1))


def test_pace_from_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(
        monkeypatch,
        OAN_MSG_TELEGRAM_RATE_PER_SECOND="30",
        OAN_MSG_TELEGRAM_OTP_RESERVE="2",
    )
    assert telegram_limit(s) == RateLimit(requests=30, otp_reserve=2)


@pytest.mark.parametrize("rate", ["31", "100", "0", "-1"])
def test_pace_out_of_range_refuses_to_start(monkeypatch: pytest.MonkeyPatch, rate: str) -> None:
    with pytest.raises(ValidationError):
        settings(monkeypatch, OAN_MSG_TELEGRAM_RATE_PER_SECOND=rate)


def test_otp_reserve_must_stay_below_the_pace(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(
        monkeypatch, OAN_MSG_TELEGRAM_RATE_PER_SECOND="5", OAN_MSG_TELEGRAM_OTP_RESERVE="5"
    )
    with pytest.raises(ValueError, match="otp_reserve"):
        telegram_limit(s)
    with pytest.raises(ValidationError):
        settings(monkeypatch, OAN_MSG_TELEGRAM_OTP_RESERVE="-1")


def test_telegram_settings_leave_sms_pacing_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(
        monkeypatch,
        OAN_MSG_TELEGRAM_RATE_PER_SECOND="7",
        OAN_MSG_TELEGRAM_OTP_RESERVE="1",
    )
    assert s.sms_rate_per_second == 4 and s.sms_otp_reserve == 1
    assert SMS_8028_DEFAULT == RateLimit(requests=4, otp_reserve=1)


# -------------------------------------------------------------- 3. API address


def test_api_address_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings(monkeypatch).telegram_api_base_url == DEFAULT_API_BASE_URL
    s = settings(monkeypatch, OAN_MSG_TELEGRAM_API_BASE_URL="https://tg.example.invalid:8443")
    assert s.telegram_api_base_url == "https://tg.example.invalid:8443"


def registry() -> TelegramBotRegistry:
    return TelegramBotRegistry({"oan": SecretStr(FAKE_TOKEN)})


def req(text: str = "Rain expected\n\nSee https://example.invalid/forecast") -> TelegramSendRequest:
    return TelegramSendRequest(
        message_id=uuid.uuid4(), attempt=1, bot="oan", chat_id=CHAT, text=text
    )


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"ok": True, "result": {"message_id": 9, "chat": {"id": CHAT, "type": "private"}}},
    )


async def test_configured_api_address_is_used() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(
            registry(), client, api_base_url="https://tg.example.invalid:8443", clock=lambda: NOW
        )
        result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.SENT
    assert seen[0].url.host == "tg.example.invalid" and seen[0].url.port == 8443


@pytest.mark.parametrize(
    "base",
    ["http://api.telegram.org", "https://api.telegram.org/path", "https://u:p@tg.example.invalid"],
)
def test_configured_api_address_must_be_an_https_origin(base: str) -> None:
    with pytest.raises(ValueError):
        TelegramAdapter(registry(), httpx.AsyncClient(), api_base_url=base)


# ----------------------------------------------------------- 2. link previews off


async def test_every_send_turns_link_previews_off() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return ok(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(registry(), client, clock=lambda: NOW)
        for text in ("plain", "with link https://example.invalid", req().text):
            await adapter.send(req(text))
    assert LINK_PREVIEW_OFF == {"is_disabled": True}
    assert [b["link_preview_options"] for b in bodies] == [{"is_disabled": True}] * 3
    assert all(set(b) == {"chat_id", "text", "link_preview_options"} for b in bodies)
