"""8. Telegram down, and Telegram's 429 with retry_after: the SMS copy submitted to OAN
Messaging still reaches the fake 8028 on time; Telegram copies go out once the fault
clears; nothing unknown is resent."""

from __future__ import annotations

import time
import uuid

from conftest import copies, link_farmer, oan_rows, wait_for

KEBELE = "ET92010102"  # FR-TEST-0008, FR-TEST-0020
SMS_PHONES = ["0900000008", "0900000020"]


def test_sms_is_on_time_while_telegram_is_down(tg, stubs, bot, oan, sms8028, new_chat):
    a, b = new_chat(), new_chat()
    link_farmer(tg, stubs, a, "FR-TEST-0008")
    link_farmer(tg, stubs, b, "FR-TEST-0020")
    tg.down(True)
    title = f"Flood warning {uuid.uuid4().hex[:6]}"
    created = bot.broadcast(
        recipients="farmers",
        level="kebele",
        code=KEBELE,
        title=title,
        body="Move animals to high ground.",
        source="Emergency",
    )[1]
    # The DA backend sends the SMS copy independently (TG-45), purpose alert.
    started = time.monotonic()
    ids = [oan.sms(p, f"{title}: Move animals to high ground.")["id"] for p in SMS_PHONES]
    for phone in SMS_PHONES:
        wait_for(
            lambda p=phone: [m for m in sms8028.sent(p) if m["text"].startswith(title)],
            20,
            every=0.5,
            what="the SMS copy at the fake 8028",
        )
    sms_seconds = time.monotonic() - started
    assert sms_seconds < 20
    assert all(oan.get(i)["status"] == "sent" for i in ids)
    assert not copies(tg, a, title) and not copies(tg, b, title)

    time.sleep(10)  # Telegram still down: the copies wait in OAN Messaging
    tg.down(False)
    for tid in (a, b):
        wait_for(lambda t=tid: copies(tg, t, title), 120, what="the copy after Telegram is back")
    done = bot.settled(created["broadcast_id"], 120)
    assert done["counts"]["sent"] == created["recipients"], done
    assert done["counts"]["unknown"] == 0
    rows = oan_rows(
        "source_reference LIKE :'pattern'", {"pattern": f"bc:{created['broadcast_id']}:%"}
    )
    assert rows and all(r["status"] == "sent" for r in rows), rows
    assert any(int(r["attempts"]) > 1 for r in rows), rows  # it really waited and retried
    for tid in (a, b):
        assert len(copies(tg, tid, title)) == 1
    print(f"SMS copy reached the fake 8028 in {sms_seconds:.1f}s while Telegram was down")


def test_429_retry_after_is_honoured(tg, stubs, bot, new_chat):
    tid = new_chat()
    link_farmer(tg, stubs, tid, "FR-TEST-0009")
    tg.fault(tid, "rate_limited")  # next send to this chat: 429 with retry_after 3
    title = f"Market day {uuid.uuid4().hex[:6]}"
    created = bot.broadcast(
        recipients="farmers",
        level="kebele",
        code="ET92010103",
        title=title,
        body="Market moved to Thursday.",
    )[1]
    got = wait_for(lambda: copies(tg, tid, title), 60, what="the copy after the 429")
    rows = oan_rows(
        "source_reference LIKE :'pattern'", {"pattern": f"bc:{created['broadcast_id']}:%"}
    )
    mine = [r for r in rows if r["status"] == "sent"]
    assert mine and any(int(r["attempts"]) >= 2 for r in mine), rows
    done = bot.settled(created["broadcast_id"])
    assert done["counts"]["unknown"] == 0
    assert len(got) == 1
