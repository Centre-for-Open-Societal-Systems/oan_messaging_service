"""Contract 3.4.0 on a real PostgreSQL: the Telegram-only broadcast switch at the worker
claim, claim order on Telegram, reply buttons through storage, and the cancel route's
storage rule (one conditional update that cannot race a claim)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tgsupport import SHARE, has_content, raw_content, row, submit_tg
from wsupport import (
    T0,
    FakeClock,
    FakeSms,
    FakeTelegram,
    Keys,
    make_caller,
    make_worker,
    submit_broadcast,
    submit_sms,
)

from oan_messaging.contracts import (
    Channel,
    FailureCode,
    IdempotencyConflict,
    IdempotencyDecision,
    LeaseLost,
    MessageStatus,
    TelegramReplyKeyboard,
    TelegramSendRequest,
    TelegramSubmission,
)
from oan_messaging.stats.snapshot import refresh
from oan_messaging.storage.repository import CancelOutcome, PostgresMessageRepository
from oan_messaging.storage.schema import stats_failures

LEASE = timedelta(minutes=2)

# --------------------------------------------------------------- broadcast switch (claim)


async def test_telegram_switch_claims_telegram_broadcasts_and_never_sms(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keys,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    sms_b = await submit_broadcast(repo, ctx, 1)
    tg_b = await submit_tg(repo, ctx)
    sms, tg = FakeSms(clock), FakeTelegram(clock)
    worker = make_worker(
        repo, engine, clock, sms=sms, tg=tg,
        broadcast_enabled=False, telegram_broadcast_enabled=True,
    )
    await worker.run_once()
    assert tg.counter[tg_b.id] == 1
    assert sms.counter[sms_b.id] == 0
    assert (await row(sf, sms_b.id))["status"] == "accepted"
    # The same switch on an SMS-only worker claims nothing.
    clock.advance(seconds=5)
    sms_only = make_worker(
        repo, engine, clock, sms=FakeSms(clock), worker_id="w-sms",
        broadcast_enabled=False, telegram_broadcast_enabled=True,
    )
    assert (await sms_only.run_once()).claimed == 0
    assert (await row(sf, sms_b.id))["status"] == "accepted"


@pytest.mark.parametrize(
    ("both", "tg_only", "sms_sent", "tg_sent"),
    [(True, False, 1, 1), (True, True, 1, 1), (False, False, 0, 0)],
    ids=["old-switch-opens-both", "both-switches", "both-off"],
)
async def test_old_switch_behaves_as_before(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keys,
    clock: FakeClock,
    both: bool,
    tg_only: bool,
    sms_sent: int,
    tg_sent: int,
) -> None:
    ctx = await make_caller(sf, keys)
    sms_b = await submit_broadcast(repo, ctx, 1)
    tg_b = await submit_tg(repo, ctx)
    sms, tg = FakeSms(clock), FakeTelegram(clock)
    worker = make_worker(
        repo, engine, clock, sms=sms, tg=tg,
        broadcast_enabled=both, telegram_broadcast_enabled=tg_only,
    )
    await worker.run_once()
    assert (sms.counter[sms_b.id], tg.counter[tg_b.id]) == (sms_sent, tg_sent)


async def test_sms_worker_claim_filter_is_the_old_statement(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keys,
    clock: FakeClock,
) -> None:
    """With the Telegram switch off, the worker passes the old boolean to the claim."""
    for both in (True, False):
        w = make_worker(repo, engine, clock, sms=FakeSms(clock), broadcast_enabled=both)
        assert w._allow_broadcast() is both
    w = make_worker(
        repo, engine, clock, sms=FakeSms(clock),
        broadcast_enabled=False, telegram_broadcast_enabled=True,
    )
    assert w._allow_broadcast() == frozenset({Channel.TELEGRAM})


async def test_each_worker_claims_only_its_own_channel(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keys,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    sms_msgs = [
        await submit_sms(repo, ctx, recipient="0900000101"),
        await submit_broadcast(repo, ctx, 102),
    ]
    tg_msgs = [
        await submit_tg(repo, ctx, purpose="transactional", chat_id=555000201),
        await submit_tg(repo, ctx, chat_id=555000202),
    ]
    sms, tg = FakeSms(clock), FakeTelegram(clock)
    sms_worker = make_worker(repo, engine, clock, sms=sms, worker_id="w-sms")
    tg_worker = make_worker(
        repo, engine, clock, tg=tg, worker_id="w-tg",
        broadcast_enabled=False, telegram_broadcast_enabled=True,
    )
    await asyncio.gather(sms_worker.run_once(), tg_worker.run_once())
    assert {c.message_id for c in sms.calls} == {m.id for m in sms_msgs}
    assert {c.message_id for c in tg.calls} == {m.id for m in tg_msgs}
    assert all(c.channel is Channel.SMS for c in sms.calls)
    assert all(c.channel is Channel.TELEGRAM for c in tg.calls)


# ------------------------------------------------------------------------- claim order


async def test_telegram_reply_is_claimed_before_waiting_broadcasts_even_aged(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keys,
    clock: FakeClock,
) -> None:
    """A bot reply (transactional) goes before broadcasts that have waited
    longer than the SMS-03 ageing time (180 s by default)."""
    ctx = await make_caller(sf, keys)
    broadcasts = [
        await submit_tg(repo, ctx, chat_id=555000300 + i, at=clock.advance(seconds=1))
        for i in range(10)
    ]
    reply_at = clock.advance(minutes=10)  # every broadcast is now "aged"
    reply = await submit_tg(repo, ctx, purpose="transactional", at=reply_at, chat_id=555000399)
    claims = await repo.claim_due(
        "w-tg", reply_at, LEASE, 3, channels=(Channel.TELEGRAM,),
        allow_broadcast={Channel.TELEGRAM}, aged_after=timedelta(seconds=180),
    )
    assert claims[0].message.id == reply.id
    assert [c.message.id for c in claims[1:]] == [b.id for b in broadcasts[:2]]


async def test_sms_ageing_is_unchanged(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    clock: FakeClock,
) -> None:
    """SMS-03 still lets an aged SMS broadcast go ahead of a newer SMS transactional."""
    ctx = await make_caller(sf, keys)
    old = await submit_broadcast(repo, ctx, 1)
    now = clock.advance(minutes=10)
    newer = await submit_sms(
        repo, ctx, at=now, recipient="0900000002", purpose="transactional", expires_in=None
    )
    claims = await repo.claim_due(
        "w-sms", now, LEASE, 2, channels=(Channel.SMS,), allow_broadcast=True,
        aged_after=timedelta(seconds=180),
    )
    assert [c.message.id for c in claims] == [old.id, newer.id]


# ----------------------------------------------------------------------- reply buttons


async def test_buttons_are_stored_encrypted_claimed_and_purged_with_the_text(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keys,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    msg = await submit_tg(repo, ctx, purpose="transactional", reply_markup=SHARE)
    assert b"Share my phone" not in await raw_content(sf, msg.id)  # encrypted at rest
    tg = FakeTelegram(clock)
    await make_worker(repo, engine, clock, tg=tg).run_once()
    request = tg.calls[0]
    assert isinstance(request, TelegramSendRequest)
    assert request.reply_markup == TelegramReplyKeyboard.model_validate(SHARE)
    stored = await row(sf, msg.id)
    assert stored["status"] == "sent" and stored["content_purged_at"] is not None
    assert not await has_content(sf, msg.id)  # buttons went with the text


async def test_message_without_buttons_is_claimed_without_reply_markup(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keys,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    await submit_tg(repo, ctx, purpose="transactional")
    tg = FakeTelegram(clock)
    await make_worker(repo, engine, clock, tg=tg).run_once()
    assert isinstance(tg.calls[0], TelegramSendRequest) and tg.calls[0].reply_markup is None


async def test_buttons_are_part_of_the_idempotency_fingerprint(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
) -> None:
    ctx = await make_caller(sf, keys)
    key = str(uuid.uuid4())
    base = {"chat_id": 555000111, "text": "Link?", "source_reference": "bot:c-1:1",
            "purpose": "transactional"}
    first = TelegramSubmission.model_validate({**base, "reply_markup": SHARE})
    msg, decision = await repo.create_or_get(ctx, key, first, T0)
    assert decision is IdempotencyDecision.NEW
    # Same buttons, defaults written out: the same delivery (200).
    spelled = TelegramSubmission.model_validate(
        {**base, "reply_markup": {**SHARE, "one_time": True, "resize": True}}
    )
    again, decision = await repo.create_or_get(ctx, key, spelled, T0)
    assert decision is IdempotencyDecision.REPLAY and again.id == msg.id
    for other in (
        base,  # buttons dropped
        {**base, "reply_markup": {"remove_keyboard": True}},
        {**base, "reply_markup": {**SHARE, "one_time": False}},
        {**base, "reply_markup": {"keyboard": [[{"text": "Share my phone number"}]]}},
    ):
        with pytest.raises(IdempotencyConflict):
            await repo.create_or_get(ctx, key, TelegramSubmission.model_validate(other), T0)


# ------------------------------------------------------------------------------ cancel


async def test_cancel_waiting_message_purges_it_and_is_repeatable(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keys,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    msg = await submit_tg(repo, ctx, reply_markup=SHARE)
    now = clock.advance(seconds=3)
    outcome, view = await repo.cancel_for_caller(ctx.caller_id, msg.id, now)
    assert outcome is CancelOutcome.CANCELLED and view is not None
    assert view.status is MessageStatus.FAILED and view.failure_code is FailureCode.CANCELLED
    assert view.attempts == 0 and view.content_purged_at == now
    assert not await has_content(sf, msg.id)
    # Repeat: the same answer, nothing changes.
    clock.advance(seconds=3)
    outcome2, view2 = await repo.cancel_for_caller(ctx.caller_id, msg.id, clock())
    assert outcome2 is CancelOutcome.CANCELLED and view2 == view
    # A worker never sees it.
    tg = FakeTelegram(clock)
    assert (await make_worker(repo, engine, clock, tg=tg).run_once()).claimed == 0
    assert tg.calls == []


async def test_cancel_refusals(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    keys: Keys,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    other = await make_caller(sf, keys, "other-svc")
    sms_msg = await submit_sms(repo, ctx)
    tg_msg = await submit_tg(repo, ctx)
    now = clock()
    assert (await repo.cancel_for_caller(ctx.caller_id, sms_msg.id, now))[0] is (
        CancelOutcome.NOT_TELEGRAM
    )
    assert (await repo.cancel_for_caller(other.caller_id, tg_msg.id, now))[0] is (
        CancelOutcome.NOT_FOUND
    )
    assert (await repo.cancel_for_caller(ctx.caller_id, uuid.uuid4(), now))[0] is (
        CancelOutcome.NOT_FOUND
    )
    # Claimed (a live lease): not cancellable; the claim still sends.
    claims = await repo.claim_due("w", now, LEASE, 1, channels=(Channel.TELEGRAM,))
    assert [c.message.id for c in claims] == [tg_msg.id]
    assert (await repo.cancel_for_caller(ctx.caller_id, tg_msg.id, now))[0] is (
        CancelOutcome.NOT_CANCELLABLE
    )
    # Sending, then sent: not cancellable.
    await repo.begin_attempt(claims[0], now)
    assert (await repo.cancel_for_caller(ctx.caller_id, tg_msg.id, now))[0] is (
        CancelOutcome.NOT_CANCELLABLE
    )
    tg = FakeTelegram(clock)
    sent_msg = await submit_tg(repo, ctx, chat_id=555000777)
    await make_worker(repo, engine, clock, tg=tg).run_once()
    assert (await row(sf, sent_msg.id))["status"] == "sent"
    assert (await repo.cancel_for_caller(ctx.caller_id, sent_msg.id, clock()))[0] is (
        CancelOutcome.NOT_CANCELLABLE
    )
    # Past expires_at (expired or about to be): not cancellable.
    late = await submit_tg(
        repo, ctx, purpose="transactional", chat_id=555000778, at=clock(),
        expires_in=timedelta(minutes=1),
    )
    clock.advance(minutes=2)
    assert (await repo.cancel_for_caller(ctx.caller_id, late.id, clock()))[0] is (
        CancelOutcome.NOT_CANCELLABLE
    )
    await make_worker(repo, engine, clock, tg=FakeTelegram(clock)).run_once()
    assert (await row(sf, late.id))["status"] == "expired"
    assert (await repo.cancel_for_caller(ctx.caller_id, late.id, clock()))[0] is (
        CancelOutcome.NOT_CANCELLABLE
    )
    # A failed message (not cancelled) is not cancellable either.
    blocked = await submit_tg(repo, ctx, chat_id=555000779, at=clock())
    from wsupport import outcome as make_outcome  # local: only this case

    from oan_messaging.contracts import Provider, ProviderOutcome

    tg_fail = FakeTelegram(clock, lambda r, at: make_outcome(
        Provider.TELEGRAM, at, ProviderOutcome.REJECTED, FailureCode.RECIPIENT_BLOCKED_BOT,
        "tg_403"))
    await make_worker(repo, engine, clock, tg=tg_fail).run_once()
    assert (await row(sf, blocked.id))["status"] == "failed"
    assert (await repo.cancel_for_caller(ctx.caller_id, blocked.id, clock()))[0] is (
        CancelOutcome.NOT_CANCELLABLE
    )


async def test_cancel_after_an_abandoned_claim_lease_ran_out(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    clock: FakeClock,
) -> None:
    """A worker that claimed but never began (lease ran out) no longer holds the message:
    the cancel wins, and that worker can no longer begin an attempt."""
    ctx = await make_caller(sf, keys)
    msg = await submit_tg(repo, ctx)
    claims = await repo.claim_due("w", clock(), LEASE, 1, channels=(Channel.TELEGRAM,))
    later = clock.advance(minutes=3)
    assert (await repo.cancel_for_caller(ctx.caller_id, msg.id, later))[0] is (
        CancelOutcome.CANCELLED
    )
    with pytest.raises(LeaseLost):
        await repo.begin_attempt(claims[0], later)
    assert (await row(sf, msg.id))["attempts"] == 0


async def test_cancel_racing_a_claim_has_exactly_one_winner(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    wins = {"claim": 0, "cancel": 0}
    for i in range(40):
        msg = await submit_tg(repo, ctx, chat_id=555001000 + i)
        claims, (outcome, _) = await asyncio.gather(
            repo.claim_due("w", clock(), LEASE, 1, channels=(Channel.TELEGRAM,)),
            repo.cancel_for_caller(ctx.caller_id, msg.id, clock()),
        )
        claimed = [c.message.id for c in claims] == [msg.id]
        cancelled = outcome is CancelOutcome.CANCELLED
        assert claimed != cancelled, (i, claimed, outcome)
        state = await row(sf, msg.id)
        if cancelled:
            wins["cancel"] += 1
            assert state["status"] == "failed" and state["lease_token"] is None
        else:
            wins["claim"] += 1
            assert outcome is CancelOutcome.NOT_CANCELLABLE
            assert state["status"] == "accepted" and state["lease_token"] is not None
            await repo.release_claim(claims[0], clock())
            # Leave it cancelled so the next round has only one waiting message.
            assert (await repo.cancel_for_caller(ctx.caller_id, msg.id, clock()))[0] is (
                CancelOutcome.CANCELLED
            )
    print(f"race winners over 40 rounds: {wins}")


async def test_statistics_count_cancelled_as_failed(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    msg = await submit_tg(repo, ctx)
    await repo.cancel_for_caller(ctx.caller_id, msg.id, clock())
    assert await refresh(sf, clock())
    async with sf() as s:
        rows = (await s.execute(select(stats_failures))).mappings().all()
    assert [(r["channel"], r["purpose"], r["failure_code"], r["total"]) for r in rows] == [
        ("telegram", "broadcast", "cancelled", 1)
    ]


async def test_database_refuses_cancel_transition_outside_the_rule(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
) -> None:
    """The trigger allows accepted -> failed only as a Telegram cancel."""
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    from oan_messaging.storage.schema import messages

    ctx = await make_caller(sf, keys)
    sms_msg = await submit_sms(repo, ctx)
    tg_msg = await submit_tg(repo, ctx)
    cases = [
        (sms_msg.id, "cancelled"),  # SMS can never be cancelled
        (tg_msg.id, "provider_rejected"),  # Telegram, but not a cancel
    ]
    for mid, code in cases:
        with pytest.raises(DBAPIError):
            async with sf() as s, s.begin():
                await s.execute(
                    update(messages)
                    .where(messages.c.id == mid)
                    .values(status="failed", failure_code=code, content_purged_at=T0)
                )
