"""9. The Bot Services worker is killed and started again in the middle of a broadcast:
no copy is sent twice and none is lost."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from conftest import compose, copies, link_farmer, wait_for

WOREDA = "ET910102"
FARMERS = ["FR-TEST-0004", "FR-TEST-0016", "FR-TEST-0028", "FR-TEST-0006", "FR-TEST-0018"]


def test_worker_killed_mid_broadcast(tg, stubs, bot, new_chat):
    chats = [new_chat() for _ in FARMERS]
    with ThreadPoolExecutor(len(chats)) as pool:
        list(pool.map(lambda a: link_farmer(tg, stubs, *a), zip(chats, FARMERS, strict=True)))
    title = f"Training {uuid.uuid4().hex[:6]}"
    created = bot.broadcast(
        recipients="farmers",
        level="woreda",
        code=WOREDA,
        title=title,
        body="Training at the FTC on Saturday.",
    )[1]
    assert created["recipients"] >= len(chats)
    # Kill the worker as soon as the first copy is out, then start it again.
    wait_for(lambda: any(copies(tg, t, title) for t in chats), 60, every=0.2, what="the first copy")
    compose("kill", "-s", "SIGKILL", "bot-worker")
    time.sleep(3)
    compose("start", "bot-worker")
    for tid in chats:
        wait_for(lambda t=tid: copies(tg, t, title), 120, what="every copy after the restart")
    done = bot.settled(created["broadcast_id"], 120)
    assert done["counts"]["sent"] == created["recipients"], done
    time.sleep(15)  # one more poll cycle: nothing new may arrive
    for tid in chats:
        assert len(copies(tg, tid, title)) == 1, tid
