"""The live relay keeps the live-test rules: one Telegram request at a time, stop at the
first refusal, never change the bot, confirm an update only after the stack accepted it,
and never print the token."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

SPEC = importlib.util.spec_from_file_location(
    "live_relay", Path(__file__).resolve().parents[2] / "telegram/stack/tools/live_relay.py"
)
relay_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay_mod)

TOKEN = "123456:TEST-TOKEN-NOT-REAL"  # noqa: S105 (a made-up test value)
CHAT = 7100000001


def update(uid: int, text: str = "/start") -> dict:
    return {
        "update_id": uid,
        "message": {
            "message_id": uid,
            "date": 0,
            "text": text,
            "chat": {"id": CHAT, "type": "private"},
            "from": {"id": CHAT, "is_bot": False},
        },
    }


class FakeTelegram:
    def __init__(self, script: dict[str, list]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        method = req.url.path.rsplit("/", 1)[1]
        body = json.loads(req.content or b"{}")
        self.calls.append((method, body))
        status, payload = self.script[method].pop(0)
        return httpx.Response(status, json=payload)


class FakeStack:
    def __init__(self, gateway_status: list[int], sent: list[dict] | None = None) -> None:
        self.gateway_status = gateway_status
        self.delivered: list[int] = []
        self._sent = sent or []

    def add_chat(self, chat_id: int) -> int:
        return 0

    def sent(self, chat_id: int) -> list[dict]:
        return self._sent

    def deliver(self, up: dict) -> int:
        self.delivered.append(up["update_id"])
        return self.gateway_status.pop(0)

    def code(self, chat_id: int, update_id: int) -> str | None:
        return "123456"


def make(script: dict, stack: FakeStack) -> tuple[object, FakeTelegram]:
    fake = FakeTelegram(script)
    tg = relay_mod.Telegram(TOKEN, httpx.Client(transport=httpx.MockTransport(fake)))
    return relay_mod.Relay(tg, stack), fake


OK_ME = (200, {"ok": True, "result": {"username": "test_bot"}})
NO_HOOK = (200, {"ok": True, "result": {"url": "", "pending_update_count": 0}})


def test_stops_at_the_first_429_after_one_request():
    relay, fake = make(
        {
            "getMe": [
                (
                    429,
                    {
                        "ok": False,
                        "description": "Too Many Requests: retry after 32",
                        "parameters": {"retry_after": 32},
                    },
                )
            ]
        },
        FakeStack([]),
    )
    with pytest.raises(relay_mod.Stop, match="wait 32 s"):
        relay.start()
    assert [m for m, _ in fake.calls] == ["getMe"]


def test_never_changes_the_webhook_and_stops_if_one_is_set():
    relay, fake = make(
        {
            "getMe": [OK_ME],
            "getWebhookInfo": [(200, {"ok": True, "result": {"url": "https://x.example.invalid"}})],
        },
        FakeStack([]),
    )
    with pytest.raises(relay_mod.Stop, match="webhook is set"):
        relay.start()
    assert [m for m, _ in fake.calls] == ["getMe", "getWebhookInfo"]


def test_update_confirmed_only_after_the_stack_accepted_it():
    stack = FakeStack([500, 200])
    relay, fake = make(
        {
            "getMe": [OK_ME],
            "getWebhookInfo": [NO_HOOK],
            "getUpdates": [
                (200, {"ok": True, "result": [update(10)]}),
                (200, {"ok": True, "result": [update(10)]}),
            ],
        },
        stack,
    )
    relay.start()
    relay.poll_once()
    assert relay.offset == 0  # the stack refused: not confirmed
    relay.poll_once()
    assert relay.offset == 11
    offsets = [b["offset"] for m, b in fake.calls if m == "getUpdates"]
    assert offsets == [0, 0]
    assert stack.delivered == [10, 10]


def test_replies_are_sent_on_one_at_a_time_with_buttons():
    sent = [
        {
            "message_id": 1,
            "text": "Choose your language.",
            "buttons": [["English"]],
            "reply_markup": {"keyboard": [[{"text": "English"}]], "resize_keyboard": True},
        }
    ]
    relay, fake = make({"sendMessage": [(200, {"ok": True, "result": {}})]}, FakeStack([], sent))
    relay.chats[CHAT] = 0
    relay.send_once()
    relay.send_once()  # nothing new: no second send
    sends = [b for m, b in fake.calls if m == "sendMessage"]
    assert len(sends) == 1 and sends[0]["reply_markup"]["keyboard"][0][0]["text"] == "English"


def test_a_connection_error_stops_without_the_token_in_the_message():
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed for {req.url}")

    tg = relay_mod.Telegram(TOKEN, httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(relay_mod.Stop) as exc:
        tg.call("getMe")
    assert TOKEN not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
