"""The Telegram webhook receiver and the worker that handles stored updates,
in the reduced runtime (RapidPro off)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from botenv import WEBHOOK_SECRET, Tg
from botsupport import sensitive

from oan_bot_services import conversation, jobs, worker
from oan_bot_services import text_keys as tk


async def stored(tg: Tg) -> int:
    [(n,)] = await tg.q("SELECT count(*) FROM telegram_updates")
    return int(n)


async def test_wrong_or_missing_secret_is_401_and_stores_nothing(tg: Tg) -> None:
    r = await tg.send(700001001, "/start", secret="wrong-" + WEBHOOK_SECRET)
    assert r.status_code == 401
    r = await tg.send(700001001, "/start", secret=None)
    assert r.status_code == 401
    assert await stored(tg) == 0
    assert await tg.q("SELECT count(*) FROM seen_updates") == [(0,)]


async def test_update_is_stored_sealed_before_the_answer_then_deleted(tg: Tg) -> None:
    chat = 700001002
    text = "my private words 0900000777"
    sensitive(text)
    r = await tg.send(chat, text)
    assert r.status_code == 200 and r.json() == {"ok": True}
    [(ref, body)] = await tg.q("SELECT contact_ref, body FROM telegram_updates")
    assert ref == tg.ref(chat)
    raw = bytes(body)
    assert str(chat).encode() not in raw and b"private words" not in raw
    await tg.run()
    assert await stored(tg) == 0  # deleted once handled
    assert await tg.q("SELECT count(*) FROM seen_updates") == [(1,)]
    # Only the minimal fields were ever kept: the sealed body opened.
    assert tg.up.replies(chat)[-1]["text"] == tg.text(tk.COMMON_NOT_READ)


async def test_a_repeated_update_id_answers_200_and_changes_nothing(tg: Tg) -> None:
    chat = 700001003
    r1 = await tg.send(chat, "/start", update_id=91000001)
    r2 = await tg.send(chat, "/start", update_id=91000001)
    assert r1.status_code == r2.status_code == 200
    assert await stored(tg) == 1
    await tg.run()
    r3 = await tg.send(chat, "/start", update_id=91000001)  # after handling: still a repeat
    assert r3.status_code == 200 and await stored(tg) == 0
    await tg.run()
    assert len(tg.up.replies(chat)) == 1


async def test_only_private_chats_are_handled(tg: Tg) -> None:
    for kind in ("group", "supergroup", "channel"):
        r = await tg.send(-100700001004, "/start", chat_type=kind)
        assert r.status_code == 200
    assert await stored(tg) == 0
    # Other update types (edited message, callback, member change) are dropped too.
    for update in ({"update_id": 91000010, "edited_message": {"text": "x"}},
                   {"update_id": 91000011, "my_chat_member": {}}):
        r = await tg.api.post("/telegram/webhook", json=update,
                              headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET})
        assert r.status_code == 200
    assert await stored(tg) == 0


async def test_oversized_or_malformed_bodies_store_nothing(tg: Tg) -> None:
    headers = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET,
               "content-type": "application/json"}
    big = json.dumps({"update_id": 91000020, "message": {"text": "x" * 70000}})
    r = await tg.api.post("/telegram/webhook", content=big, headers=headers)
    assert r.status_code == 413
    r = await tg.api.post("/telegram/webhook", content=b"not json", headers=headers)
    assert r.status_code == 400
    r = await tg.api.post("/telegram/webhook", json={"message": {}}, headers=headers)
    assert r.status_code == 400
    assert await stored(tg) == 0


async def test_store_failure_answers_503_so_telegram_retries(
    tg: Tg, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*a: object, **k: object) -> None:
        raise RuntimeError("database gone")

    monkeypatch.setattr("oan_bot_services.telegram.receive", boom)
    r = await tg.send(700001005, "/start")
    assert r.status_code == 503


async def test_worker_crash_mid_update_loses_nothing_and_does_nothing_twice(
    tg: Tg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the step changed but before the commit: everything rolls back, the
    update stays stored, and the next run handles it exactly once."""
    chat = 700001006
    await tg.send(chat, "/start")
    real = conversation.Turn.reply
    crashes = {"n": 1}

    async def crash_after_work(self: conversation.Turn, *a: object, **k: object) -> None:
        await real(self, *a, **k)  # type: ignore[arg-type]
        if crashes["n"]:
            crashes["n"] -= 1
            raise RuntimeError("worker killed")

    monkeypatch.setattr(conversation.Turn, "reply", crash_after_work)
    await tg.run()
    assert await stored(tg) == 1
    assert await tg.q("SELECT count(*) FROM outbox") == [(0,)]
    assert await tg.q("SELECT count(*) FROM conversations") == [(0,)]
    assert await tg.q("SELECT attempts FROM telegram_updates") == [(1,)]
    assert tg.up.replies(chat) == []
    await tg.run()
    assert await stored(tg) == 0
    assert len(tg.up.replies(chat)) == 1
    assert tg.up.replies(chat)[0]["text"] == tg.text(tk.ONBOARDING_CHOOSE_LANGUAGE)


async def test_crash_after_oan_accepted_a_reply_resends_with_the_same_key(
    tg: Tg, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = 700001007
    await tg.send(chat, "/start")
    await conversation.process_updates(tg.deps)
    [(key,)] = await tg.q("SELECT idempotency_key FROM outbox")
    real_submit = tg.deps.oan.submit

    async def accepted_then_killed(**kw: object) -> object:
        await real_submit(**kw)  # type: ignore[arg-type]
        raise RuntimeError("worker killed")

    monkeypatch.setattr(tg.deps.oan, "submit", accepted_then_killed)
    with pytest.raises(RuntimeError):
        await conversation.send_outbox(tg.deps)
    monkeypatch.setattr(tg.deps.oan, "submit", real_submit)
    await conversation.send_outbox(tg.deps)
    assert len(tg.up.oan_messages) == 1  # OAN Messaging saw the same key twice
    assert next(iter(tg.up.oan_messages.values()))["key"] == str(key)
    assert await tg.q("SELECT count(*) FROM outbox") == [(0,)]


async def test_updates_of_one_chat_wait_behind_a_failing_one(
    tg: Tg, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = 700001008, 700001009
    await tg.send(a, "first", update_id=91000030)
    await tg.send(a, "second", update_id=91000031)
    await tg.send(b, "other chat", update_id=91000032)
    real = conversation.handle

    async def fail_first(deps, s, ref, update_id, msg):  # type: ignore[no-untyped-def]
        if update_id == 91000030:
            raise RuntimeError("bug")
        await real(deps, s, ref, update_id, msg)

    monkeypatch.setattr(conversation, "handle", fail_first)
    assert await conversation.process_updates(tg.deps) == 1  # only chat b
    assert {u for (u,) in await tg.q("SELECT update_id FROM telegram_updates")} == {
        91000030, 91000031}
    for _ in range(conversation.MAX_ATTEMPTS - 1):
        await conversation.process_updates(tg.deps)
    # Dropped after the last attempt; then the chat's next update goes on.
    assert {u for (u,) in await tg.q("SELECT update_id FROM telegram_updates")} == {91000031}
    await conversation.process_updates(tg.deps)
    assert await stored(tg) == 0


async def test_update_ids_are_kept_thirty_days_then_purged(tg: Tg) -> None:
    await tg.send(700001010, "hello")
    await tg.run()
    tg.clock.now += timedelta(days=29)
    assert (await jobs.purge(tg.deps))["seen_updates"] == 0
    tg.clock.now += timedelta(days=2)
    assert (await jobs.purge(tg.deps))["seen_updates"] == 1


async def test_reply_not_sent_in_time_is_dropped(tg: Tg) -> None:
    chat = 700001011
    tg.up.down.add("oan")
    await tg.send(chat, "/start")
    await tg.run()
    assert await tg.q("SELECT count(*) FROM outbox") == [(1,)]  # kept while OAN is down
    tg.up.down.discard("oan")
    tg.clock.now += timedelta(minutes=31)
    await conversation.send_outbox(tg.deps)
    assert tg.up.replies(chat) == []
    assert await tg.q("SELECT count(*) FROM outbox") == [(0,)]


async def test_the_worker_runs_the_conversation_end_to_end(tg: Tg) -> None:
    chat = 700001012
    await tg.send(chat, "/start")
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(tg.deps, stop, tick=0.05, timer=0.05))
    for _ in range(100):
        if tg.up.replies(chat):
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 10)
    [reply] = tg.up.replies(chat)
    assert reply["purpose"] == "transactional"
    assert reply["source_reference"].startswith(f"bot:{tg.ref(chat)}:")
    assert reply["reply_markup"]["keyboard"] == [
        [{"text": tg.label(tk.BTN_LANG_EN)}, {"text": tg.label(tk.BTN_LANG_AM)}]
    ]
