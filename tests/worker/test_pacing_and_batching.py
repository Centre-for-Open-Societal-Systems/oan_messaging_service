"""SMS pacing per SMS part at a true rate, and 8028 batching (real PostgreSQL).

Pace: a true ``requests`` units per window, counted per SMS part, one slot kept for OTP;
a batch counts numbers x parts. Batching: broadcast and alert only, same caller and text,
off by default; one message per recipient with its own attempt, status and sms_id.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from wsupport import (
    T0,
    FakeClock,
    FakeSms,
    Keys,
    make_caller,
    make_worker,
    outcome,
    sent,
    submit_sms,
)

from oan_messaging.adapters.sms8028 import BATCH_TOO_LARGE
from oan_messaging.contracts import (
    FailureCode,
    Provider,
    ProviderOutcome,
    ProviderResult,
    SmsSendRequest,
    sms_parts,
)
from oan_messaging.storage.repository import PostgresMessageRepository, Reservation
from oan_messaging.storage.schema import message_attempts, messages
from oan_messaging.worker import RateLimit
from oan_messaging.worker.rate_limit import SMS_8028_DEFAULT, RateLimited, pace

ONE = "Rain expected tomorrow."  # one part
TWO = "Rain expected tomorrow. " * 8  # 192 GSM-7 characters: two parts
THREE = "ዝናብ " * 40  # Ethiopic, 160 UTF-16 units: three parts


def at(ms: int) -> datetime:
    return T0 + timedelta(milliseconds=ms)


# ------------------------------------------------------------ the pace decision


def test_text_part_counts_used_by_these_tests() -> None:
    assert (sms_parts(ONE), sms_parts(TWO), sms_parts(THREE)) == (1, 2, 3)


def test_true_four_a_second_with_one_slot_kept_for_otp() -> None:
    limit = SMS_8028_DEFAULT
    assert (limit.requests, limit.otp_reserve, limit.window) == (4, 1, timedelta(seconds=1))
    starts: list[tuple[datetime, int]] = []
    for ms in (0, 0, 0):  # three broadcast SMS at once
        assert isinstance(pace(limit, at(ms), 1, False, starts, None, None), Reservation)
        starts.append((at(ms), 1))
    # The fourth slot of the second is kept for OTP.
    refused = pace(limit, at(0), 1, False, starts, None, None)
    assert isinstance(refused, RateLimited) and refused.retry_at == at(1000)
    assert isinstance(pace(limit, at(0), 1, True, starts, None, None), Reservation)
    starts.append((at(0), 1))
    assert isinstance(pace(limit, at(0), 1, True, starts, None, None), RateLimited)
    # Exactly one window later (not 1.4 s): free again.
    assert isinstance(pace(limit, at(1000), 1, False, starts, None, None), Reservation)


def test_a_three_part_sms_uses_three_units() -> None:
    limit = SMS_8028_DEFAULT
    starts = [(at(0), 3)]  # one 3-part broadcast
    assert isinstance(pace(limit, at(10), 1, False, starts, None, None), RateLimited)
    assert isinstance(pace(limit, at(10), 1, True, starts, None, None), Reservation)
    # A 2-part OTP does not fit beside it (3 + 2 > 4).
    assert isinstance(pace(limit, at(10), 2, True, starts, None, None), RateLimited)


def test_a_batch_bigger_than_one_window_waits_for_an_empty_window_then_leaves_debt() -> None:
    limit = SMS_8028_DEFAULT
    busy = [(at(0), 1)]
    assert isinstance(pace(limit, at(500), 10, False, busy, None, None), RateLimited)
    got = pace(limit, at(1000), 10, False, busy, None, None)
    assert isinstance(got, Reservation)
    # 10 units: 2.5 s of the provider's time (4 a second), 3.33 s of the non-OTP share.
    assert got.pace_total_until == at(1000 + 2500)
    assert got.pace_bulk_until is not None
    assert abs((got.pace_bulk_until - at(1000)).total_seconds() - 10 / 3) < 1e-5
    after = [(at(1000), 10)]
    otp = pace(limit, at(1100), 1, True, after, got.pace_total_until, got.pace_bulk_until)
    assert isinstance(otp, RateLimited) and otp.retry_at == at(3500)
    bulk = pace(limit, at(1100), 1, False, after, got.pace_total_until, got.pace_bulk_until)
    assert isinstance(bulk, RateLimited) and bulk.retry_at == got.pace_bulk_until


# ------------------------------------------------------------------- helpers


class BatchSms(FakeSms):
    """Fake 8028 with ``send_batch``; every batch is recorded."""

    def __init__(
        self,
        clock: FakeClock,
        batch_script: Callable[[Sequence[SmsSendRequest], datetime], list[ProviderResult]]
        | None = None,
    ) -> None:
        super().__init__(clock)
        self.batches: list[list[SmsSendRequest]] = []
        self.batch_script = batch_script

    async def send_batch(self, requests: Sequence[SmsSendRequest]) -> list[ProviderResult]:
        self.batches.append(list(requests))
        for r in requests:
            self.counter[r.message_id] += 1
        now = self.clock()
        if self.batch_script is not None:
            return self.batch_script(requests, now)
        out = []
        for _ in requests:
            self._seq += 1
            out.append(sent(Provider.SMS_8028, now, str(self._seq)))
        return out


async def no_time(_s: float) -> None:
    await asyncio.sleep(0)


async def submit(
    repo: PostgresMessageRepository,
    ctx: Any,
    n: int,
    *,
    text: str = ONE,
    purpose: str = "broadcast",
    first: int = 0,
    at_: datetime = T0,
) -> list[Any]:
    return [
        await submit_sms(
            repo,
            ctx,
            at=at_,
            recipient=f"0900000{first + i:03d}",
            text=text,
            purpose=purpose,
            expires_in=None if purpose != "otp" else timedelta(minutes=5),
        )
        for i in range(n)
    ]


async def rows(sf: async_sessionmaker[AsyncSession]) -> dict[Any, dict[str, Any]]:
    async with sf() as s:
        got = (
            await s.execute(
                select(messages.c.id, messages.c.status, messages.c.provider_message_id)
            )
        ).mappings()
        return {r["id"]: dict(r) for r in got}


async def attempt_units(sf: async_sessionmaker[AsyncSession]) -> list[tuple[datetime, int]]:
    async with sf() as s:
        got = await s.execute(
            select(message_attempts.c.started_at, message_attempts.c.units).order_by(
                message_attempts.c.started_at
            )
        )
        return [(r[0], r[1]) for r in got]


def worst_window(starts: list[tuple[datetime, int]], window: timedelta) -> int:
    return max(
        (sum(u for s, u in starts if t <= s < t + window) for t, _ in starts), default=0
    )


# --------------------------------------------------------------------- pacing


async def test_broadcasts_go_at_a_true_three_a_second_counted_per_part(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    await submit(repo, ctx, 6, text=ONE)
    await submit(repo, ctx, 4, text=THREE, first=100)
    sms = FakeSms(clock)
    w = make_worker(repo, engine, clock, sms=sms, sms_limit=SMS_8028_DEFAULT, batch_size=10)
    for _ in range(40):
        await w.run_once()
        if len(sms.counter) == 10:
            break
        clock.advance(milliseconds=250)
    assert len(sms.counter) == 10 and max(sms.counter.values()) == 1
    starts = await attempt_units(sf)
    assert sorted(u for _, u in starts) == [1] * 6 + [3] * 4  # units = parts
    # Never more than 3 units (4 minus the OTP slot) in any real second.
    assert worst_window(starts, timedelta(seconds=1)) <= 3
    # 18 units at 3 a second take 5 to 6 seconds; a 3-part SMS needs a second to itself,
    # and this worker only looks again every 250 ms, so allow up to 7.5 s.
    span = (starts[-1][0] - starts[0][0]).total_seconds()
    assert 4.5 <= span <= 7.5, span


async def test_otp_still_gets_its_slot_while_broadcasts_fill_the_rest(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    bulk = await submit(repo, ctx, 5)
    sms = FakeSms(clock)
    w = make_worker(repo, engine, clock, sms=sms, sleep=no_time, sms_limit=SMS_8028_DEFAULT)
    await w.run_once()
    assert len(sms.calls) == 3  # 3 broadcasts fill the non-OTP share of this second
    otp = (await submit(repo, ctx, 1, purpose="otp", first=500, at_=clock.now))[0]
    await w.run_once()
    assert sms.counter[otp.id] == 1
    assert sum(sms.counter[b.id] for b in bulk) == 3


# ------------------------------------------------------------------- batching


async def test_batching_is_off_by_default(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    await submit(repo, ctx, 4)
    sms = BatchSms(clock)
    w = make_worker(repo, engine, clock, sms=sms)
    await w.run_once()
    assert sms.batches == [] and len(sms.calls) == 4


async def test_same_text_broadcasts_go_in_one_request_each_with_its_own_sms_id(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    bulk = await submit(repo, ctx, 5)
    other = await submit(repo, ctx, 2, text=TWO, first=50)
    alerts = await submit(repo, ctx, 2, purpose="alert", first=60)
    otps = await submit(repo, ctx, 2, purpose="otp", first=70, text="Your OAN code is 482913.")
    notices = await submit(repo, ctx, 2, purpose="transactional", first=80)
    sms = BatchSms(clock)
    w = make_worker(repo, engine, clock, sms=sms, batch_max_recipients=100)
    stats = await w.run_once()
    groups = sorted(sorted(r.message_id for r in b) for b in sms.batches)
    assert groups == sorted(
        [sorted(m.id for m in bulk), sorted(m.id for m in other), sorted(m.id for m in alerts)]
    )
    # OTPs and transactional notices: one per request, even with the same text.
    assert sorted(c.message_id for c in sms.calls) == sorted(m.id for m in otps + notices)
    assert (stats.batches, stats.batched_messages) == (3, 9)
    state = await rows(sf)
    ids = [state[m.id]["provider_message_id"] for m in bulk + other + alerts]
    assert all(state[m.id]["status"] == "sent" for m in bulk + other + alerts)
    assert len(set(ids)) == len(ids)  # every message kept its own sms_id
    assert max(sms.counter.values()) == 1


async def test_batches_are_capped_and_never_mix_callers(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    a = await make_caller(sf, keys, "svc-a")
    b = await make_caller(sf, keys, "svc-b")
    from_a = {m.id for m in await submit(repo, a, 5)}
    from_b = {m.id for m in await submit(repo, b, 2, first=20)}
    sms = BatchSms(clock)
    w = make_worker(repo, engine, clock, sms=sms, batch_max_recipients=3)
    await w.run_once()
    assert sorted(len(x) for x in sms.batches) == [2, 2, 3]
    for x in sms.batches:
        ids = {r.message_id for r in x}
        assert ids <= from_a or ids <= from_b


async def test_a_batch_counts_numbers_times_parts_against_the_pace(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    await submit(repo, ctx, 5, text=TWO)  # 5 numbers x 2 parts = 10 units
    later = await submit(repo, ctx, 1, text=ONE, first=40, at_=T0 + timedelta(milliseconds=1))
    sms = BatchSms(clock)
    w = make_worker(
        repo,
        engine,
        clock,
        sms=sms,
        sleep=no_time,
        sms_limit=SMS_8028_DEFAULT,
        batch_max_recipients=100,
    )
    await w.run_once()
    assert [len(x) for x in sms.batches] == [5]
    assert sms.counter[later[0].id] == 0
    async with sf() as s:
        units = (await s.execute(select(message_attempts.c.units))).scalars().all()
    assert sorted(units) == [2] * 5
    # The batch used 10 units: 3.33 s of the non-OTP share. Not before that.
    clock.advance(seconds=3)
    await w.run_once()
    assert sms.counter[later[0].id] == 0
    clock.advance(milliseconds=400)
    await w.run_once()
    assert sms.counter[later[0].id] == 1


async def test_batch_timeout_makes_every_message_unknown_and_none_is_resent(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    bulk = await submit(repo, ctx, 4)

    def timeout(requests: Sequence[SmsSendRequest], now: datetime) -> list[ProviderResult]:
        return [
            outcome(Provider.SMS_8028, now, ProviderOutcome.UNKNOWN, FailureCode.TIMEOUT, "timeout")
        ] * len(requests)

    sms = BatchSms(clock, timeout)
    w = make_worker(repo, engine, clock, sms=sms, batch_max_recipients=100)
    await w.run_once()
    for _ in range(3):
        clock.advance(minutes=30)
        await w.run_once()
    state = await rows(sf)
    assert {state[m.id]["status"] for m in bulk} == {"unknown"}
    assert len(sms.batches) == 1 and max(sms.counter.values()) == 1


async def test_batch_worker_deadline_makes_every_message_unknown(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    bulk = await submit(repo, ctx, 3)

    class Hanging(BatchSms):
        async def send_batch(self, requests: Sequence[SmsSendRequest]) -> list[ProviderResult]:
            self.batches.append(list(requests))
            await asyncio.sleep(10)
            raise AssertionError("not reached")

    sms = Hanging(clock)
    w = make_worker(
        repo, engine, clock, sms=sms, batch_max_recipients=100, lease=timedelta(seconds=31)
    )
    await w.run_once()
    state = await rows(sf)
    assert {state[m.id]["status"] for m in bulk} == {"unknown"}


async def test_batch_500_retries_every_message(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    bulk = await submit(repo, ctx, 3)
    answers = iter(["500", "ok"])

    def script(requests: Sequence[SmsSendRequest], now: datetime) -> list[ProviderResult]:
        if next(answers) == "500":
            return [
                outcome(
                    Provider.SMS_8028,
                    now,
                    ProviderOutcome.NOT_SENT,
                    FailureCode.PROVIDER_SERVER_ERROR,
                    "http_500",
                    retryable=True,
                )
            ] * len(requests)
        return [sent(Provider.SMS_8028, now, f"id-{r.message_id}") for r in requests]

    sms = BatchSms(clock, script)
    w = make_worker(repo, engine, clock, sms=sms, batch_max_recipients=100)
    await w.run_once()
    assert {(await rows(sf))[m.id]["status"] for m in bulk} == {"accepted"}
    clock.advance(seconds=31)  # first backoff step
    await w.run_once()
    assert {(await rows(sf))[m.id]["status"] for m in bulk} == {"sent"}
    assert [len(b) for b in sms.batches] == [3, 3]


async def test_batch_413_is_retried_one_message_at_a_time(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    bulk = await submit(repo, ctx, 3)

    def too_large(requests: Sequence[SmsSendRequest], now: datetime) -> list[ProviderResult]:
        return [
            ProviderResult(
                outcome=ProviderOutcome.NOT_SENT,
                provider=Provider.SMS_8028,
                provider_status="http_413",
                failure_code=FailureCode.TEXT_REJECTED,
                retryable=True,
                started_at=now,
                finished_at=now,
                detail=BATCH_TOO_LARGE,
            )
        ] * len(requests)

    sms = BatchSms(clock, too_large)
    w = make_worker(repo, engine, clock, sms=sms, batch_max_recipients=100)
    await w.run_once()
    clock.advance(seconds=31)
    await w.run_once()
    assert len(sms.batches) == 1
    assert sorted(c.message_id for c in sms.calls) == sorted(m.id for m in bulk)
    assert {(await rows(sf))[m.id]["status"] for m in bulk} == {"sent"}


async def test_otp_is_never_batched_even_when_identical(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    otps = await submit(repo, ctx, 4, purpose="otp", text="Your OAN code is 482913.")
    sms = BatchSms(clock)
    w = make_worker(repo, engine, clock, sms=sms, batch_max_recipients=100)
    await w.run_once()
    assert sms.batches == []
    assert sorted(c.message_id for c in sms.calls) == sorted(m.id for m in otps)


def test_batch_size_setting_is_bounded() -> None:
    from oan_messaging.worker import WorkerConfig

    assert WorkerConfig().batch_max_recipients == 1
    WorkerConfig(batch_max_recipients=100)
    for bad in (0, 101):
        try:
            WorkerConfig(batch_max_recipients=bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_default_rate_limit_is_unchanged_in_value() -> None:
    assert RateLimit(requests=4, otp_reserve=1) == SMS_8028_DEFAULT


async def test_begin_batch_one_claim_that_cannot_begin_never_stops_the_others(
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
) -> None:
    from sqlalchemy import update

    from oan_messaging.contracts import LeaseLost

    ctx = await make_caller(sf, keys)
    await submit(repo, ctx, 3)
    claims = await repo.claim_due("w", T0, timedelta(minutes=2), 3)
    async with sf() as s, s.begin():  # another worker took the middle one over
        await s.execute(
            update(messages)
            .where(messages.c.id == claims[1].message.id)
            .values(lease_token=None, lease_owner=None, lease_expires_at=None)
        )
    out = await repo.begin_batch(claims, T0)
    assert out[0] == 1 and out[2] == 1 and isinstance(out[1], LeaseLost)
    state = await rows(sf)
    assert [state[c.message.id]["status"] for c in claims] == ["sending", "accepted", "sending"]
