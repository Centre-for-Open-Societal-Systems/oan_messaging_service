"""``POST /broadcasts`` and its reads, sent through OAN Messaging (§6.8, SMS-02 revised)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from botenv import BODY, BROADCAST_TOKEN, Bot, farmers_broadcast, link_farmer
from botsupport import new_contact, rid, sensitive
from sqlalchemy import text

from oan_bot_services import broadcasts as bc


async def seed_farmers(bot: Bot) -> dict[str, dict]:
    """Woreda ET010101: 3 farmers; ET010102: 1; another zone: 1. One has updates off."""
    out = {}
    plan = [
        ("A1", "ET01010101"),
        ("A2", "ET01010102"),
        ("A3", "ET01010101"),
        ("B1", "ET01010201"),
        ("Z1", "ET02010101"),
        ("OFF", "ET01010101"),
    ]
    for i, (name, kebele) in enumerate(plan):
        c = new_contact(700000300 + i)
        await link_farmer(bot, c, f"FR-TEST-03{i:02d}", f"+2519000003{i:02d}", kebele)
        sensitive(f"FR-TEST-03{i:02d}")
        out[name] = c
    await bot.flow(
        "PUT", "/contacts/updates", {"request_id": rid(), "contact": out["OFF"], "updates": "off"}
    )
    return out


async def run_jobs(bot: Bot) -> None:
    await bc.submit_pending(bot.deps)
    bot.clock.now += timedelta(seconds=10)
    await bc.poll_results(bot.deps)


async def test_farmers_of_a_zone_in_woreda_round_robin(bot: Bot) -> None:
    f = await seed_farmers(bot)
    r = await farmers_broadcast(bot, "zone", "ET0101")
    assert r.status_code == 202
    out = r.json()
    assert out["recipients"] == 4 and out["not_on_telegram"] == []
    rows = await bot.q("SELECT n, woreda_code, chat_id FROM broadcast_recipients ORDER BY n")
    assert [w for _, w, _ in rows] == ["ET010101", "ET010102", "ET010101", "ET010101"]
    assert f["OFF"]["telegram_id"] not in [c for _, _, c in rows]
    assert f["Z1"]["telegram_id"] not in [c for _, _, c in rows]
    await bc.submit_pending(bot.deps)
    sent = sorted(bot.up.oan_messages.values(), key=lambda m: m["body"]["source_reference"])
    assert len(sent) == 4
    m = sent[0]["body"]
    assert m["text"] == "Heavy rain\n\nExpect heavy rain on Friday. Cover the harvest."
    assert m["purpose"] == "alert"  # Weather -> alert (SMS-06)
    assert m["source_reference"] == f"bc:{out['broadcast_id']}:1"
    # Chat ids are cleared once OAN Messaging has the message; keys stay.
    assert await bot.q("SELECT count(*) FROM broadcast_recipients WHERE chat_id IS NOT NULL") == [
        (0,)
    ]
    keys = {m["key"] for m in bot.up.oan_messages.values()}
    db_keys = {str(k) for (k,) in await bot.q("SELECT idempotency_key FROM broadcast_recipients")}
    assert keys == db_keys


async def test_progress_counts_and_telegram_recipients(bot: Bot) -> None:
    f = await seed_farmers(bot)
    bid = (await farmers_broadcast(bot, "woreda", "ET010101")).json()["broadcast_id"]
    r = await bot.bc("GET", f"/broadcasts/{bid}")
    assert r.json() == {
        "broadcast_id": bid,
        "state": "running",
        "counts": {"accepted": 3, "sending": 0, "sent": 0, "failed": 0, "expired": 0, "unknown": 0},
    }
    await bc.submit_pending(bot.deps)
    bot.up.settle("sent", chat_id=f["A1"]["telegram_id"])
    bot.clock.now += timedelta(seconds=10)
    await bc.poll_results(bot.deps)
    r = await bot.bc("GET", f"/broadcasts/{bid}")
    assert r.json()["state"] == "running" and r.json()["counts"]["accepted"] == 2
    bot.up.settle("sent", chat_id=f["A2"]["telegram_id"])
    bot.up.settle("failed", "recipient_blocked_bot", chat_id=f["A3"]["telegram_id"])
    bot.clock.now += timedelta(seconds=10)
    await bc.poll_results(bot.deps)
    r = await bot.bc("GET", f"/broadcasts/{bid}")
    assert r.json()["state"] == "finished"
    assert r.json()["counts"]["sent"] == 2 and r.json()["counts"]["failed"] == 1
    r = await bot.bc("GET", f"/broadcasts/{bid}/telegram-recipients")
    assert set(r.json()["farmer_ids"]) == {"FR-TEST-0300", "FR-TEST-0301"}
    assert r.json()["next_cursor"] is None
    # TG-09: the blocked farmer is skipped next time.
    assert await bot.q(
        "SELECT state FROM subscriptions WHERE chat_id = :c", c=f["A3"]["telegram_id"]
    ) == [("unreachable",)]
    again = await farmers_broadcast(bot, "woreda", "ET010101")
    assert again.json()["recipients"] == 2


async def test_results_are_polled_at_most_every_ten_seconds(bot: Bot) -> None:
    await seed_farmers(bot)
    await farmers_broadcast(bot, "kebele", "ET01010102")
    await bc.submit_pending(bot.deps)
    bot.up.settle("sending")
    assert await bc.poll_results(bot.deps) == 0
    gets = sum(1 for m, h, p in bot.up.calls if m == "GET" and p.startswith("/api/v1/messages"))
    bot.up.settle("sent")
    await bc.poll_results(bot.deps)  # too soon
    assert (
        sum(1 for m, h, p in bot.up.calls if m == "GET" and p.startswith("/api/v1/messages"))
        == gets
    )
    bot.clock.now += timedelta(seconds=10)
    assert await bc.poll_results(bot.deps) == 1


@pytest.mark.parametrize(
    ("status", "code", "counted", "unreachable"),
    [
        ("sent", None, "sent", False),
        ("delivered", None, "sent", False),
        ("failed", "recipient_blocked_bot", "failed", True),
        ("failed", "chat_not_found", "failed", True),
        ("failed", "text_rejected", "failed", False),
        ("failed", "provider_rejected", "failed", False),
        ("expired", "expired_before_send", "expired", False),
        ("unknown", "timeout", "unknown", False),
    ],
)
def test_one_table_maps_every_oan_result(
    status: str, code: str | None, counted: str, unreachable: bool
) -> None:
    assert bc.map_result(status, code) == (counted, unreachable)


def test_unreachable_codes_are_oan_failure_codes() -> None:
    from oan_messaging.contracts import FailureCode

    assert set(bc.UNREACHABLE) <= {c.value for c in FailureCode}
    assert bc.UNREACHABLE == {"recipient_blocked_bot": "blocked", "chat_not_found": "chat_deleted"}


async def test_idempotency_replay_and_conflict(bot: Bot) -> None:
    await seed_farmers(bot)
    key = "client-op-" + uuid.uuid4().hex
    first = await farmers_broadcast(bot, "woreda", "ET010102", key=key)
    assert first.status_code == 202
    again = await farmers_broadcast(bot, "woreda", "ET010102", key=key)
    assert again.status_code == 200 and again.json() == first.json()
    assert await bot.q("SELECT count(*) FROM broadcasts") == [(1,)]
    other = await farmers_broadcast(bot, "woreda", "ET010102", key=key, title="Other")
    assert other.status_code == 409
    assert other.json()["code"] == "idempotency_conflict"
    assert other.headers["content-type"].startswith("application/problem+json")
    # The binding stores fingerprints only.
    [(key_fp, body_fp)] = await bot.q("SELECT key_fp, body_fp FROM broadcast_bindings")
    assert key not in key_fp and len(key_fp) == len(body_fp) == 64


async def test_development_agents_from_the_roster_minus_the_sender(bot: Bot) -> None:
    agent = {"role": "development_agent", "display_name": "Test", "telegram_linked": True}
    sensitive(700000401, 700000402)
    bot.up.roster = {
        "ET01010101": [
            {**agent, "ref": "DA-1", "telegram_id": 700000401},
            {**agent, "ref": "DA-SENDER", "telegram_id": 700000402},
            {**agent, "ref": "DA-OFF", "telegram_linked": False},
        ],
        "ET01010102": [
            {**agent, "ref": "DA-1", "telegram_id": 700000401},
            {**agent, "ref": "DA-2", "telegram_id": 700000403},
        ],
        "ET01010201": [{**agent, "ref": "DA-3", "telegram_id": 700000404}],
    }
    sensitive(700000403, 700000404)
    body = {
        "recipients": "das",
        "scope": {"level": "zone", "code": "ET0101"},
        **BODY,
        "sender_ref": "DA-SENDER",
        "source": "Informational",
    }
    r = await bot.bc("POST", "/broadcasts", key=str(uuid.uuid4()), body=body)
    assert r.status_code == 202
    assert r.json()["recipients"] == 3 and r.json()["not_on_telegram"] == ["DA-OFF"]
    await bc.submit_pending(bot.deps)
    assert {m["body"]["purpose"] for m in bot.up.oan_messages.values()} == {"broadcast"}
    bot.up.settle("failed", "chat_not_found", chat_id=700000404)
    bot.clock.now += timedelta(seconds=10)
    await bc.poll_results(bot.deps)
    assert bot.up.unreachable == [("DA-3", "chat_deleted")]
    # Kebele scope: one roster call for the one kebele.
    body["scope"] = {"level": "kebele", "code": "ET01010102"}
    r = await bot.bc("POST", "/broadcasts", key=str(uuid.uuid4()), body=body)
    assert r.json()["recipients"] == 2


async def test_r3_is_retried_when_the_da_backend_is_down(bot: Bot) -> None:
    sensitive(700000405)
    bot.up.roster = {
        "ET01010101": [
            {
                "ref": "DA-9",
                "role": "development_agent",
                "display_name": "T",
                "telegram_linked": True,
                "telegram_id": 700000405,
            }
        ]
    }
    body = {"recipients": "das", "scope": {"level": "kebele", "code": "ET01010101"}, **BODY}
    await bot.bc("POST", "/broadcasts", key=str(uuid.uuid4()), body=body)
    await bc.submit_pending(bot.deps)
    bot.up.settle("failed", "recipient_blocked_bot")
    bot.up.down.add("da")
    bot.clock.now += timedelta(seconds=10)
    await bc.poll_results(bot.deps)
    assert bot.up.unreachable == []
    bot.up.down.discard("da")
    assert await bc.retry_unreachable(bot.deps) == 1
    assert bot.up.unreachable == [("DA-9", "blocked")]


@pytest.mark.parametrize(
    ("over", "field", "code"),
    [
        ({"body": "x" * 4090}, "body", "too_long"),
        ({"body": "tab\there"}, "body", "control_characters"),
        ({"title": "", "body": " "}, "body", "blank"),
        ({"expires_at": "2026-11-30T08:00:00+03:00"}, "expires_at", "in_past"),
        ({"expires_at": "2026-12-09T08:00:00+03:00"}, "expires_at", "too_far"),
        ({"expires_at": "2026-12-02T08:00:00"}, "expires_at", "timezone_aware"),
        ({"source": "Gossip"}, "source", "literal_error"),
        ({"title": "t" * 141}, "title", "string_too_long"),
        ({"evidence": []}, None, "unknown_field"),
    ],
)
async def test_validation_is_value_free(bot: Bot, over: dict, field: str | None, code: str) -> None:
    r = await farmers_broadcast(bot, "woreda", "ET010101", **over)
    assert r.status_code == 422, r.text
    item = {"code": code} if field is None else {"field": field, "code": code}
    assert item in r.json()["errors"]
    assert "x" * 50 not in r.text and "tab" not in r.text
    assert await bot.q("SELECT count(*) FROM broadcasts") == [(0,)]


async def test_text_at_the_limit_is_accepted(bot: Bot) -> None:
    title = "Heavy rain"
    body = "y" * (4096 - len(title) - 2)
    r = await farmers_broadcast(bot, "woreda", "ET010101", title=title, body=body)
    assert r.status_code == 202


async def test_auth_media_type_and_missing_key(bot: Bot) -> None:
    body = {"recipients": "farmers", "scope": {"level": "woreda", "code": "ET010101"}, **BODY}
    for token in (None, "wrong"):
        r = await bot.bc("POST", "/broadcasts", key="k1", body=body, token=token)
        assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
    r = await bot.bc("POST", "/broadcasts", body=body)
    assert r.status_code == 422 and r.json()["errors"] == [
        {"field": "header.Idempotency-Key", "code": "required"}
    ]

    r = await bot.api.post(
        "/broadcasts",
        content=b"x=1",
        headers={
            "Authorization": f"Bearer {BROADCAST_TOKEN}",
            "Idempotency-Key": "k2",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert r.status_code == 415
    r = await bot.api.post(
        "/broadcasts",
        content=b"{",
        headers={
            "Authorization": f"Bearer {BROADCAST_TOKEN}",
            "Idempotency-Key": "k3",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 422
    r = await bot.api.post(
        "/broadcasts",
        content=b"[" + b"1," * 40000 + b"1]",
        headers={
            "Authorization": f"Bearer {BROADCAST_TOKEN}",
            "Idempotency-Key": "k4",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 413
    assert (await bot.bc("GET", "/broadcasts/nope")).status_code == 404
    assert (await bot.bc("GET", f"/broadcasts/{uuid.uuid4()}")).status_code == 404
    assert (
        await bot.bc("GET", f"/broadcasts/{uuid.uuid4()}/telegram-recipients")
    ).status_code == 404
    assert (await bot.bc("GET", f"/broadcasts/{uuid.uuid4()}", token=None)).status_code == 401


async def test_upstream_down_gives_service_unavailable(bot: Bot) -> None:
    bot.up.down.add("master-data")
    body = {"recipients": "das", "scope": {"level": "woreda", "code": "ET010101"}, **BODY}
    r = await bot.api.post(
        "/broadcasts",
        json=body,
        headers={
            "Authorization": f"Bearer {BROADCAST_TOKEN}",
            "Idempotency-Key": "k5",
        },
    )
    assert r.status_code == 503 and r.json()["code"] == "service_unavailable"


async def test_no_recipients_finishes_at_once(bot: Bot) -> None:
    r = await farmers_broadcast(bot, "region", "ET02")
    bid = r.json()["broadcast_id"]
    assert r.json()["recipients"] == 0
    assert (await bot.bc("GET", f"/broadcasts/{bid}")).json()["state"] == "finished"


async def test_expiry_default_and_pending_copies_expire(bot: Bot) -> None:
    await seed_farmers(bot)
    await farmers_broadcast(bot, "woreda", "ET010102")
    [(expires,)] = await bot.q("SELECT expires_at FROM broadcasts")
    assert expires == bot.clock.now + timedelta(hours=24)
    bot.clock.now += timedelta(hours=25)
    await run_jobs(bot)
    assert bot.up.oan_messages == {}
    assert await bot.q("SELECT status FROM broadcast_recipients") == [("expired",)]
    assert await bot.q("SELECT state, text IS NULL FROM broadcasts") == [("finished", True)]


async def test_oan_messaging_down_then_back_reuses_the_same_keys(bot: Bot) -> None:
    await seed_farmers(bot)
    await farmers_broadcast(bot, "woreda", "ET010101")
    bot.up.down.add("oan")
    assert await bc.submit_pending(bot.deps) == 0
    assert await bot.q("SELECT count(*) FROM broadcast_recipients WHERE status = 'pending'") == [
        (3,)
    ]
    bot.up.down.discard("oan")
    bot.deps.oan.http.breaker.success()
    assert await bc.submit_pending(bot.deps) == 3
    # A repeat submit with the same key is a replay at OAN Messaging, never a second message.
    async with bot.deps.engine.begin() as conn:
        await conn.execute(
            text("UPDATE broadcast_recipients SET status = 'pending', chat_id = 700000300")
        )
        await conn.execute(
            text("UPDATE broadcasts SET text = :t"),
            {"t": bot.deps.keys.seal("Heavy rain\n\nx", "broadcast")},
        )
    await bc.submit_pending(bot.deps)
    assert len(bot.up.oan_messages) == 3


async def test_oan_refusal_is_failed_not_retried(bot: Bot) -> None:
    await seed_farmers(bot)
    await farmers_broadcast(bot, "kebele", "ET01010102")
    bot.up.oan_auth_bad = True  # type: ignore[attr-defined]
    orig = bot.up._oan

    def refuse(request: Any, path: str, body: Any) -> Any:
        import httpx

        return httpx.Response(422, json={"status": 422, "code": "validation_failed"})

    bot.up._oan = refuse  # type: ignore[method-assign]
    await bc.submit_pending(bot.deps)
    bot.up._oan = orig  # type: ignore[method-assign]
    assert await bot.q("SELECT status, failure_code FROM broadcast_recipients") == [
        ("failed", "validation_failed")
    ]


async def test_unknown_message_at_oan_counts_unknown(bot: Bot) -> None:
    await seed_farmers(bot)
    await farmers_broadcast(bot, "kebele", "ET01010102")
    await bc.submit_pending(bot.deps)
    bot.up.oan_messages.clear()
    bot.clock.now += timedelta(seconds=10)
    await bc.poll_results(bot.deps)
    assert await bot.q("SELECT status FROM broadcast_recipients") == [("unknown",)]


async def test_telegram_recipients_pages(bot: Bot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bc, "PAGE_SIZE", 2)
    f = await seed_farmers(bot)
    bid = (await farmers_broadcast(bot, "zone", "ET0101")).json()["broadcast_id"]
    await run_jobs(bot)
    bot.up.settle("sent")
    bot.clock.now += timedelta(seconds=10)
    await bc.poll_results(bot.deps)
    ids: list[str] = []
    cursor = None
    while True:
        params = {"cursor": cursor} if cursor else None
        page = (await bot.bc("GET", f"/broadcasts/{bid}/telegram-recipients", params=params)).json()
        ids += page["farmer_ids"]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(ids) == 4 and len(set(ids)) == 4
    assert f
    bad = await bot.bc("GET", f"/broadcasts/{bid}/telegram-recipients", params={"cursor": "!!"})
    assert bad.status_code == 422
