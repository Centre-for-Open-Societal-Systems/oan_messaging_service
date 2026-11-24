"""End-to-end tests against the local Telegram stack (telegram/stack).

Start the stack first: ``cd telegram/stack && ./init-env.sh && ./up.sh``. Then, from the
repository root: ``pytest telegram/tests``. The tests drive the stack over HTTP on the
loopback ports of its `edge` container, play the farmers through the fake Telegram, and
read what OAN Messaging sent there and to the fake 8028. They read the generated
``telegram/stack/.env`` for the tokens a test needs, and never print them.

Every test uses its own Telegram ids and its own kebeles, so the tests do not depend on
each other. Run them on a fresh stack (``./reset.sh && ./up.sh``) for exact counts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import ssl
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

STACK = Path(__file__).resolve().parent.parent / "stack"
TG = "http://127.0.0.1:28301"
SMS = "http://127.0.0.1:28302"
GATEWAY = "https://127.0.0.1:28443"
BOT = "http://127.0.0.1:28100"
STUBS = "http://127.0.0.1:28200"
OAN = "https://127.0.0.1:28000"

#: Seed agents of the DA stub that are linked to Telegram (telegram/stubs/seed.json).
SEED_AGENT_CHATS = [
    900000101,
    900000102,
    900000104,
    900000105,
    900000107,
    900000108,
    900000109,
    900000110,
    900000201,
    900000203,
]


def env() -> dict[str, str]:
    out = {}
    for line in (STACK / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def compose(*args: str) -> str:
    return subprocess.run(
        ["docker", "compose", *args], cwd=STACK, check=True, capture_output=True, text=True
    ).stdout


def oan_rows(where: str, params: dict[str, str]) -> list[dict[str, str]]:
    """Read OAN Messaging's message rows (status, failure code, attempts) for evidence.
    ``where`` uses psql variables, for example ``source_reference LIKE :'pattern'``."""
    cols = "source_reference, channel, purpose, status, coalesce(failure_code, ''), attempts"
    args = [
        "exec",
        "-T",
        "oan-db",
        "psql",
        "-U",
        "postgres",
        "-d",
        "oan_messaging",
        "-tA",
        "-F",
        "|",
        "-v",
        "ON_ERROR_STOP=1",
    ]
    for k, v in params.items():
        args += ["-v", f"{k}={v}"]
    out = subprocess.run(
        ["docker", "compose", *args],
        cwd=STACK,
        check=True,
        text=True,
        capture_output=True,
        input=f"SELECT {cols} FROM messages WHERE {where} ORDER BY created_at;",
    )
    keys = ["source_reference", "channel", "purpose", "status", "failure_code", "attempts"]
    return [
        dict(zip(keys, line.split("|"), strict=True)) for line in out.stdout.splitlines() if line
    ]


def wait_for(
    check: Callable[[], Any], timeout: float = 60, every: float = 1.0, what: str = "condition"
) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        got = check()
        if got:
            return got
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(every)


class FakeTelegram:
    def __init__(self) -> None:
        self.c = httpx.Client(base_url=TG, timeout=40)

    def user(self, tid: int) -> int:
        self.c.post(
            "/_test/users", json={"telegram_id": tid, "first_name": "Test"}
        ).raise_for_status()
        return tid

    def send(self, tid: int, text: str | None = None, contact: dict | None = None) -> dict:
        body: dict[str, Any] = {"telegram_id": tid}
        if contact is not None:
            body["contact"] = contact
        else:
            body["text"] = text
        r = self.c.post("/_test/updates", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["webhook_status"] == 200, r.text
        return r.json()

    def wait(self, tid: int, count: int = 1, timeout: float = 30) -> list[dict]:
        r = self.c.get(
            "/_test/wait",
            params={"telegram_id": tid, "count": count, "timeout_ms": int(timeout * 1000)},
        )
        assert r.status_code == 200, f"no reply within {timeout}s: {r.text[:300]}"
        return r.json()["messages"]

    def say(
        self, tid: int, text: str | None = None, contact: dict | None = None, replies: int = 1
    ) -> list[dict]:
        self.send(tid, text, contact)
        return self.wait(tid, replies)

    def sent(self, tid: int) -> list[dict]:
        return self.c.get("/_test/sent", params={"telegram_id": tid}).json()["messages"]

    def fault(self, tid: int, mode: str) -> None:
        self.c.post("/_test/faults", json={"telegram_id": tid, "mode": mode}).raise_for_status()

    def down(self, on: bool) -> None:
        self.c.post("/_test/telegram", json={"down": on}).raise_for_status()

    def reset(self) -> None:
        self.c.post("/_test/reset").raise_for_status()


#: OAN Bot Services derives the Auth link request id from the chat and the update that
#: shared the contact (conversation.LINK_NS), so a redo asks Auth again with the same id.
LINK_NS = uuid.UUID("6f1d8c3e-4b2a-4e0f-9c71-0a3b5d7e9f11")


class Stubs:
    def __init__(self, contact_ref_key: str) -> None:
        self.c = httpx.Client(base_url=STUBS, timeout=20)
        self._ref_key = base64.b64decode(contact_ref_key)

    def otp(self, tid: int, update_id: int) -> dict:
        """The code the Auth stub "sent" for the link request of this contact update."""
        ref = hmac.new(self._ref_key, str(tid).encode(), hashlib.sha256).hexdigest()
        lrid = uuid.uuid5(LINK_NS, f"{ref}:{update_id}")
        r = self.c.get("/auth/_test/codes", params={"link_request_id": str(lrid)})
        assert r.status_code == 200, r.text
        return r.json()

    def staff_code(self, ref: str) -> str:
        r = self.c.post("/da/_test/link-codes", json={"ref": ref})
        r.raise_for_status()
        return r.json()["code"]

    def reset(self) -> None:
        self.c.post("/_test/reset").raise_for_status()


class BotServices:
    def __init__(self, token: str) -> None:
        self.c = httpx.Client(
            base_url=BOT, timeout=20, headers={"Authorization": f"Bearer {token}"}
        )

    def broadcast(
        self,
        *,
        recipients: str,
        level: str,
        code: str,
        title: str,
        body: str,
        sender_ref: str = "COMMS-TEST-1",
        sender_role: str = "communications_officer",
        source: str = "Knowledge",
        key: str | None = None,
    ) -> tuple[int, dict]:
        key = key or str(uuid.uuid4())
        payload = {
            "recipients": recipients,
            "scope": {"level": level, "code": code},
            "sender_ref": sender_ref,
            "sender_role": sender_role,
            "source": source,
            "source_reference": f"e2e:{key[:8]}",
            "title": title,
            "body": body,
        }
        r = self.c.post("/broadcasts", json=payload, headers={"Idempotency-Key": key})
        assert r.status_code in (200, 202), r.text
        return r.status_code, r.json()

    def progress(self, broadcast_id: str) -> dict:
        r = self.c.get(f"/broadcasts/{broadcast_id}")
        r.raise_for_status()
        return r.json()

    def settled(self, broadcast_id: str, timeout: float = 90) -> dict:
        """Wait until no copy is waiting (accepted or sending)."""

        def done() -> dict | None:
            p = self.progress(broadcast_id)
            c = p["counts"]
            return p if c["accepted"] == 0 and c["sending"] == 0 and sum(c.values()) else None

        return wait_for(done, timeout, every=2, what="broadcast results")


class Oan:
    def __init__(self, token: str) -> None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False  # loopback port of the edge; the stack's test CA
        ctx.verify_mode = ssl.CERT_NONE
        self.c = httpx.Client(
            base_url=OAN, timeout=20, verify=ctx, headers={"Authorization": f"Bearer {token}"}
        )

    def sms(self, phone: str, text: str, purpose: str = "alert") -> dict:
        r = self.c.post(
            "/api/v1/messages/sms",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "recipient": phone,
                "text": text,
                "purpose": purpose,
                "source_reference": f"e2e:{uuid.uuid4().hex[:8]}",
            },
        )
        assert r.status_code == 202, r.text
        return r.json()

    def get(self, message_id: str) -> dict:
        r = self.c.get(f"/api/v1/messages/{message_id}")
        r.raise_for_status()
        return r.json()


class Fake8028:
    def __init__(self) -> None:
        self.c = httpx.Client(base_url=SMS, timeout=20)

    def sent(self, phone: str | None = None) -> list[dict]:
        params = {"phone": phone} if phone else {}
        return self.c.get("/_test/sent", params=params).json()["messages"]

    def reset(self) -> None:
        self.c.post("/_test/reset").raise_for_status()


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="session")
def stack_env() -> dict[str, str]:
    if not (STACK / ".env").exists():
        pytest.skip("local stack not set up (telegram/stack/init-env.sh, up.sh)")
    try:
        httpx.get(f"{TG}/_test/health", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("local stack not running (telegram/stack/up.sh)")
    return env()


@pytest.fixture(scope="session")
def tg(stack_env: dict[str, str]) -> FakeTelegram:
    t = FakeTelegram()
    if not t.c.get("/_test/webhook").json()["registered"]:
        # The fake keeps its webhook in memory; set it again after a restart.
        compose("--profile", "setup", "run", "--rm", "-T", "tg-setup")
    hook = t.c.get("/_test/webhook").json()
    assert hook == {"registered": True, "secret": True, "allowed_updates": ["message"]}, hook
    t.down(False)
    t.reset()
    for chat in SEED_AGENT_CHATS:
        t.user(chat)
    return t


@pytest.fixture(scope="session")
def stubs(stack_env: dict[str, str]) -> Stubs:
    s = Stubs(stack_env["CONTACT_REF_KEY"])
    s.reset()
    return s


@pytest.fixture(scope="session")
def bot(stack_env: dict[str, str]) -> BotServices:
    return BotServices(stack_env["BOT_BROADCAST_TOKEN"])


@pytest.fixture(scope="session")
def oan(stack_env: dict[str, str]) -> Oan:
    return Oan(stack_env["OAN_MSG_SMS_TEST_TOKEN"])


@pytest.fixture(scope="session")
def sms8028(stack_env: dict[str, str]) -> Fake8028:
    f = Fake8028()
    f.reset()
    return f


_used: set[int] = set()


@pytest.fixture
def new_chat(tg: FakeTelegram) -> Iterator[Callable[[], int]]:
    """A fresh Telegram user id for this test (not a phone number)."""

    def make() -> int:
        while True:
            tid = random.randint(7_100_000_000, 7_199_999_999)
            if tid not in _used:
                _used.add(tid)
                return tg.user(tid)

    yield make
    tg.down(False)


# ------------------------------------------------------------------ journeys


def start(tg: FakeTelegram, tid: int) -> None:
    """/start, English, privacy notice with the main menu."""
    tg.say(tid, "/start")
    tg.say(tid, "English")


def link_farmer(tg: FakeTelegram, stubs: Stubs, tid: int, farmer_id: str) -> None:
    start(tg, tid)
    tg.say(tid, "Link my Farmer ID")
    tg.say(tid, farmer_id)
    code = share_contact(tg, stubs, tid)["code"]
    done = tg.say(tid, code)[0]
    assert done["text"].startswith("Your Farmer ID is linked"), done["text"]


def share_contact(tg: FakeTelegram, stubs: Stubs, tid: int, user_id: int | None = None) -> dict:
    """Tap Share my phone number; returns the code record Auth "sent"."""
    up = tg.send(tid, contact={"user_id": user_id or tid, "phone_number": "+251900000999"})
    reply = tg.wait(tid)[0]
    assert "We sent a code by SMS" in reply["text"], reply["text"]
    return stubs.otp(tid, up["update_id"])


def copies(tg: FakeTelegram, tid: int, title: str) -> list[dict]:
    return [m for m in tg.sent(tid) if (m.get("text") or "").startswith(title)]


def dump(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)[:500]
