"""Real-time helpers for the contract 3.4.0 isolation and load tests: an SMS worker and a
Telegram worker run side by side on the same database (like two pods), with the real
clock, production pace settings and the default worker loop (1 s idle).

The SMS worker uses a fake 8028 adapter that records when each message reached it.
"""

from __future__ import annotations

import asyncio
import contextlib
import statistics
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine
from wsupport import OTP_TEXT, FakeSms, Keys

from oan_messaging.contracts import (
    CallerContext,
    Channel,
    MessageAdapter,
    ProviderResult,
    SendRequest,
    SmsSubmission,
)
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.worker import RateLimit, SharedRateLimiter, Worker, WorkerConfig

#: Existing pacing limits these tests hold SMS to (tests/worker/test_pacing_and_batching.py
#: and the worker loop): an OTP gets its reserved slot within one 1 s pace window of being
#: claimed, and a waiting OTP is claimed within one idle period (1 s) of acceptance.
PACE_WINDOW_S = 1.0
IDLE_S = 1.0
MAX_CLAIM_TO_SEND_S = PACE_WINDOW_S
MAX_ACCEPT_TO_SEND_S = IDLE_S + PACE_WINDOW_S


class TimedSms(FakeSms):
    """Fake 8028 that records the monotonic time each message reached it."""

    def __init__(self) -> None:
        super().__init__(clock=None)  # type: ignore[arg-type]
        self.clock = lambda: datetime.now(UTC)  # type: ignore[assignment]
        self.reached: dict[uuid.UUID, float] = {}

    async def send(self, request: SendRequest) -> ProviderResult:
        self.reached.setdefault(request.message_id, time.monotonic())
        return await super().send(request)


class ClaimTimer:
    """Wraps a repository's ``claim_due`` to record when each message was claimed."""

    def __init__(self, repo: PostgresMessageRepository) -> None:
        self.claimed: dict[uuid.UUID, float] = {}
        original = repo.claim_due

        async def timed(*a: Any, **kw: Any) -> Any:
            claims = await original(*a, **kw)
            at = time.monotonic()
            for c in claims:
                self.claimed.setdefault(c.message.id, at)
            return claims

        repo.claim_due = timed  # type: ignore[method-assign]


def production_limiter() -> SharedRateLimiter:
    """Pace as in production defaults: SMS 4/s with 1 OTP slot, Telegram 20/s."""
    return SharedRateLimiter(
        None,
        {
            Channel.SMS: RateLimit(requests=4, otp_reserve=1),
            Channel.TELEGRAM: RateLimit(requests=20, otp_reserve=0),
        },
    )


@dataclass
class Pods:
    sms_repo: PostgresMessageRepository
    sms: TimedSms
    claims: ClaimTimer
    sms_stats: list[Any] = field(default_factory=list)


@contextlib.asynccontextmanager
async def two_workers(
    keys: Keys,
    sf: Any,
    engine: AsyncEngine,
    telegram: MessageAdapter,
) -> AsyncIterator[Pods]:
    """Run an SMS-only worker and a Telegram-only worker until the block ends."""
    sms_repo, tg_repo = keys.repo(sf), keys.repo(sf)
    sms = TimedSms()
    pods = Pods(sms_repo, sms, ClaimTimer(sms_repo))
    sms_limiter, tg_limiter = production_limiter(), production_limiter()
    sms_worker = Worker(
        sms_repo, {Channel.SMS: sms}, sms_limiter,
        WorkerConfig(worker_id="pod-sms", broadcast_enabled=False),
    )
    tg_worker = Worker(
        tg_repo, {Channel.TELEGRAM: telegram}, tg_limiter,
        WorkerConfig(
            worker_id="pod-tg", broadcast_enabled=False, telegram_broadcast_enabled=True,
        ),
    )
    original = sms_worker.run_once

    async def counted() -> Any:
        stats = await original()
        pods.sms_stats.append(stats)
        return stats

    sms_worker.run_once = counted  # type: ignore[method-assign]
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(sms_worker.run_forever(stop, idle=IDLE_S)),
        asyncio.create_task(tg_worker.run_forever(stop, idle=IDLE_S)),
    ]
    try:
        yield pods
    finally:
        stop.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
        await sms_limiter.aclose()
        await tg_limiter.aclose()


async def submit_otps(
    repo: PostgresMessageRepository,
    ctx: CallerContext,
    n: int,
    every: float,
    submit: Callable[[SmsSubmission, str], Any] | None = None,
) -> tuple[dict[uuid.UUID, float], list[float]]:
    """Submit ``n`` OTPs, one every ``every`` seconds. Returns accept times (monotonic) and
    each submit's duration in seconds."""
    accepted: dict[uuid.UUID, float] = {}
    durations: list[float] = []
    for i in range(n):
        body = SmsSubmission.model_validate({
            "recipient": f"0900000{i % 1000:03d}",
            "text": OTP_TEXT,
            "source_reference": f"esignet:otp:{i}",
            "purpose": "otp",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        })
        start = time.monotonic()
        if submit is None:
            msg, _ = await repo.create_or_get(ctx, str(uuid.uuid4()), body, datetime.now(UTC))
            mid = msg.id
        else:
            mid = await submit(body, str(uuid.uuid4()))
        done = time.monotonic()
        accepted[mid] = done
        durations.append(done - start)
        await asyncio.sleep(every)
    return accepted, durations


async def wait_sent(sms: TimedSms, ids: set[uuid.UUID], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not ids <= set(sms.reached) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


def summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {
        "n": len(values),
        "median_ms": round(statistics.median(values) * 1000, 1),
        "p95_ms": round(p95 * 1000, 1),
        "max_ms": round(max(values) * 1000, 1),
    }
