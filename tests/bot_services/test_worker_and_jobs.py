"""The worker: one replica holds the advisory lock, a second waits; timer and purge jobs."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from botsupport import new_contact, rid
from conftest import Bot
from test_flow_contacts import link_farmer

from oan_bot_services import jobs, worker


async def test_a_second_worker_waits_for_the_lock(bot: Bot) -> None:
    first_stop, second_stop = asyncio.Event(), asyncio.Event()
    first_locked, second_locked = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(
        worker.run(bot.deps, first_stop, tick=0.05, wait=0.05, on_locked=first_locked.set)
    )
    await asyncio.wait_for(first_locked.wait(), 10)
    second = asyncio.create_task(
        worker.run(bot.deps, second_stop, tick=0.05, wait=0.05, on_locked=second_locked.set)
    )
    await asyncio.sleep(0.3)
    assert not second_locked.is_set()  # waiting, running nothing
    first_stop.set()
    await asyncio.wait_for(first, 10)
    await asyncio.wait_for(second_locked.wait(), 10)
    second_stop.set()
    await asyncio.wait_for(second, 10)


async def test_worker_sends_a_broadcast_end_to_end(bot: Bot) -> None:
    from test_broadcasts import farmers_broadcast

    c = new_contact(700000501)
    await link_farmer(bot, c, "FR-TEST-0501", "+251900000501", "ET01010101")
    bid = (await farmers_broadcast(bot, "kebele", "ET01010101")).json()["broadcast_id"]
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(bot.deps, stop, tick=0.05, timer=0.05))
    for _ in range(100):
        if bot.up.oan_messages:
            break
        await asyncio.sleep(0.05)
    bot.up.settle("sent")
    bot.clock.now += timedelta(seconds=11)
    for _ in range(100):
        state = (await bot.bc("GET", f"/broadcasts/{bid}")).json()["state"]
        if state == "finished":
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert state == "finished"


async def test_a_failing_job_does_not_stop_the_others(bot: Bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []

    async def boom(deps: object) -> int:
        raise RuntimeError("x")

    async def fine(deps: object) -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr(worker.keyed, "release_idle", boom)
    monkeypatch.setattr(worker.stop, "retry_pending", fine)
    await worker.timer_job(bot.deps)
    assert calls == [1]


async def test_purge_removes_what_is_past_its_keep_time(bot: Bot) -> None:
    from test_broadcasts import farmers_broadcast

    c = new_contact(700000502)
    await link_farmer(bot, c, "FR-TEST-0502", "+251900000502", "ET01010101")
    await bot.flow("PUT", "/contacts/updates", {"request_id": rid(), "contact": c, "updates": "on"})
    await farmers_broadcast(bot, "kebele", "ET01010101")
    from oan_bot_services import broadcasts as bc

    await bc.submit_pending(bot.deps)
    bot.up.settle("sent")
    bot.clock.now += timedelta(seconds=10)
    await bc.poll_results(bot.deps)
    assert (await jobs.purge(bot.deps))["subscriptions"] == 0
    bot.clock.now += timedelta(days=366)
    out = await jobs.purge(bot.deps)
    assert out["subscriptions"] == 1 and out["broadcasts"] == 1
    assert out["request_replays"] >= 1 and out["broadcast_bindings"] == 1
    for table in (
        "subscriptions",
        "farmer_links",
        "broadcasts",
        "broadcast_recipients",
        "request_replays",
        "link_requests",
    ):
        assert await bot.q(f"SELECT count(*) FROM {table}") == [(0,)], table  # noqa: S608
