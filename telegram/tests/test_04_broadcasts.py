"""4. POST /broadcasts to the farmers of a region and to Development Agents (the sender
left out): copies arrive at the fake Telegram; status ends at sent, never delivered.
5 (part). A repeated broadcast request creates nothing new."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from conftest import copies, link_farmer, wait_for

REGION_FARMERS = ["FR-TEST-0010", "FR-TEST-0012", "FR-TEST-0019"]  # region ET92


def test_region_broadcast_reaches_linked_farmers_once(tg, stubs, bot, new_chat):
    chats = [new_chat() for _ in REGION_FARMERS]
    with ThreadPoolExecutor(len(chats)) as pool:
        list(
            pool.map(lambda a: link_farmer(tg, stubs, *a), zip(chats, REGION_FARMERS, strict=True))
        )
    outsider = new_chat()  # started the bot, never linked: gets nothing
    tg.say(outsider, "/start")
    title = f"Rain alert {uuid.uuid4().hex[:6]}"
    key = str(uuid.uuid4())
    status, created = bot.broadcast(
        recipients="farmers",
        level="region",
        code="ET92",
        title=title,
        body="Heavy rain expected tomorrow.",
        source="Weather",
        key=key,
    )
    assert status == 202
    assert created["recipients"] >= len(chats)
    for tid in chats:
        got = wait_for(lambda t=tid: copies(tg, t, title), 60, what="the broadcast copy")
        assert got[0]["text"] == f"{title}\n\nHeavy rain expected tomorrow."
        assert got[0]["reply_markup"] is None
    done = bot.settled(created["broadcast_id"])
    # Every linked chat of the region; on a fresh stack all of them are this test's or
    # earlier tests' chats, all reachable.
    assert done["counts"]["sent"] >= len(chats), done
    assert done["counts"]["sent"] + done["counts"]["failed"] == created["recipients"], done
    assert done["counts"]["unknown"] == 0
    assert "delivered" not in done["counts"]
    assert not copies(tg, outsider, title)

    # The same request again: the original answer, nothing new sent.
    again_status, again = bot.broadcast(
        recipients="farmers",
        level="region",
        code="ET92",
        title=title,
        body="Heavy rain expected tomorrow.",
        source="Weather",
        key=key,
    )
    assert again_status == 200 and again["broadcast_id"] == created["broadcast_id"]
    time.sleep(12)
    for tid in chats:
        assert len(copies(tg, tid, title)) == 1


def test_agent_broadcast_leaves_out_the_sender(tg, bot):
    title = f"Agents meeting {uuid.uuid4().hex[:6]}"
    status, created = bot.broadcast(
        recipients="das",
        level="woreda",
        code="ET910101",
        title=title,
        body="Woreda meeting on Friday.",
        sender_ref="DA-TEST-001",
        sender_role="development_agent",
    )
    assert status == 202
    assert "DA-TEST-003" in created["not_on_telegram"]  # not linked in the DA backend
    for chat in (900000102, 900000104, 900000105):
        wait_for(lambda c=chat: copies(tg, c, title), 60, what="the agent's copy")
    done = bot.settled(created["broadcast_id"])
    assert done["counts"]["sent"] == created["recipients"], done
    assert not copies(tg, 900000101, title)  # the sender
    assert not copies(tg, 900000107, title)  # another woreda
