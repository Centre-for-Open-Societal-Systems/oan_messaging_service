"""Contract 3.4.0: the Telegram adapter sends ``reply_markup`` only when the message has
buttons; a message without buttons sends exactly the pre-3.4.0 body. No network."""

from __future__ import annotations

import json
import socket
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from oan_messaging.adapters.telegram import LINK_PREVIEW_OFF, TelegramAdapter, TelegramBotRegistry
from oan_messaging.contracts import (
    ProviderOutcome,
    TelegramRemoveKeyboard,
    TelegramReplyKeyboard,
    TelegramSendRequest,
)

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijk"  # noqa: S105 (fake test token)
NOW = datetime(2026, 9, 1, 5, 12, 13, tzinfo=UTC)
CHAT = 555000111


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("unexpected outbound network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def adapter() -> tuple[TelegramAdapter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 7, "date": 1788246733,
                                          "chat": {"id": CHAT, "type": "private"}}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = TelegramBotRegistry({"grievance": SecretStr(FAKE_TOKEN)})
    return TelegramAdapter(registry, client, clock=lambda: NOW), seen


def request(**kw: Any) -> TelegramSendRequest:
    return TelegramSendRequest(
        message_id=uuid.uuid4(), attempt=1, bot="grievance", chat_id=CHAT, text="Hello", **kw
    )


async def test_without_buttons_the_body_is_exactly_as_before() -> None:
    a, seen = adapter()
    result = await a.send(request())
    assert result.outcome is ProviderOutcome.SENT
    assert json.loads(seen[0].content) == {
        "chat_id": CHAT, "text": "Hello", "link_preview_options": LINK_PREVIEW_OFF
    }


async def test_reply_keyboard_is_sent_as_bot_api_reply_markup() -> None:
    a, seen = adapter()
    markup = TelegramReplyKeyboard.model_validate(
        {"keyboard": [[{"text": "Share my phone number", "request_contact": True}],
                      [{"text": "Yes"}, {"text": "No"}]],
         "one_time": False}
    )
    result = await a.send(request(reply_markup=markup))
    assert result.outcome is ProviderOutcome.SENT
    body = json.loads(seen[0].content)
    assert body["reply_markup"] == {
        "keyboard": [[{"text": "Share my phone number", "request_contact": True}],
                     [{"text": "Yes"}, {"text": "No"}]],
        "one_time_keyboard": False,
        "resize_keyboard": True,
    }
    assert body["text"] == "Hello" and "parse_mode" not in body


async def test_remove_keyboard_is_sent() -> None:
    a, seen = adapter()
    await a.send(request(reply_markup=TelegramRemoveKeyboard(remove_keyboard=True)))
    assert json.loads(seen[0].content)["reply_markup"] == {"remove_keyboard": True}


def test_buttons_never_appear_in_the_request_repr() -> None:
    markup = TelegramReplyKeyboard.model_validate({"keyboard": [[{"text": "Secret-ish"}]]})
    assert "Secret-ish" not in repr(request(reply_markup=markup))
