"""Contract 3.4.0 over HTTP (real PostgreSQL): the Telegram-only broadcast switch at the
API, reply buttons on the Telegram endpoint, and the cancel route."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import (
    FakeClock,
    Secrets,
    headers,
    make_caller,
    make_client,
    new_key,
    sms_body,
    tg_body,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oan_messaging.contracts import Channel, MessageStatusView
from oan_messaging.storage.repository import PostgresMessageRepository

SHARE = {"keyboard": [[{"text": "Share my phone number", "request_contact": True}]]}


def problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["status"] == status and body["code"] == code
    return body


# -------------------------------------------------------------------- broadcast switch


@pytest.mark.parametrize(
    ("both", "tg_only", "sms_status", "tg_status"),
    [
        (False, True, 403, 202),  # the new switch: Telegram only
        (True, False, 202, 202),  # the old switch: both, as before
        (True, True, 202, 202),
        (False, False, 403, 403),
    ],
    ids=["telegram-switch", "old-switch", "both", "off"],
)
async def test_broadcast_switches_at_the_api(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
    both: bool,
    tg_only: bool,
    sms_status: int,
    tg_status: int,
) -> None:
    token = await make_caller(sf, keys, "da-kit")
    async with make_client(
        repo, sf, keys, clock, broadcast_enabled=both, telegram_broadcast_enabled=tg_only
    ) as c:
        sms = await c.post(
            "/api/v1/messages/sms",
            json=sms_body(purpose="broadcast", expires_at=None),
            headers=headers(token, new_key()),
        )
        tg = await c.post(
            "/api/v1/messages/telegram",
            json=tg_body(purpose="broadcast"),
            headers=headers(token, new_key()),
        )
    assert (sms.status_code, tg.status_code) == (sms_status, tg_status), (sms.text, tg.text)
    if sms_status == 403:
        assert problem(sms, 403, "forbidden")["errors"] == [
            {"field": "purpose", "code": "purpose_not_permitted"}
        ]


async def test_telegram_switch_does_not_widen_caller_purposes(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
) -> None:
    from oan_messaging.contracts import Purpose

    token = await make_caller(sf, keys, "bot", purposes=frozenset({Purpose.TRANSACTIONAL}))
    async with make_client(
        repo, sf, keys, clock, broadcast_enabled=False, telegram_broadcast_enabled=True
    ) as c:
        resp = await c.post(
            "/api/v1/messages/telegram",
            json=tg_body(purpose="broadcast"),
            headers=headers(token, new_key()),
        )
    problem(resp, 403, "forbidden")


# ----------------------------------------------------------------------- reply buttons


@pytest.mark.parametrize(
    "markup",
    [
        SHARE,
        {"remove_keyboard": True},
        {"keyboard": [[{"text": "a"}] * 3] * 4},  # the maximum: 4 rows of 3
        {"keyboard": [[{"text": "x" * 30}]]},  # 30 UTF-16 units
        {"keyboard": [[{"text": "ሀ" * 30}]]},  # 30 Ethiopic letters = 30 units
        {"keyboard": [[{"text": "😀" * 15}]]},  # 15 surrogate pairs = 30 units
        {"keyboard": [[{"text": "Yes"}, {"text": "No"}]], "one_time": False, "resize": False},
    ],
    ids=["share", "remove", "4x3", "30-ascii", "30-ethiopic", "15-emoji", "flags"],
)
async def test_valid_reply_markup_is_accepted(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    markup: dict[str, Any],
) -> None:
    token = await make_caller(sf, keys, "bot")
    resp = await client.post(
        "/api/v1/messages/telegram",
        json=tg_body(reply_markup=markup),
        headers=headers(token, new_key()),
    )
    assert resp.status_code == 202, resp.text


@pytest.mark.parametrize(
    ("markup", "code"),
    [
        ({"keyboard": []}, "too_short"),
        ({"keyboard": [[{"text": "a"}]] * 5}, "too_long"),
        ({"keyboard": [[]]}, "too_short"),
        ({"keyboard": [[{"text": "a"}] * 4]}, "too_long"),
        ({"keyboard": [[{"text": ""}]]}, "blank"),
        ({"keyboard": [[{"text": "   "}]]}, "blank"),
        ({"keyboard": [[{"text": "x" * 31}]]}, "invalid"),
        ({"keyboard": [[{"text": "😀" * 15 + "a"}]]}, "invalid"),
        ({"keyboard": [[{"text": "line\nbreak"}]]}, "control_characters"),
        ({"keyboard": [[{"text": "tab\tx"}]]}, "control_characters"),
        ({"keyboard": [[{"text": "a", "request_contact": "true"}]]}, "bool_type"),
        ({"keyboard": [[{"text": "a", "url": "https://example.invalid"}]]}, "unknown_field"),
        ({"keyboard": [[{"text": "a"}]], "one_time": 1}, "bool_type"),
        ({"keyboard": [[{"text": "a"}]], "selective": True}, "unknown_field"),
        ({"keyboard": [[{"text": "a"}]], "remove_keyboard": True}, "unknown_field"),
        ({"remove_keyboard": False}, "literal_error"),
        ({}, "required"),
        ("buttons", "model_type"),
    ],
)
async def test_invalid_reply_markup_is_422_without_echo(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    markup: Any,
    code: str,
) -> None:
    token = await make_caller(sf, keys, "bot")
    resp = await client.post(
        "/api/v1/messages/telegram",
        json=tg_body(reply_markup=markup),
        headers=headers(token, new_key()),
    )
    body = problem(resp, 422, "validation_failed")
    assert body["errors"], body
    # The expected code is present; pydantic may add follow-on "too_short" items for a
    # row whose only button failed. All items are value-free.
    assert code in {e["code"] for e in body["errors"]}, body
    assert "example.invalid" not in resp.text and "line\\nbreak" not in resp.text


async def test_reply_markup_is_unknown_on_the_sms_endpoint(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "esignet")
    resp = await client.post(
        "/api/v1/messages/sms",
        json=sms_body(reply_markup=SHARE),
        headers=headers(token, new_key()),
    )
    assert problem(resp, 422, "validation_failed")["errors"] == [{"code": "unknown_field"}]


async def test_reply_markup_replay_and_conflict_over_http(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "bot")
    key = new_key()
    first = await client.post(
        "/api/v1/messages/telegram", json=tg_body(reply_markup=SHARE), headers=headers(token, key)
    )
    again = await client.post(
        "/api/v1/messages/telegram", json=tg_body(reply_markup=SHARE), headers=headers(token, key)
    )
    other = await client.post(
        "/api/v1/messages/telegram",
        json=tg_body(reply_markup={"remove_keyboard": True}),
        headers=headers(token, key),
    )
    assert (first.status_code, again.status_code) == (202, 200)
    assert again.json()["id"] == first.json()["id"]
    problem(other, 409, "idempotency_conflict")


# ------------------------------------------------------------------------------ cancel


async def submit(c: httpx.AsyncClient, token: str, path: str, body: dict[str, Any]) -> str:
    resp = await c.post(path, json=body, headers=headers(token, new_key()))
    assert resp.status_code == 202, resp.text
    return str(resp.json()["id"])


async def test_cancel_route_table(
    client: httpx.AsyncClient,
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
) -> None:
    token = await make_caller(sf, keys, "bot")
    other = await make_caller(sf, keys, "other")
    auth = {"Authorization": f"Bearer {token}"}
    tg_id = await submit(client, token, "/api/v1/messages/telegram", tg_body())
    sms_id = await submit(client, token, "/api/v1/messages/sms", sms_body())

    # 401 without a credential; nothing changes.
    problem(await client.post(f"/api/v1/messages/{tg_id}/cancel"), 401, "unauthenticated")
    # 404: another caller's message, an unknown id, not a UUID.
    for mid, tok in ((tg_id, other), ("0b9f3a52-5c1e-4d55-9b7a-3f1c2a7e9d10", token),
                     ("not-a-uuid", token)):
        resp = await client.post(
            f"/api/v1/messages/{mid}/cancel", headers={"Authorization": f"Bearer {tok}"}
        )
        problem(resp, 404, "not_found")
    # 422: an SMS message.
    body = problem(
        await client.post(f"/api/v1/messages/{sms_id}/cancel", headers=auth),
        422, "validation_failed",
    )
    assert body["errors"] == [{"field": "message_id", "code": "channel_not_supported"}]
    # 200: cancelled, a valid status view.
    clock.advance(seconds=2)
    first = await client.post(f"/api/v1/messages/{tg_id}/cancel", headers=auth)
    assert first.status_code == 200, first.text
    view = MessageStatusView.model_validate(first.json())
    assert (view.status.value, view.failure_code, view.final) == ("failed", "cancelled", True)
    assert view.channel is Channel.TELEGRAM and view.attempts == 0
    # 200 again: the same view.
    clock.advance(seconds=2)
    again = await client.post(f"/api/v1/messages/{tg_id}/cancel", headers=auth)
    assert again.status_code == 200 and again.json() == first.json()
    # GET shows the same.
    got = await client.get(f"/api/v1/messages/{tg_id}", headers=auth)
    assert got.json() == first.json()
    # The SMS message is untouched.
    sms_view = (await client.get(f"/api/v1/messages/{sms_id}", headers=auth)).json()
    assert sms_view["status"] == "accepted"

    # 409: a message a worker holds.
    held_id = await submit(client, token, "/api/v1/messages/telegram", tg_body(chat_id=555000222))
    claims = await repo.claim_due("w", clock(), __import__("datetime").timedelta(minutes=2), 5,
                                  channels=(Channel.TELEGRAM,))
    assert [str(c.message.id) for c in claims] == [held_id]
    resp = await client.post(f"/api/v1/messages/{held_id}/cancel", headers=auth)
    assert problem(resp, 409, "not_cancellable")["errors"] == [{"code": "not_cancellable"}]
    assert resp.json()["title"] == "Not cancellable"


async def test_cancel_needs_no_body_and_ignores_one(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "bot")
    tg_id = await submit(client, token, "/api/v1/messages/telegram", tg_body())
    resp = await client.post(
        f"/api/v1/messages/{tg_id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
        content=b"",
    )
    assert resp.status_code == 200, resp.text


async def test_get_on_cancel_path_is_not_allowed(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "bot")
    tg_id = await submit(client, token, "/api/v1/messages/telegram", tg_body())
    resp = await client.get(
        f"/api/v1/messages/{tg_id}/cancel", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code in (404, 405)
