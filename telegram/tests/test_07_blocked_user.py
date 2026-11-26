"""7. A user who blocked the bot: the subscription becomes unreachable and later
broadcasts skip it."""

from __future__ import annotations

import uuid

from conftest import copies, link_farmer, wait_for

KEBELE = "ET91010103"  # FR-TEST-0003, FR-TEST-0015


def test_blocked_user_is_skipped_later(tg, stubs, bot, new_chat):
    blocked, fine = new_chat(), new_chat()
    link_farmer(tg, stubs, blocked, "FR-TEST-0003")
    link_farmer(tg, stubs, fine, "FR-TEST-0015")
    tg.fault(blocked, "blocked")  # every send to this chat: 403 bot was blocked by the user

    title = f"First {uuid.uuid4().hex[:6]}"
    first = bot.broadcast(
        recipients="farmers", level="kebele", code=KEBELE, title=title, body="Vaccination day."
    )[1]
    done = bot.settled(first["broadcast_id"])
    assert done["counts"]["failed"] == 1, done
    assert done["counts"]["sent"] == first["recipients"] - 1, done
    assert copies(tg, fine, title)

    title2 = f"Second {uuid.uuid4().hex[:6]}"
    second = bot.broadcast(
        recipients="farmers",
        level="kebele",
        code=KEBELE,
        title=title2,
        body="Vaccination day moved.",
    )[1]
    assert second["recipients"] == first["recipients"] - 1
    wait_for(lambda: copies(tg, fine, title2), 60, what="the second copy")
    done2 = bot.settled(second["broadcast_id"])
    assert done2["counts"]["failed"] == 0, done2
    tg.fault(blocked, "none")
