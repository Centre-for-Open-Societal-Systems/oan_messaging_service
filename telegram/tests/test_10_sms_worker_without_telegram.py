"""10. The SMS worker starts and sends with no Telegram settings at all."""

from __future__ import annotations

import json
import subprocess
import uuid

from conftest import STACK, compose, wait_for


def test_sms_worker_has_no_telegram_settings_and_sends(oan, sms8028):
    cid = compose("ps", "-q", "oan-worker-sms").strip()
    inspect = json.loads(
        subprocess.run(
            ["docker", "inspect", cid], check=True, text=True, capture_output=True, cwd=STACK
        ).stdout
    )[0]
    names = [e.split("=", 1)[0] for e in inspect["Config"]["Env"]]
    assert not [n for n in names if "TELEGRAM" in n], names
    assert inspect["State"]["Running"]
    assert "OAN_MSG_WORKER_CHANNELS" in names

    # Restart it, to show it starts with no Telegram settings, then send through it.
    compose("restart", "oan-worker-sms")
    text = f"Your grievance was updated {uuid.uuid4().hex[:6]}."
    mid = oan.sms("0900000031", text, purpose="transactional")["id"]
    wait_for(
        lambda: [m for m in sms8028.sent("0900000031") if m["text"] == text],
        30,
        every=0.5,
        what="the SMS at the fake 8028",
    )
    assert oan.get(mid)["status"] == "sent"
