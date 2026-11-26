"""Contract 3.4.0 with the REAL runtime entrypoints as OS processes:

* caller registration (``caller --purposes broadcast``) follows the per-channel switch;
* an SMS-only worker starts and sends with no Telegram token and no Telegram setting at
  all, while Telegram messages wait in the same database.

Generated keys and certificates, 0900000xxx numbers, made-up chat ids, loopback only.
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest
from itsupport import Certs, Harness, TlsFakeProvider, new_key
from test_int_process import (
    Proc,
    _accept_now,
    child_env,
    procs,  # noqa: F401 (fixture)
    scalar,
    wait_until,
    worker_env,
)
from test_runtime_entrypoints import run_cli

from oan_messaging.contracts import Channel

pytestmark = pytest.mark.postgres

ON = "true"


@pytest.mark.parametrize(
    ("switches", "channels", "ok"),
    [
        ({"OAN_MSG_TELEGRAM_BROADCAST_ENABLED": ON}, "telegram", True),
        ({"OAN_MSG_TELEGRAM_BROADCAST_ENABLED": ON}, "sms", False),
        ({"OAN_MSG_TELEGRAM_BROADCAST_ENABLED": ON}, "sms,telegram", False),
        ({"OAN_MSG_BROADCAST_ENABLED": ON}, "sms,telegram", True),
        ({"OAN_MSG_BROADCAST_ENABLED": ON}, "sms", True),
        ({}, "telegram", False),
        ({}, "sms", False),
    ],
    ids=["tg-switch-tg", "tg-switch-sms", "tg-switch-both", "old-both", "old-sms",
         "off-tg", "off-sms"],
)
async def test_caller_registration_judges_broadcast_on_the_callers_channels(
    h: Harness, database_url: str, switches: dict[str, str], channels: str, ok: bool
) -> None:
    env = child_env(database_url, h.keys, **switches)
    out = run_cli(
        env, "caller", "--caller", "bulk", "--channels", channels, "--purposes",
        "transactional,broadcast", "--bot", "oan",
    )
    assert (out.returncode == 0) is ok, out.stderr[-2000:]
    stored = scalar(database_url, "SELECT count(*) FROM callers WHERE caller_id = 'bulk'")
    assert stored == (1 if ok else 0)


async def test_sms_only_worker_starts_and_sends_with_no_telegram_settings(
    h: Harness,
    certs: Certs,
    database_url: str,
    tmp_path: Path,
    procs: list[Proc],  # noqa: F811
) -> None:
    _token, sms_id = await _accept_now(h)
    # A Telegram caller (with a bot the SMS worker has no token for) and a waiting
    # Telegram message in the same database.
    bot = await h.caller("bot-svc", bot="oan", channels=frozenset({Channel.TELEGRAM}))
    tg = await h.post_tg(bot, new_key())
    assert tg.status_code == 202, tg.text
    gw = TlsFakeProvider(certs, require_client_cert=True, mode="ok")
    env = worker_env(database_url, h.keys, certs, gw, "sms-only") | {
        "OAN_MSG_WORKER_CHANNELS": "sms",
        "OAN_MSG_WORKER_HEARTBEAT_FILE": str(tmp_path / "w.alive"),
    }
    assert not [k for k in env if "TELEGRAM" in k]
    try:
        w = Proc(["-m", "oan_messaging.runtime", "worker"], env, tmp_path / "w.log")
        procs.append(w)
        await asyncio.to_thread(
            wait_until,
            lambda: scalar(
                database_url, "SELECT status FROM messages WHERE id=%s::uuid", sms_id
            ) == "sent",
            30,
            "SMS-only worker to send",
        )
        assert len(gw.requests) == 1
        tg_status = scalar(
            database_url, "SELECT status FROM messages WHERE id=%s::uuid", tg.json()["id"]
        )
        assert tg_status == "accepted"  # never touched by the SMS worker
        assert "channels=sms" in w.output()
        w.p.send_signal(signal.SIGTERM)
        assert w.p.wait(30) == 0, w.output()[-2000:]
    finally:
        gw.close()
