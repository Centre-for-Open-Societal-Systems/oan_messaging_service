"""Contract 3.4.0: Telegram trouble never stops or slows SMS (real time, two workers on
one database, production pace settings).

The Telegram worker uses the REAL Telegram adapter against a mocked Bot API (no network):
an invalid bot token (401), an outage (500 after a delay, or connection refused), and
429 with ``retry_after``. Meanwhile OTPs go through the SMS worker. Each OTP must reach
8028 within the existing pacing limits, exactly once, and the SMS scope must never be
paused.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from rtsupport import (
    MAX_ACCEPT_TO_SEND_S,
    MAX_CLAIM_TO_SEND_S,
    submit_otps,
    summary,
    two_workers,
    wait_sent,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tgsupport import submit_tg
from wsupport import Keys, make_caller

from oan_messaging.adapters.telegram import TelegramAdapter, TelegramBotRegistry
from oan_messaging.storage.schema import dispatch_controls, messages

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijk"  # noqa: S105 (fake test token)
N_TELEGRAM = 60
N_OTP = 8


def bot_api(mode: str) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        if mode == "ok":
            chat = json.loads(request.content)["chat_id"]
            return httpx.Response(200, json={"ok": True, "result": {
                "message_id": 1, "date": 1788246733, "chat": {"id": chat, "type": "private"}}})
        if mode == "invalid_token":
            return httpx.Response(401, json={"ok": False, "error_code": 401,
                                             "description": "Unauthorized"})
        if mode == "outage_5xx":
            await asyncio.sleep(2.0)  # a slow, failing upstream
            return httpx.Response(502, text="bad gateway")
        if mode == "refused":
            raise httpx.ConnectError("connection refused", request=request)
        if mode == "rate_limited":
            return httpx.Response(429, json={
                "ok": False, "error_code": 429, "description": "Too Many Requests",
                "parameters": {"retry_after": 30}})
        raise AssertionError(mode)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "mode", ["ok", "invalid_token", "outage_5xx", "refused", "rate_limited"]
)
async def test_telegram_failure_does_not_stop_or_slow_sms(
    repo: Any,
    sf: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keys,
    mode: str,
) -> None:
    ctx = await make_caller(sf, keys, bot="grievance")
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    for i in range(N_TELEGRAM):
        await submit_tg(repo, ctx, at=now, chat_id=555002000 + i)
    telegram = TelegramAdapter(
        TelegramBotRegistry({"grievance": SecretStr(FAKE_TOKEN)}), bot_api(mode)
    )
    async with two_workers(keys, sf, engine, telegram) as pods:
        await asyncio.sleep(1.5)  # Telegram trouble is under way before the first OTP
        accepted, _ = await submit_otps(repo, ctx, N_OTP, every=0.4)
        await wait_sent(pods.sms, set(accepted), timeout=15)
    reached = pods.sms.reached
    assert set(accepted) <= set(reached), f"{mode}: OTPs not sent"
    assert max(pods.sms.counter[m] for m in accepted) == 1
    accept_to_send = [reached[m] - accepted[m] for m in accepted]
    claim_to_send = [reached[m] - pods.claims.claimed[m] for m in accepted]
    print(f"\nmode={mode} accept_to_send={summary(accept_to_send)} "
          f"claim_to_send={summary(claim_to_send)}")
    assert max(accept_to_send) <= MAX_ACCEPT_TO_SEND_S, (mode, accept_to_send)
    assert max(claim_to_send) <= MAX_CLAIM_TO_SEND_S, (mode, claim_to_send)
    assert sum(s.late_start_not_called for s in pods.sms_stats) == 0
    async with sf() as s:
        sms_scope = (
            await s.execute(select(dispatch_controls).where(dispatch_controls.c.channel == "sms"))
        ).mappings().first()
        tg_states = (
            await s.execute(select(messages.c.status).where(messages.c.channel == "telegram"))
        ).scalars().all()
    assert sms_scope is None or (
        not sms_scope["suspended"] and sms_scope["cooldown_until"] is None
    ), f"{mode}: SMS scope changed {dict(sms_scope)}"
    print(f"mode={mode} telegram states={sorted(set(tg_states))}")
    if mode == "ok":
        assert "sent" in tg_states
    else:
        assert "sent" not in tg_states  # Telegram really was failing
