"""5. A repeated Telegram update changes nothing (the repeated broadcast request is in
test_04)."""

from __future__ import annotations

import time

import httpx

from conftest import GATEWAY


def test_repeated_update_is_answered_once(tg, stack_env, new_chat):
    tid = new_chat()
    update = {
        "update_id": 990000000 + tid % 1000000,
        "message": {
            "message_id": 1,
            "date": int(time.time()),
            "text": "/start",
            "chat": {"id": tid, "type": "private", "first_name": "Test"},
            "from": {"id": tid, "is_bot": False, "first_name": "Test"},
        },
    }
    gw = httpx.Client(
        base_url=GATEWAY,
        verify=False,
        headers={
            "Host": "webhook.example.invalid",
            "X-Telegram-Bot-Api-Secret-Token": stack_env["TG_WEBHOOK_SECRET"],
        },
    )
    assert gw.post("/telegram/webhook", json=update).status_code == 200
    assert gw.post("/telegram/webhook", json=update).status_code == 200
    first = tg.wait(tid, 1)
    assert first[0]["text"] == "Welcome to OpenAgriNet. Please choose your language."
    time.sleep(8)
    assert len(tg.sent(tid)) == 1
