"""Provider rate limits shared by every worker process (MSG-05, restructured in MSG-07b).

The limiter counts the durable attempt rows that the storage layer writes
(``message_attempts.started_at``, joined to ``messages.channel``) and never writes itself.

Reservation inside the attempt transaction (MSG-07b, review finding F-03 / design D1). The limiter
passes a reservation hook to ``repository.begin_attempt``. Inside that ONE transaction, on
the repository's own pooled connection:

1. the provider scope and caller rows are locked (``FOR SHARE``);
2. ``pg_try_advisory_xact_lock(RATE_LOCK_CLASS, provider)`` is taken without waiting
   (busy: the transaction rolls back, nothing is held while the task sleeps and retries);
3. the monotonic mark is taken, then the shared time is sampled and recent starts counted;
4. the message becomes ``sending`` and the attempt row is inserted in one statement;
5. COMMIT releases the transaction lock.

No second engine, no per-attempt connection set-up and no lock-release round trips. The
transaction lock cannot outlive the transaction, even on cancellation, so a cancelled or
broken reservation can never leave the provider locked.

Deadline: ``deadline - now`` becomes one monotonic budget that bounds the whole call,
including retry sleeps. If it runs out before COMMIT was issued the result is
``DeadlineReached`` (provably nothing began). If it runs out after COMMIT was issued the
result is ``BeginInterrupted``: commit uncertain, so there is no provider call and no
finish; recovery settles it safely.

Time: one authoritative domain for everyone.

* The shared bound is evaluated with ``time_source``, sampled **after** the provider lock
  is held. The default is the database clock (``database_time``). Simulated time must be
  injected explicitly (``clock_time_source``) and shared by every worker in a test. A
  per-host wall clock is never used for the bound.
* ``effective = max(sampled, latest committed start)`` never moves backwards: if a
  committed start is ahead of the sampled time (a backward clock step), nothing starts
  until the clock passes it.

Pace (a true rate, counted per SMS part; replaces the count of requests over
``window + 2 x dispatch_tolerance``, which gave 8028 about 2.1 broadcast SMS a second):

* Each attempt row has ``units``: its SMS parts (a 3-part SMS is 3), Telegram 1. A
  batched 8028 request is one start of ``numbers x parts`` units (one row per number).
* Window: the units started in the last ``window`` plus the new start must not exceed
  ``requests`` for an OTP, or ``requests - otp_reserve`` for any other purpose, so one
  slot of each window is always left for OTP.
* A start bigger than that (a batch) is allowed only into an empty window and leaves pace
  debt on its attempt rows (``pace_total_until``, ``pace_bulk_until``, migration 0006):
  nothing starts until ``units x window / requests`` has passed (OTP), or ``units x
  window / (requests - otp_reserve)`` (other purposes). An OTP can wait that long behind a
  batch, which is why batching is off by default.
* Everything is read and written under the same transaction lock, so it holds across
  workers.

Boundaries (remediation plan §3.4):

* **Enforced:** started units per provider never exceed ``requests`` in any ``window``,
  except one batch, which is then paid back before anything else starts; non-OTP units
  never exceed ``requests - otp_reserve`` in any ``window``; one adapter call per committed attempt
  and none on an uncertain commit; the worker's dispatch decision is within
  ``dispatch_tolerance`` of the monotonic mark.
* **Not enforced:** the scheduling gap between the worker's final check and adapter
  invocation, and network delay to the provider. There is no provider-arrival guarantee.
  Deployments measure dispatch latency (``measure_dispatch_latency``), keep database time
  synchronized and alert on late starts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from oan_messaging.contracts import Channel, Claim, Purpose
from oan_messaging.storage.repository import (
    BeginProgress,
    PostgresMessageRepository,
    Reservation,
    ReserveHook,
    attempt_units,
)

log = logging.getLogger(__name__)

#: Advisory lock class for provider rate limiting ("OANL"); second key = provider id.
RATE_LOCK_CLASS = 0x4F414E4C
_PROVIDER_KEY = {Channel.SMS: 1, Channel.TELEGRAM: 2}

TimeSource = Callable[[AsyncConnection], Awaitable[datetime]]

#: Selected by the reference-latency benchmark (tests/worker/test_dispatch_latency.py).
DEFAULT_DISPATCH_TOLERANCE = timedelta(milliseconds=200)
T = TypeVar("T")


async def database_time(conn: AsyncConnection) -> datetime:
    """Authoritative shared time: the database server's clock (production default).
    Runs inside the caller's transaction and never commits it."""
    value = (await conn.execute(text("SELECT clock_timestamp()"))).scalar_one()
    assert isinstance(value, datetime)
    return value


#: How far back a batch's pace debt is read. A batch moves the ledger at most 100
#: numbers x 3 parts ahead at the non-OTP rate (100 s at 3 a second); an hour covers it.
LEDGER_LOOKBACK = timedelta(hours=1)

#: Starts and their units inside the window (same order), the latest batch ledgers (every
#: purpose, non-OTP), with database time sampled in the same round trip...
_SAMPLE_AND_COUNT = text(
    "WITH t AS MATERIALIZED (SELECT clock_timestamp() AS now) SELECT t.now, "
    "ARRAY(SELECT a.started_at FROM message_attempts a"
    " JOIN messages m ON m.id = a.message_id"
    " WHERE m.channel = :ch AND a.started_at > t.now - :span"
    " ORDER BY a.started_at), "
    "ARRAY(SELECT a.units FROM message_attempts a"
    " JOIN messages m ON m.id = a.message_id"
    " WHERE m.channel = :ch AND a.started_at > t.now - :span"
    " ORDER BY a.started_at), "
    "(SELECT a.pace_total_until"
    " FROM message_attempts a JOIN messages m ON m.id = a.message_id"
    " WHERE m.channel = :ch AND a.started_at > t.now - :lookback"
    " AND a.pace_total_until IS NOT NULL"
    " ORDER BY a.started_at DESC LIMIT 1), "
    "(SELECT a.pace_bulk_until"
    " FROM message_attempts a JOIN messages m ON m.id = a.message_id"
    " WHERE m.channel = :ch AND a.started_at > t.now - :lookback"
    " AND a.pace_bulk_until IS NOT NULL AND m.purpose <> 'otp'"
    " ORDER BY a.started_at DESC LIMIT 1)"
    " FROM t"
)
#: ...or relative to an injected time source (tests and simulations).
_COUNT_AT = text(
    "SELECT "
    "ARRAY(SELECT a.started_at FROM message_attempts a"
    " JOIN messages m ON m.id = a.message_id"
    " WHERE m.channel = :ch AND a.started_at > CAST(:now AS timestamptz) - :span"
    " ORDER BY a.started_at), "
    "ARRAY(SELECT a.units FROM message_attempts a"
    " JOIN messages m ON m.id = a.message_id"
    " WHERE m.channel = :ch AND a.started_at > CAST(:now AS timestamptz) - :span"
    " ORDER BY a.started_at), "
    "(SELECT a.pace_total_until"
    " FROM message_attempts a JOIN messages m ON m.id = a.message_id"
    " WHERE m.channel = :ch AND a.started_at > CAST(:now AS timestamptz) - :lookback"
    " AND a.pace_total_until IS NOT NULL"
    " ORDER BY a.started_at DESC LIMIT 1), "
    "(SELECT a.pace_bulk_until"
    " FROM message_attempts a JOIN messages m ON m.id = a.message_id"
    " WHERE m.channel = :ch AND a.started_at > CAST(:now AS timestamptz) - :lookback"
    " AND a.pace_bulk_until IS NOT NULL AND m.purpose <> 'otp'"
    " ORDER BY a.started_at DESC LIMIT 1)"
)


@dataclass(frozen=True)
class RateLimit:
    """At most ``requests`` provider calls per ``window`` across all workers."""

    requests: int
    window: timedelta = timedelta(seconds=1)
    #: Slots per window that only OTP may use.
    otp_reserve: int = 0

    def __post_init__(self) -> None:
        if self.requests <= 0 or self.window <= timedelta(0):
            raise ValueError("rate limit must be positive")
        if not 0 <= self.otp_reserve < self.requests:
            raise ValueError("otp_reserve must be in [0, requests)")

    @property
    def unit_time(self) -> timedelta:
        """Ledger step of one unit, every purpose: ``window / requests``."""
        return self.window / self.requests

    @property
    def bulk_unit_time(self) -> timedelta:
        """Ledger step of one non-OTP unit: ``window / (requests - otp_reserve)``."""
        return self.window / (self.requests - self.otp_reserve)


#: 8028 (20 Nov 2026): no API rate limit; everything is queued at 8028, and the operator
#: sends 2-5 SMS a second. Sending faster only moves the queue into 8028, where our order
#: (OTP first, SMS-03 ageing) no longer applies, so we pace to the operator: a true 4 SMS
#: a second, counted per SMS part (a 3-part SMS is 3, a batch is numbers x parts), one
#: unit per second kept for OTP. Settings OAN_MSG_SMS_RATE_PER_SECOND and
#: OAN_MSG_SMS_OTP_RESERVE override it.
SMS_8028_DEFAULT = RateLimit(requests=4, window=timedelta(seconds=1), otp_reserve=1)
#: Telegram Bot API guidance is about 30 messages/second per bot; stay below it.
TELEGRAM_DEFAULT = RateLimit(requests=25, window=timedelta(seconds=1), otp_reserve=2)


@dataclass(frozen=True)
class Begun:
    attempt: int
    #: Effective (shared-domain) time the attempt was started at.
    begin_time: datetime
    #: Limiter monotonic reading when the shared time was sampled. The provider call must
    #: start within ``dispatch_tolerance`` of it, measured on the same monotonic clock,
    #: so wall-clock adjustments cannot hide a delayed start.
    mono_mark: float


@dataclass(frozen=True)
class BatchBegun:
    """One batched request began. ``outcomes`` has one entry per claim, in order: the
    attempt number, or why that claim did not begin (``LeaseLost``, ``MessageExpired``,
    ``DispatchNotAuthorized``)."""

    outcomes: list[int | Exception]
    begin_time: datetime
    mono_mark: float


@dataclass(frozen=True)
class _Started:
    value: object
    at: datetime
    mark: float


@dataclass(frozen=True)
class RateLimited:
    #: Earliest effective time a slot frees up for this purpose.
    retry_at: datetime
    #: The same, as a duration from the sampled effective time.
    wait: timedelta


@dataclass(frozen=True)
class DeadlineReached:
    """The deadline budget ran out before COMMIT was issued. Nothing began."""


@dataclass(frozen=True)
class BeginInterrupted:
    """The deadline budget ran out after ``begin_attempt`` issued COMMIT. Its commit is
    uncertain. The caller must NOT call the provider and must NOT finish the attempt.
    If nothing was committed, the claim lapses and is re-claimable (provably unsent). If
    ``sending`` was committed, ``recover_stale`` marks it unknown and it is never resent."""


class _BudgetExhausted(Exception):
    pass


class _LockBusy(Exception):
    pass


class _Deferred(Exception):
    def __init__(self, result: RateLimited) -> None:
        super().__init__("rate limited")
        self.result = result


def clock_time_source(clock: Callable[[], datetime]) -> TimeSource:
    """Explicitly injected simulated time, for tests and simulations only. Every limiter
    that shares a database must use the same domain; production uses ``database_time``."""

    async def source(_conn: AsyncConnection) -> datetime:
        return clock()

    return source


class SharedRateLimiter:
    """Cross-process limiter: rate reservation inside the attempt transaction.

    The shared bound is evaluated in ONE time domain for every worker. The default
    ``time_source`` is the database clock (``database_time``). All workers and limiters of
    a deployment must use the same source; a per-host wall clock is never used.

    ``database`` is accepted for call-site compatibility and is not used: coordination
    runs on the repository's own transaction. ``max_concurrent_reservations`` bounds how
    many reservation transactions one limiter runs at once (each uses one repository
    connection for its duration).
    """

    def __init__(
        self,
        database: object | None = None,
        limits: dict[Channel, RateLimit] | None = None,
        *,
        time_source: TimeSource = database_time,
        dispatch_tolerance: timedelta = DEFAULT_DISPATCH_TOLERANCE,
        lock_retry_interval: timedelta = timedelta(milliseconds=10),
        max_concurrent_reservations: int = 2,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        del database
        if dispatch_tolerance <= timedelta(0) or lock_retry_interval <= timedelta(0):
            raise ValueError("dispatch_tolerance and lock_retry_interval must be positive")
        if max_concurrent_reservations <= 0:
            raise ValueError("max_concurrent_reservations must be positive")
        self._limits = limits or {Channel.SMS: SMS_8028_DEFAULT, Channel.TELEGRAM: TELEGRAM_DEFAULT}
        self._time_source = time_source
        self.dispatch_tolerance = dispatch_tolerance
        self._retry = lock_retry_interval
        self._slots = asyncio.Semaphore(max_concurrent_reservations)
        self.monotonic = monotonic
        self._sleep = sleep

    def limit_for(self, channel: Channel) -> RateLimit:
        return self._limits[channel]

    async def aclose(self) -> None:
        """Nothing to release: the limiter owns no connections."""

    async def begin_within_limit(
        self,
        repo: PostgresMessageRepository,
        claim: Claim,
        now: datetime,
        *,
        deadline: datetime | None = None,
    ) -> Begun | RateLimited | DeadlineReached | BeginInterrupted:
        """Start the attempt if the provider has capacity at the shared effective time.

        ``deadline - now`` (both caller clock) becomes a monotonic budget that bounds the
        WHOLE call: semaphore admission, the reservation transaction and retry sleeps. The
        rate decision itself uses only the shared ``time_source``. Exceptions from
        ``begin_attempt`` (``LeaseLost``, ``MessageExpired``, ``DispatchNotAuthorized``)
        propagate unchanged; the transaction lock never outlives the transaction.
        """
        units = attempt_units(claim.send_request)
        otp = claim.message.purpose is Purpose.OTP

        async def begin(reserve: ReserveHook, progress: BeginProgress) -> int:
            return await repo.begin_attempt(
                claim, now, reserve=reserve, progress=progress, units=units
            )

        outcome = await self._reserve_and_begin(
            claim.message.channel, units, otp, now, deadline, begin, claim.message.id
        )
        if isinstance(outcome, _Started):
            assert isinstance(outcome.value, int)
            return Begun(attempt=outcome.value, begin_time=outcome.at, mono_mark=outcome.mark)
        return outcome

    async def begin_batch_within_limit(
        self,
        repo: PostgresMessageRepository,
        claims: list[Claim],
        now: datetime,
        *,
        deadline: datetime | None = None,
    ) -> BatchBegun | RateLimited | DeadlineReached | BeginInterrupted:
        """``begin_within_limit`` for one batched provider request: ONE reservation of
        ``numbers x parts`` units (never OTP), then ``repository.begin_batch``. Per-claim
        reasons for not beginning come back in ``BatchBegun.outcomes``."""
        if not claims:
            raise ValueError("a batch needs at least one claim")
        if any(c.message.purpose is Purpose.OTP for c in claims):
            raise ValueError("OTPs are never batched")
        units = sum(attempt_units(c.send_request) for c in claims)

        async def begin(reserve: ReserveHook, progress: BeginProgress) -> list[int | Exception]:
            return await repo.begin_batch(claims, now, reserve=reserve, progress=progress)

        outcome = await self._reserve_and_begin(
            claims[0].message.channel, units, False, now, deadline, begin, claims[0].message.id
        )
        if isinstance(outcome, _Started):
            assert isinstance(outcome.value, list)
            return BatchBegun(outcomes=outcome.value, begin_time=outcome.at, mono_mark=outcome.mark)
        return outcome

    async def _reserve_and_begin(
        self,
        channel: Channel,
        units: int,
        otp: bool,
        now: datetime,
        deadline: datetime | None,
        begin: Callable[[ReserveHook, BeginProgress], Awaitable[Any]],
        log_id: object,
    ) -> _Started | RateLimited | DeadlineReached | BeginInterrupted:
        limit = self._limits[channel]
        key = _PROVIDER_KEY[channel]
        mono_end = (
            None
            if deadline is None
            else self.monotonic() + max((deadline - now).total_seconds(), 0.0)
        )

        def remaining() -> float | None:
            return None if mono_end is None else mono_end - self.monotonic()

        async def bounded(aw: Awaitable[T]) -> T:
            left = remaining()
            if left is None:
                return await aw
            if left <= 0:
                if asyncio.iscoroutine(aw):
                    aw.close()
                raise _BudgetExhausted
            try:
                return await asyncio.wait_for(aw, left)
            except TimeoutError:
                raise _BudgetExhausted from None

        while True:
            try:
                await bounded(self._slots.acquire())
            except _BudgetExhausted:
                return DeadlineReached()
            try:
                outcome = await self._try_once(
                    channel, limit, units, otp, key, bounded, begin, log_id
                )
            except _BudgetExhausted:
                return DeadlineReached()
            finally:
                self._slots.release()
            if outcome is not None:
                return outcome
            # Lock busy: the transaction was rolled back; nothing is held while waiting.
            left = remaining()
            if left is not None and left <= 0:
                return DeadlineReached()
            wait = self._retry.total_seconds()
            await self._sleep(wait if left is None else min(wait, left))

    async def _read_window(self, conn: AsyncConnection, channel: Channel, span: timedelta) -> tuple[
        datetime, list[tuple[datetime, int]], datetime | None, datetime | None
    ]:
        """(sampled shared time, [(start, units)] inside the window, total ledger, non-OTP
        ledger)."""
        params: dict[str, Any] = {"ch": channel.value, "span": span, "lookback": LEDGER_LOOKBACK}
        if self._time_source is database_time:
            row = (await conn.execute(_SAMPLE_AND_COUNT, params)).one()
            sampled, rest = row[0], row[1:]
        else:
            sampled = await self._time_source(conn)
            rest = (await conn.execute(_COUNT_AT, params | {"now": sampled})).one()
        starts = list(zip(rest[0] or [], rest[1] or [], strict=True))
        return sampled, starts, rest[2], rest[3]

    async def _try_once(
        self,
        channel: Channel,
        limit: RateLimit,
        units: int,
        otp: bool,
        key: int,
        bounded: Callable[[Awaitable[Any]], Awaitable[Any]],
        begin: Callable[[ReserveHook, BeginProgress], Awaitable[Any]],
        log_id: object,
    ) -> _Started | RateLimited | BeginInterrupted | None:
        progress = BeginProgress()
        mark: list[float] = []
        chosen: list[datetime] = []

        async def reserve(conn: AsyncConnection) -> Reservation:
            got = (
                await conn.execute(
                    text("SELECT pg_try_advisory_xact_lock(:c, :k)"),
                    {"c": RATE_LOCK_CLASS, "k": key},
                )
            ).scalar_one()
            if not got:
                raise _LockBusy
            mark.append(self.monotonic())
            # Fresh shared time, sampled only while the provider lock is held. The ledger
            # is read in a LATER statement than the lock, so under READ COMMITTED (the
            # repository default) its snapshot includes every start committed before the
            # lock was granted.
            sampled, starts, total_prev, bulk_prev = await self._read_window(
                conn, channel, limit.window
            )
            effective = max([sampled, *(start for start, _ in starts)])
            if effective > sampled:
                # A start recorded ahead of the shared time (backward clock step): never
                # start "early" relative to it.
                raise _Deferred(RateLimited(retry_at=effective, wait=effective - sampled))
            decision = pace(limit, effective, units, otp, starts, total_prev, bulk_prev)
            if isinstance(decision, RateLimited):
                raise _Deferred(decision)
            chosen.append(effective)
            return decision

        try:
            value = await bounded(begin(reserve, progress))
        except _LockBusy:
            return None
        except _Deferred as deferred:
            return deferred.result
        except _BudgetExhausted:
            if not progress.commit_sent:
                raise  # nothing committed: DeadlineReached
            log.warning(
                "begin_attempt exceeded its deadline; outcome left to recovery (message_id=%s)",
                log_id,
            )
            return BeginInterrupted()
        return _Started(value=value, at=chosen[-1], mark=mark[-1])


def pace(
    limit: RateLimit,
    effective: datetime,
    units: int,
    otp: bool,
    starts: list[tuple[datetime, int]],
    total_prev: datetime | None,
    bulk_prev: datetime | None,
) -> Reservation | RateLimited:
    """The pace decision for ``units`` SMS parts starting at ``effective``.

    * Window: the units started in the last ``window`` plus ``units`` must not exceed the
      cap: ``requests`` for an OTP, ``requests - otp_reserve`` otherwise.
    * A start bigger than the cap (a batch) is allowed only into an empty window. It then
      leaves pace debt: the total ledger moves to ``effective + units x window /
      requests`` and the non-OTP ledger to ``effective + units x window / (requests -
      otp_reserve)``.
    * Nothing starts before the ledger it is bound by: OTP the total ledger, non-OTP both.

    A refusal says when the start would be allowed."""
    if units <= 0:
        raise ValueError("units must be positive")
    cap = limit.requests if otp else limit.requests - limit.otp_reserve
    recent = [(start, used) for start, used in starts if start > effective - limit.window]
    retry_at = effective
    # Pace debt left by an earlier batch.
    for ledger in (total_prev,) if otp else (total_prev, bulk_prev):
        if ledger is not None and ledger > retry_at:
            retry_at = ledger
    # Window: free the oldest starts until this one fits (an oversized one needs it empty).
    need = min(units, cap)
    used = sum(u for _, u in recent)
    for start, u in recent:
        if used + need <= cap:
            break
        used -= u
        retry_at = max(retry_at, start + limit.window)
    if retry_at > effective:
        return RateLimited(
            retry_at=retry_at, wait=max(retry_at - effective, timedelta(milliseconds=1))
        )
    if units <= cap:
        return Reservation(at=effective)
    return Reservation(
        at=effective,
        pace_total_until=effective + units * limit.unit_time,
        pace_bulk_until=effective + units * limit.bulk_unit_time,
    )


@dataclass(frozen=True)
class DispatchLatency:
    """Start-up measurement of the repository round trip (remediation plan §3.3)."""

    p95: timedelta
    estimate: timedelta
    tolerance: timedelta

    @property
    def fits(self) -> bool:
        return self.estimate <= self.tolerance / 2


async def measure_dispatch_latency(
    connect: Callable[[], Any],
    tolerance: timedelta,
    *,
    samples: int = 20,
    monotonic: Callable[[], float] = time.monotonic,
) -> DispatchLatency:
    """Time ``samples`` round trips on one repository connection. The reservation path
    after the monotonic mark costs three round trips (sample and count, state change,
    COMMIT) plus client overhead, so the estimate is ``4 x p95 + 5 ms``; a worker must
    refuse to start unless it fits in ``tolerance / 2``.
    ``connect`` is an async context manager factory (``engine.connect``)."""
    if samples < 5:
        raise ValueError("at least 5 samples are required")
    timings: list[float] = []
    async with connect() as conn:
        await conn.execute(text("SELECT 1"))  # warm the connection
        for _ in range(samples):
            start = monotonic()
            await conn.execute(text("SELECT 1"))
            timings.append(monotonic() - start)
    timings.sort()
    p95 = timedelta(seconds=timings[min(len(timings) - 1, int(0.95 * len(timings)))])
    estimate = 4 * p95 + timedelta(milliseconds=5)
    return DispatchLatency(p95=p95, estimate=estimate, tolerance=tolerance)
