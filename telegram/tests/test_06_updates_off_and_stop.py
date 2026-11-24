"""6. Updates off and Stop: later copies are not sent, and a copy still waiting in OAN
Messaging is cancelled."""

from __future__ import annotations

import time
import uuid

from conftest import copies, link_farmer, oan_rows, wait_for

KEBELE = "ET91010102"  # FR-TEST-0002 and FR-TEST-0014


def send(bot, title):
    return bot.broadcast(
        recipients="farmers",
        level="kebele",
        code=KEBELE,
        title=title,
        body="Seed distribution on Monday.",
    )[1]


def test_updates_off_on_and_stop(tg, stubs, bot, new_chat):
    a, b = new_chat(), new_chat()
    link_farmer(tg, stubs, a, "FR-TEST-0002")
    link_farmer(tg, stubs, b, "FR-TEST-0014")

    assert tg.say(a, "Updates on/off")[0]["text"].startswith("Updates are off.")
    stop = tg.say(b, "Stop")[0]
    assert stop["text"].startswith("You will get no more updates here.")
    assert "90 days" in stop["text"]
    assert stop["reply_markup"] == {"remove_keyboard": True}

    title = f"Seeds {uuid.uuid4().hex[:6]}"
    created = send(bot, title)
    time.sleep(12)
    assert not copies(tg, a, title) and not copies(tg, b, title)

    # Updates on again within the 90 days: copies come again, without linking again.
    assert tg.say(a, "Updates on/off")[0]["text"] == "Updates are on."
    title2 = f"Seeds again {uuid.uuid4().hex[:6]}"
    created2 = send(bot, title2)
    assert created2["recipients"] == created["recipients"] + 1
    wait_for(lambda: copies(tg, a, title2), 60, what="the copy after updates on")
    assert not copies(tg, b, title2)


def test_a_copy_waiting_in_oan_messaging_is_cancelled(tg, stubs, bot, new_chat):
    tid = new_chat()
    link_farmer(tg, stubs, tid, "FR-TEST-0002")
    # Telegram cannot be reached: the copy waits in OAN Messaging (accepted, retried later).
    tg.down(True)
    title = f"Waiting {uuid.uuid4().hex[:6]}"
    created = bot.broadcast(
        recipients="farmers",
        level="kebele",
        code=KEBELE,
        title=title,
        body="This copy must never arrive.",
    )[1]
    bid = created["broadcast_id"]
    pattern = {"pattern": f"bc:{bid}:%"}
    # Handed over: OAN Messaging holds the copy and has tried Telegram at least once.
    wait_for(
        lambda: any(
            r["status"] == "accepted" and int(r["attempts"]) >= 1
            for r in oan_rows("source_reference LIKE :'pattern'", pattern)
        ),
        60,
        what="the copy waiting in OAN Messaging",
    )
    # The farmer turns updates off; Bot Services asks OAN Messaging to cancel the copy.
    tg.send(tid, "Updates on/off")
    cancelled = wait_for(
        lambda: [
            r
            for r in oan_rows("source_reference LIKE :'pattern'", pattern)
            if r["status"] == "failed" and r["failure_code"] == "cancelled"
        ],
        60,
        what="the cancelled copy in OAN Messaging",
    )
    assert len(cancelled) >= 1
    done = wait_for(
        lambda: (p := bot.progress(bid)) and p["counts"]["failed"] >= 1 and p,
        60,
        what="the cancelled copy counted",
    )
    tg.down(False)
    # The reply to Updates off arrives once Telegram is back; the copy never does.
    wait_for(
        lambda: any((m.get("text") or "").startswith("Updates are off") for m in tg.sent(tid)),
        60,
        what="the delayed reply",
    )
    time.sleep(10)
    assert not copies(tg, tid, title)
    assert done["counts"]["failed"] >= 1
