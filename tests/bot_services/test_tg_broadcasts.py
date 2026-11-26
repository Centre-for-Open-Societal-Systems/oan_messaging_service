"""Broadcast copies and retention without RapidPro: targeting, the re-check before each
submit and the cancel route, unreachable chats, and the 90-day rule."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

from botenv import BODY, Tg
from botsupport import sensitive
from sqlalchemy import text

from oan_bot_services import broadcasts as bc
from oan_bot_services import jobs, worker
from oan_bot_services import text_keys as tk


async def farmers(tg: Tg, level: str, code: str, key: str | None = None) -> Any:
    body = {"recipients": "farmers", "scope": {"level": level, "code": code}, **BODY}
    r = await tg.bc("POST", "/broadcasts", key=key or str(uuid.uuid4()), body=body)
    assert r.status_code in (200, 202), r.text
    return r.json()


def copies(tg: Tg, chat: int) -> list[dict[str, Any]]:
    return [m for m in tg.up.oan_messages.values()
            if m["body"]["chat_id"] == chat and m["body"]["purpose"] != "transactional"]


async def status_of(tg: Tg, chat: int) -> list[tuple[str, str | None]]:
    rows = await tg.q(
        "SELECT status, failure_code FROM broadcast_recipients WHERE contact_ref = :r ORDER BY n",
        r=tg.ref(chat),
    )
    return sorted(((s, f) for s, f in rows), key=str)


async def seed(tg: Tg) -> dict[str, int]:
    """Region ET01: two woredas; region ET02: one farmer."""
    plan = {"A": (700003001, "ET01010101"), "B": (700003002, "ET01010201"),
            "C": (700003003, "ET01010102"), "Z": (700003004, "ET02010101")}
    for i, (chat, kebele) in enumerate(plan.values()):
        await tg.link(chat, f"FR-TEST-31{i:02d}", f"+2519000031{i:02d}", kebele)
    return {k: v[0] for k, v in plan.items()}


async def test_region_targeting_round_robin_and_one_copy_each(tg: Tg) -> None:
    f = await seed(tg)
    out = await farmers(tg, "region", "ET01")
    assert out["recipients"] == 3
    await bc.submit_pending(tg.deps)
    for name in ("A", "B", "C"):
        [m] = copies(tg, f[name])
        assert m["body"]["text"] == f"{BODY['title']}\n\n{BODY['body']}"
        assert "reply_markup" not in m["body"]
    assert copies(tg, f["Z"]) == []
    rows = await tg.q("SELECT woreda_code FROM broadcast_recipients ORDER BY n")
    assert [w for (w,) in rows] == ["ET010101", "ET010102", "ET010101"]


async def test_same_key_is_one_broadcast(tg: Tg) -> None:
    await seed(tg)
    key = str(uuid.uuid4())
    first = await farmers(tg, "zone", "ET0101", key)
    again = await farmers(tg, "zone", "ET0101", key)
    assert first == again
    assert await tg.q("SELECT count(*) FROM broadcasts") == [(1,)]


async def test_development_agents_minus_the_sender(tg: Tg) -> None:
    agent = {"role": "development_agent", "display_name": "Test", "telegram_linked": True}
    tg.up.roster["ET01010101"] = [
        {**agent, "ref": "COMMS-TEST-001", "telegram_id": 700003101},
        {**agent, "ref": "DA-TEST-102", "telegram_id": 700003102},
        {**agent, "ref": "DA-TEST-103", "telegram_linked": False},
    ]
    sensitive(700003101, 700003102)
    body = {"recipients": "das", "scope": {"level": "kebele", "code": "ET01010101"}, **BODY}
    r = await tg.bc("POST", "/broadcasts", key=str(uuid.uuid4()), body=body)
    assert r.json()["recipients"] == 1 and r.json()["not_on_telegram"] == ["DA-TEST-103"]
    await bc.submit_pending(tg.deps)
    assert copies(tg, 700003101) == [] and len(copies(tg, 700003102)) == 1


async def test_updates_off_skips_waiting_copies_and_cancels_handed_over_ones(tg: Tg) -> None:
    f = await seed(tg)
    await farmers(tg, "kebele", "ET01010101")  # A only
    await bc.submit_pending(tg.deps)
    await farmers(tg, "kebele", "ET01010101")  # a second one, not yet submitted
    assert (await tg.say(f["A"], tg.label(tk.BTN_UPDATES_TOGGLE)))["text"] == tg.text(
        tk.UPDATES_OFF)
    assert await status_of(tg, f["A"]) == sorted(
        [("accepted", None), ("failed", "not_eligible")], key=str)
    assert await bc.cancel_pending(tg.deps) == 1
    assert await status_of(tg, f["A"]) == sorted(
        [("failed", "cancelled"), ("failed", "not_eligible")], key=str)
    [m] = copies(tg, f["A"])
    assert m["status"] == "failed" and m["failure_code"] == "cancelled"
    # Nothing else is sent to A, and both broadcasts are finished.
    await bc.submit_pending(tg.deps)
    tg.clock.now += timedelta(seconds=10)
    await bc.poll_results(tg.deps)
    assert len(copies(tg, f["A"])) == 1
    states = {s for (s,) in await tg.q("SELECT state FROM broadcasts")}
    assert states == {"finished"}


async def test_stop_cancels_and_a_claimed_copy_is_not_cancellable(tg: Tg) -> None:
    f = await seed(tg)
    out = await farmers(tg, "zone", "ET0101")
    await bc.submit_pending(tg.deps)
    tg.up.claim(f["A"])  # OAN's worker already took A's copy
    await tg.say(f["A"], "/stop")
    await tg.say(f["B"], "/stop")
    assert await bc.cancel_pending(tg.deps) == 1  # B only
    assert await status_of(tg, f["B"]) == [("failed", "cancelled")]
    assert await status_of(tg, f["A"]) == [("accepted", None)]  # it may still arrive
    assert await tg.q("SELECT count(*) FROM broadcast_recipients WHERE cancel_requested") == [
        (0,)]
    tg.up.settle("sent", chat_id=f["A"])
    tg.clock.now += timedelta(seconds=10)
    await bc.poll_results(tg.deps)
    assert await status_of(tg, f["A"]) == [("sent", None)]
    progress = (await tg.bc("GET", f"/broadcasts/{out['broadcast_id']}")).json()
    assert progress["counts"]["failed"] == 1 and progress["counts"]["sent"] == 1


async def test_cancel_is_retried_when_oan_is_down(tg: Tg) -> None:
    f = await seed(tg)
    await farmers(tg, "kebele", "ET01010101")
    await bc.submit_pending(tg.deps)
    await tg.say(f["A"], "/stop")
    tg.up.down.add("oan")
    assert await bc.cancel_pending(tg.deps) == 0
    assert await tg.q("SELECT cancel_requested FROM broadcast_recipients") == [(True,)]
    tg.up.down.discard("oan")
    assert await bc.cancel_pending(tg.deps) == 1


async def test_the_subscription_is_checked_again_just_before_each_submit(tg: Tg) -> None:
    f = await seed(tg)
    await farmers(tg, "zone", "ET0101")
    # Changed after the broadcast was created (e.g. marked unreachable by another one).
    async with tg.deps.engine.begin() as conn:
        await conn.execute(
            text("UPDATE subscriptions SET state = 'unreachable' WHERE chat_id = :c"),
            {"c": f["B"]},
        )
    await bc.submit_pending(tg.deps)
    assert copies(tg, f["B"]) == []
    assert await status_of(tg, f["B"]) == [("failed", "not_eligible")]
    assert len(copies(tg, f["A"])) == 1 and len(copies(tg, f["C"])) == 1


async def test_worker_restart_submits_each_copy_once(tg: Tg) -> None:
    f = await seed(tg)
    out = await farmers(tg, "zone", "ET0101")
    tg.deps.settings = tg.deps.settings.model_copy(update={"batch_size": 1})
    await bc.submit_pending(tg.deps)  # one copy, then the worker "dies"
    assert sum(len(copies(tg, f[n])) for n in "ABC") == 1
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(tg.deps, stop, tick=0.02, timer=60))
    for _ in range(200):
        if sum(len(copies(tg, f[n])) for n in "ABC") == 3:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert [len(copies(tg, f[n])) for n in "ABC"] == [1, 1, 1]
    keys = [m["key"] for m in tg.up.oan_messages.values()
            if m["body"]["purpose"] != "transactional"]
    assert len(keys) == len(set(keys)) == 3
    assert (await tg.bc("GET", f"/broadcasts/{out['broadcast_id']}")).json()["counts"][
        "accepted"] == 3


async def test_unknown_is_never_resent(tg: Tg) -> None:
    f = await seed(tg)
    await farmers(tg, "kebele", "ET01010101")
    await bc.submit_pending(tg.deps)
    tg.up.settle("unknown", "timeout", chat_id=f["A"])
    tg.clock.now += timedelta(seconds=10)
    await bc.poll_results(tg.deps)
    for _ in range(3):
        tg.clock.now += timedelta(seconds=10)
        await bc.submit_pending(tg.deps)
        await bc.poll_results(tg.deps)
    assert len(copies(tg, f["A"])) == 1
    assert await status_of(tg, f["A"]) == [("unknown", "timeout")]


async def test_blocked_chat_becomes_unreachable_and_comes_back_by_writing(tg: Tg) -> None:
    f = await seed(tg)
    await farmers(tg, "kebele", "ET01010101")
    await bc.submit_pending(tg.deps)
    tg.up.settle("failed", "recipient_blocked_bot", chat_id=f["A"])
    tg.clock.now += timedelta(seconds=10)
    await bc.poll_results(tg.deps)
    assert await tg.q("SELECT state, stopped_at FROM subscriptions WHERE chat_id = :c",
                      c=f["A"]) == [("unreachable", tg.clock.now)]
    assert (await farmers(tg, "kebele", "ET01010101"))["recipients"] == 0
    # The farmer unblocks the bot and writes: the chat works again.
    await tg.say(f["A"], "hello")
    assert await tg.q("SELECT state, stopped_at FROM subscriptions WHERE chat_id = :c",
                      c=f["A"]) == [("active", None)]


# -------------------------------------------------------------------- retention


async def test_stopped_subscription_is_deleted_after_ninety_days(tg: Tg) -> None:
    f = await seed(tg)
    await tg.say(f["A"], "/stop")
    tg.clock.now += timedelta(days=89)
    assert (await jobs.purge(tg.deps))["subscriptions"] == 0
    tg.clock.now += timedelta(days=2)
    out = await jobs.purge(tg.deps)
    assert out["subscriptions"] == 1 and out["contacts"] == 1
    ref = tg.ref(f["A"])
    for table in ("subscriptions", "farmer_links", "contacts", "conversations"):
        rows = await tg.q(f"SELECT count(*) FROM {table} WHERE contact_ref = :r", r=ref)  # noqa: S608
        assert rows == [(0,)], table
    assert await tg.q("SELECT count(*) FROM subscriptions") == [(3,)]


async def test_unreachable_subscription_is_deleted_after_ninety_days(tg: Tg) -> None:
    f = await seed(tg)
    await farmers(tg, "kebele", "ET01010101")
    await bc.submit_pending(tg.deps)
    tg.up.settle("failed", "chat_not_found", chat_id=f["A"])
    tg.clock.now += timedelta(seconds=10)
    await bc.poll_results(tg.deps)
    tg.clock.now += timedelta(days=91)
    assert (await jobs.purge(tg.deps))["subscriptions"] == 1
    assert await tg.q("SELECT count(*) FROM farmer_links WHERE contact_ref = :r",
                      r=tg.ref(f["A"])) == [(0,)]


async def test_updates_on_within_ninety_days_restores_without_linking(tg: Tg) -> None:
    f = await seed(tg)
    await tg.say(f["A"], "/stop")
    tg.clock.now += timedelta(days=80)
    r = await tg.say(f["A"], tg.label(tk.BTN_UPDATES_TOGGLE))
    assert r["text"] == tg.text(tk.UPDATES_ON)
    assert await tg.q("SELECT updates_on, state, stopped_at FROM subscriptions WHERE chat_id = :c",
                      c=f["A"]) == [(True, "active", None)]
    tg.clock.now += timedelta(days=30)
    assert (await jobs.purge(tg.deps))["subscriptions"] == 0
    assert (await farmers(tg, "kebele", "ET01010101"))["recipients"] == 1


async def test_nothing_is_deleted_for_reachable_subscribers(tg: Tg) -> None:
    """No inactivity expiry while reachable, even with updates turned off."""
    f = await seed(tg)
    await tg.say(f["B"], tg.label(tk.BTN_UPDATES_TOGGLE))  # updates off, still reachable
    tg.clock.now += timedelta(days=800)
    out = await jobs.purge(tg.deps)
    assert out["subscriptions"] == 0 and out["contacts"] == 0
    assert await tg.q("SELECT count(*) FROM subscriptions") == [(4,)]
    assert await tg.q("SELECT count(*) FROM farmer_links") == [(4,)]


async def test_unlinked_stopped_chat_is_deleted_after_ninety_days(tg: Tg) -> None:
    chat = 700003020
    await tg.onboard(chat)
    await tg.say(chat, "/stop")
    tg.clock.now += timedelta(days=91)
    assert (await jobs.purge(tg.deps))["contacts"] == 1
    assert await tg.q("SELECT count(*) FROM contacts") == [(0,)]
