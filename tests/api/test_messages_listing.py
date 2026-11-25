"""GET /api/v1/messages: the read-only listing behind the staff console (real PostgreSQL).

A reader token (created with ``can_list_messages``) sees every caller's messages, newest
first, masked and without text, each with its callbacks. Senders and statistics readers
may not list.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from conftest import (
    T0,
    FakeClock,
    Secrets,
    headers,
    make_caller,
    make_client,
    new_key,
    sms_body,
    tg_body,
)
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oan_messaging.contracts import Channel, Purpose
from oan_messaging.storage.credentials import issue_credential, upsert_caller
from oan_messaging.storage.listing import (
    ListQueryError,
    MessageListing,
    decode_cursor,
    encode_cursor,
    parse_limit,
)
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.storage.schema import receipts

# -------------------------------------------------------------------- pure helpers


def test_cursor_round_trip() -> None:
    mid = uuid.uuid4()
    assert decode_cursor(encode_cursor(T0, mid)) == (T0, mid)


@pytest.mark.parametrize("bad", ["", "!!!", "e30", "WyJ4IiwgInkiXQ"])
def test_bad_cursor_is_refused(bad: str) -> None:
    with pytest.raises(ListQueryError):
        decode_cursor(bad)


def test_limit_bounds() -> None:
    assert parse_limit(None) == 50
    assert parse_limit("100") == 100
    for bad in ("0", "101", "-1", "x"):
        with pytest.raises(ListQueryError):
            parse_limit(bad)


# -------------------------------------------------------------------- over HTTP


async def reader_token(
    sf: async_sessionmaker[AsyncSession], keys: Secrets, caller: str = "staff-console"
) -> str:
    await upsert_caller(sf, caller, channels=[], purposes=[], can_list_messages=True)
    return await issue_credential(sf, keys.pepper, caller, T0)


def listing_client(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
) -> httpx.AsyncClient:
    return make_client(repo, sf, keys, clock, listing=MessageListing(sf))


async def send_sms(c: httpx.AsyncClient, token: str, ref: str) -> str:
    r = await c.post(
        "/api/v1/messages/sms",
        headers=headers(token, new_key()),
        json=sms_body(source_reference=ref, purpose="transactional", expires_at=None),
    )
    assert r.status_code == 202, r.text
    return str(r.json()["id"])


async def test_reader_lists_every_callers_messages_without_content(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
) -> None:
    sender_a = await make_caller(sf, keys, "grievance")
    sender_b = await make_caller(sf, keys, "credit", bot=None, channels=frozenset({Channel.SMS}))
    reader = await reader_token(sf, keys)
    async with listing_client(repo, sf, keys, clock) as c:
        first = await send_sms(c, sender_a, "grievance:G-1:u1")
        clock.advance(seconds=5)
        second = await send_sms(c, sender_b, "credit:L-9:u1")
        r = await c.get("/api/v1/messages", headers=headers(reader))
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {i["id"] for i in body["items"]}
    assert {first, second} <= ids
    for item in body["items"]:
        assert item["caller_id"] in {"grievance", "credit"}
        assert item["callbacks"] == [] and item["attempt_log"] == []
        text = str(item)
        assert "Your OAN code" not in text and "0900000001" not in text
        assert "recipient_masked" in item
    assert "next_cursor" not in body


async def test_callbacks_are_listed_with_their_message(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
) -> None:
    sender = await make_caller(sf, keys, "grievance")
    reader = await reader_token(sf, keys)
    async with listing_client(repo, sf, keys, clock) as c:
        mid = await send_sms(c, sender, "grievance:G-2:u1")
        async with sf() as s, s.begin():
            await s.execute(
                insert(receipts).values(
                    id=uuid.uuid4(),
                    provider="sms8028",
                    provider_message_id="8028-1",
                    recipient_fp_version="v1",
                    recipient_fp_digest="0" * 64,
                    recipient_masked="+2519******01",
                    raw_status="Delivered",
                    event_time=T0,
                    received_at=T0,
                    disposition="recorded_no_change",
                    message_id=uuid.UUID(mid),
                )
            )
        r = await c.get("/api/v1/messages", headers=headers(reader), params={"channel": "sms"})
    item = next(i for i in r.json()["items"] if i["id"] == mid)
    assert [cb["raw_status"] for cb in item["callbacks"]] == ["Delivered"]
    assert item["callbacks"][0]["disposition"] == "recorded_no_change"
    assert "recipient_fp_digest" not in str(item)


async def test_paging_filters_and_validation(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
) -> None:
    sender = await make_caller(sf, keys, "grievance")
    reader = await reader_token(sf, keys)
    async with listing_client(repo, sf, keys, clock) as c:
        for n in range(3):
            await send_sms(c, sender, f"grievance:page:{n}")
            clock.advance(seconds=1)
        seen: list[str] = []
        cursor = None
        for _ in range(5):
            params = {"limit": "2", "source_reference": "grievance:page:"}
            if cursor:
                params["cursor"] = cursor
            r = await c.get("/api/v1/messages", headers=headers(reader), params=params)
            assert r.status_code == 200, r.text
            seen += [i["id"] for i in r.json()["items"]]
            cursor = r.json().get("next_cursor")
            if cursor is None:
                break
        assert len(seen) == 3 and len(set(seen)) == 3
        none = await c.get(
            "/api/v1/messages", headers=headers(reader), params={"status": "delivered"}
        )
        assert none.json()["items"] == []
        for bad in ({"status": "nope"}, {"channel": "fax"}, {"limit": "0"}, {"cursor": "zz"}):
            r = await c.get("/api/v1/messages", headers=headers(reader), params=bad)
            assert r.status_code == 422, (bad, r.text)


async def test_only_message_readers_may_list(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
) -> None:
    sender = await make_caller(sf, keys, "grievance")
    await upsert_caller(sf, "reports", channels=[], purposes=[], can_read_stats=True)
    stats = await issue_credential(sf, keys.pepper, "reports", T0)
    async with listing_client(repo, sf, keys, clock) as c:
        assert (await c.get("/api/v1/messages")).status_code == 401
        for token in (sender, stats):
            r = await c.get("/api/v1/messages", headers=headers(token))
            assert r.status_code == 403
            assert r.json()["errors"][0]["code"] == "listing_not_permitted"
        # A reader cannot send.
        reader = await reader_token(sf, keys)
        r = await c.post(
            "/api/v1/messages/telegram", headers=headers(reader, new_key()), json=tg_body()
        )
        assert r.status_code in (401, 403, 422)


async def test_a_reader_cannot_be_given_channels(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ValueError):
        await upsert_caller(
            sf, "x", channels=[Channel.SMS], purposes=[Purpose.OTP], can_list_messages=True
        )
