"""Contract 3.4.0 load test: about 20,000 Telegram broadcast messages waiting (for ten
minutes already, so they count as "aged") while OTPs are submitted through the API.

Two workers run side by side on one database (SMS-only and Telegram-only, production pace:
SMS 4/s with one OTP slot, Telegram 20/s, 1 s idle). The same OTP run is measured first
with no backlog (control) and then with the backlog. OTP submit latency and OTP
claim-to-send / accept-to-send times must stay within the existing pacing limits.
The numbers are printed (run with ``-s``).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from rtsupport import (
    MAX_ACCEPT_TO_SEND_S,
    MAX_CLAIM_TO_SEND_S,
    submit_otps,
    summary,
    two_workers,
    wait_sent,
)
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from wsupport import FakeClock, FakeTelegram, Keys

from oan_messaging.api.app import ApiDependencies, create_app
from oan_messaging.contracts import (
    PURPOSE_PRIORITY,
    Channel,
    FingerprintDomain,
    HmacFingerprinter,
    Purpose,
    SmsSubmission,
    TelegramSubmission,
    canonical_payload,
    mask_chat_id,
)
from oan_messaging.storage.credentials import authenticate, issue_credential, upsert_caller
from oan_messaging.storage.crypto import ContentCipher
from oan_messaging.storage.schema import message_content, messages

BACKLOG = 20_000
N_OTP = 15
EVERY = 0.4


async def insert_backlog(sf: async_sessionmaker[AsyncSession], keys: Keys, caller: str) -> None:
    """20,000 accepted Telegram broadcasts, written in bulk exactly as ``create_or_get``
    would (encrypted content, payload fingerprint), created ten minutes ago."""
    cipher = ContentCipher(keys.content, keys.content_current)
    fp = HmacFingerprinter(keys.fingerprint, keys.fingerprint_current)
    created = datetime.now(UTC) - timedelta(minutes=10)
    for start in range(0, BACKLOG, 2000):
        rows: list[dict[str, Any]] = []
        contents: list[dict[str, Any]] = []
        for i in range(start, min(start + 2000, BACKLOG)):
            mid = uuid.uuid4()
            chat = 600_000_000 + i
            sub = TelegramSubmission(
                chat_id=chat, text="Weather advisory for your kebele.",
                source_reference="bc:BC-LOAD:1", purpose=Purpose.BROADCAST,
            )
            pfp = fp.fingerprint(FingerprintDomain.PAYLOAD, canonical_payload(caller, sub))
            at = created + timedelta(microseconds=i)
            rows.append({
                "id": mid, "caller_id": caller, "channel": "telegram",
                "idempotency_key": str(uuid.uuid4()), "purpose": "broadcast",
                "priority": PURPOSE_PRIORITY[Purpose.BROADCAST],
                "source_reference": "bc:BC-LOAD:1", "status": "accepted",
                "payload_fp_version": pfp.key_version, "payload_fp_digest": pfp.digest,
                "recipient_masked": mask_chat_id(chat), "authorized_bot": "grievance",
                "attempts": 0, "created_at": at, "updated_at": at,
            })
            blob = cipher.encrypt(mid, "telegram", {"text": sub.text, "chat_id": chat})
            contents.append({
                "message_id": mid, "key_version": blob.key_version, "nonce": blob.nonce,
                "ciphertext": blob.ciphertext, "created_at": at,
            })
        async with sf() as s, s.begin():
            await s.execute(insert(messages), rows)
            await s.execute(insert(message_content), contents)
    async with sf() as s:
        await s.execute(__import__("sqlalchemy").text("ANALYZE messages"))
        await s.execute(__import__("sqlalchemy").text("ANALYZE message_content"))
        await s.commit()


async def run_phase(
    label: str,
    keys: Keys,
    sf: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    token: str,
) -> dict[str, Any]:
    async def auth(t: str) -> Any:
        return await authenticate(sf, keys.pepper, t)

    app = create_app(ApiDependencies(repository=keys.repo(sf), authenticate=auth))
    tg = FakeTelegram(FakeClock())
    tg.clock = lambda: datetime.now(UTC)  # type: ignore[assignment]
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as api,
        two_workers(keys, sf, engine, tg) as pods,
    ):
        async def submit(body: SmsSubmission, key: str) -> uuid.UUID:
            resp = await api.post(
                "/api/v1/messages/sms",
                content=body.model_dump_json(exclude_none=True),
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key,
                         "Content-Type": "application/json"},
            )
            assert resp.status_code == 202, resp.text
            return uuid.UUID(resp.json()["id"])

        await_start = time.monotonic()
        accepted, durations = await submit_otps(
            keys.repo(sf), None, N_OTP, EVERY, submit=submit  # type: ignore[arg-type]
        )
        await wait_sent(pods.sms, set(accepted), timeout=20)
        tg_sent = len(tg.calls)
        elapsed = time.monotonic() - await_start
    reached = pods.sms.reached
    assert set(accepted) <= set(reached), f"{label}: OTPs not sent"
    assert max(pods.sms.counter[m] for m in accepted) == 1
    result = {
        "label": label,
        "submit": summary(durations),
        "claim_to_send": summary([reached[m] - pods.claims.claimed[m] for m in accepted]),
        "accept_to_send": summary([reached[m] - accepted[m] for m in accepted]),
        "late_start": sum(s.late_start_not_called for s in pods.sms_stats),
        "telegram_sent": tg_sent,
        "seconds": round(elapsed, 1),
    }
    print(f"\n{result}")
    return result


async def test_otp_pacing_holds_with_20000_queued_telegram_broadcasts(
    sf: async_sessionmaker[AsyncSession], engine: AsyncEngine, keys: Keys
) -> None:
    await upsert_caller(
        sf, "esignet", channels={Channel.SMS}, purposes={Purpose.OTP}, telegram_bot=None,
        enabled=True,
    )
    await upsert_caller(
        sf, "da-kit", channels={Channel.TELEGRAM}, purposes={Purpose.BROADCAST},
        telegram_bot="grievance", enabled=True,
    )
    token = await issue_credential(sf, keys.pepper, "esignet", datetime.now(UTC))

    control = await run_phase("control (no backlog)", keys, sf, engine, token)
    started = time.monotonic()
    await insert_backlog(sf, keys, "da-kit")
    print(f"\nbacklog of {BACKLOG} inserted in {time.monotonic() - started:.1f} s")
    async with sf() as s:
        waiting = (
            await s.execute(
                select(func.count()).select_from(messages).where(
                    messages.c.channel == "telegram", messages.c.status == "accepted"
                )
            )
        ).scalar_one()
    assert waiting == BACKLOG
    load = await run_phase("load (20,000 Telegram broadcasts waiting)", keys, sf, engine, token)

    for r in (control, load):
        assert r["late_start"] == 0, r
        assert r["claim_to_send"]["max_ms"] <= MAX_CLAIM_TO_SEND_S * 1000, r
        assert r["accept_to_send"]["max_ms"] <= MAX_ACCEPT_TO_SEND_S * 1000, r
    # Submit latency is not slowed by the backlog beyond noise: p95 within 2x control or
    # control + 50 ms, whichever is larger.
    limit = max(2 * control["submit"]["p95_ms"], control["submit"]["p95_ms"] + 50)
    assert load["submit"]["p95_ms"] <= limit, (control, load)
    assert load["telegram_sent"] > 0  # the backlog really was draining meanwhile
