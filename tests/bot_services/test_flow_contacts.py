"""Flow routes for contacts: location, updates, idle, stop (§5.4, §5.8)."""

from __future__ import annotations

import uuid

from botsupport import Farmer, minutes, new_contact, rid, sensitive
from conftest import Bot

from oan_bot_services import keyed
from oan_bot_services import text_keys as tk


async def link_farmer(
    bot: Bot,
    contact: dict,
    farmer_id: str = "FR-TEST-0001",
    phone: str = "+251900000101",
    kebele: str = "ET01010101",
) -> None:
    sensitive(farmer_id, phone, phone[-9:])
    bot.up.farmers[farmer_id] = Farmer(farmer_id, phone, kebele)
    r = await bot.flow(
        "POST",
        "/farmer-link/start",
        {
            "request_id": rid(),
            "contact": contact,
            "farmer_id": farmer_id,
            "shared_contact": {"user_id": contact["telegram_id"], "phone_number": phone},
        },
    )
    assert r.json()["result"] == "code_sent", r.json()
    r = await bot.flow(
        "POST",
        "/farmer-link/verify",
        {
            "request_id": rid(),
            "contact": contact,
            "link_request_id": r.json()["link_request_id"],
            "code": bot.up.otp_code,
        },
    )
    assert r.json()["result"] == "linked", r.json()


async def test_location_saves_kebele_and_returns_parents(bot: Bot) -> None:
    c = new_contact(700000101)
    r = await bot.flow(
        "POST",
        "/contacts/location",
        {"request_id": rid(), "contact": c, "kebele_code": "ET01010102", "language": "am"},
    )
    assert r.status_code == 200
    assert r.json() == {
        "kebele_code": "ET01010102",
        "woreda_code": "ET010101",
        "zone_code": "ET0101",
        "region_code": "ET01",
    }
    rows = await bot.q("SELECT kebele_code, language FROM contacts")
    assert rows == [("ET01010102", "am")]


async def test_location_unknown_or_not_a_kebele_is_an_invalid_choice(bot: Bot) -> None:
    c = new_contact(700000102)
    for code in ("ET99999999", "ET010101"):
        r = await bot.flow(
            "POST",
            "/contacts/location",
            {"request_id": rid(), "contact": c, "kebele_code": code, "language": "en"},
        )
        assert r.status_code == 422
        assert r.json() == {"error": "validation_failed", "text_key": tk.COMMON_INVALID_CHOICE}


async def test_location_serves_the_cached_area_when_master_data_is_down(bot: Bot) -> None:
    c = new_contact(700000103)
    body = {"contact": c, "kebele_code": "ET01010101", "language": "en"}
    assert (
        await bot.flow("POST", "/contacts/location", {"request_id": rid(), **body})
    ).status_code == 200
    bot.up.down.add("master-data")
    bot.clock.now += minutes(60 * 25)  # cache is stale, but it is all there is
    r = await bot.flow("POST", "/contacts/location", {"request_id": rid(), **body})
    assert r.status_code == 200 and r.json()["woreda_code"] == "ET010101"
    r = await bot.flow(
        "POST",
        "/contacts/location",
        {"request_id": rid(), "contact": c, "kebele_code": "ET01010201", "language": "en"},
    )
    assert r.status_code == 503
    assert r.json() == {"error": "unavailable", "text_key": tk.COMMON_UNAVAILABLE}


async def test_repeat_request_id_returns_the_first_answer_and_changes_nothing(bot: Bot) -> None:
    c = new_contact(700000104)
    body = {"request_id": rid(), "contact": c, "kebele_code": "ET01010101", "language": "en"}
    first = await bot.flow("POST", "/contacts/location", body)
    calls = len(bot.up.calls)
    again = await bot.flow("POST", "/contacts/location", body)
    assert again.json() == first.json() and len(bot.up.calls) == calls
    # The same request_id on another route is refused.
    other = await bot.flow(
        "PUT",
        "/contacts/updates",
        {"request_id": body["request_id"], "contact": c, "updates": "on"},
    )
    assert other.status_code == 422


async def test_flow_secret_is_required(bot: Bot) -> None:
    c = new_contact(700000105)
    body = {"contact": c}
    for secret in (None, "wrong-secret-value-0000"):
        r = await bot.flow("POST", "/contacts/idle", body, secret=secret)
        assert r.status_code == 401
        assert r.json() == {"error": "unauthenticated", "text_key": tk.COMMON_UNAVAILABLE}


async def test_unknown_fields_and_bad_values_are_refused(bot: Bot) -> None:
    c = new_contact(700000106)
    bad = [
        {"request_id": rid(), "contact": c, "updates": "maybe"},
        {"request_id": rid(), "contact": c, "updates": "on", "extra": 1},
        {"request_id": "not-a-uuid", "contact": c, "updates": "on"},
        {"request_id": rid(), "contact": {**c, "telegram_id": "12"}, "updates": "on"},
    ]
    for body in bad:
        r = await bot.flow("PUT", "/contacts/updates", body)
        assert r.status_code == 422 and r.json()["text_key"] == tk.COMMON_INVALID_CHOICE
    r = await bot.api.put(
        "/contacts/updates",
        content=b"{",
        headers={"X-OAN-Flow-Secret": bot.deps.settings.flow_secret.get_secret_value()},
    )
    assert r.status_code == 422


async def test_updates_need_a_link_then_turn_on_and_off(bot: Bot) -> None:
    c = new_contact(700000107)
    r = await bot.flow(
        "PUT", "/contacts/updates", {"request_id": rid(), "contact": c, "updates": "on"}
    )
    assert r.json() == {"result": "not_linked", "text_key": tk.LINK_INVITE}
    await link_farmer(bot, c)
    r = await bot.flow(
        "PUT", "/contacts/updates", {"request_id": rid(), "contact": c, "updates": "off"}
    )
    assert r.json() == {"result": "ok", "text_key": tk.UPDATES_OFF}
    assert await bot.q("SELECT updates_on FROM subscriptions") == [(False,)]
    await bot.q("SELECT 1")
    async with bot.deps.engine.begin() as conn:
        from sqlalchemy import text

        await conn.execute(text("UPDATE subscriptions SET state = 'unreachable'"))
    r = await bot.flow(
        "PUT", "/contacts/updates", {"request_id": rid(), "contact": c, "updates": "on"}
    )
    assert r.json() == {"result": "ok", "text_key": tk.UPDATES_ON}
    assert await bot.q("SELECT updates_on, state FROM subscriptions") == [(True, "active")]


async def test_idle_releases_queued_keyed_messages_in_order(bot: Bot) -> None:
    c = new_contact(700000108)
    ref = bot.deps.keys.contact_ref(c["telegram_id"])
    cu = uuid.UUID(c["uuid"])
    r = await bot.flow("POST", "/contacts/idle", {"contact": c})
    assert r.json() == {"released": 0}
    await keyed.enqueue(bot.deps, ref, cu, tk.STAFF_MENU)
    await keyed.enqueue(bot.deps, ref, cu, tk.LINK_INVITE, keyboard=[tk.STAFF_MENU])
    bot.up.down.add("rapidpro")
    assert (await bot.flow("POST", "/contacts/idle", {"contact": c})).json() == {"released": 0}
    bot.up.down.discard("rapidpro")
    assert (await bot.flow("POST", "/contacts/idle", {"contact": c})).json() == {"released": 2}
    assert [s["params"]["text_key"] for s in bot.up.rp_starts] == [tk.STAFF_MENU, tk.LINK_INVITE]
    assert all(s["exclude_active"] is False for s in bot.up.rp_starts)
    assert await bot.q("SELECT count(*) FROM keyed_queue") == [(0,)]


async def test_timer_releases_only_contacts_not_in_a_flow(bot: Bot) -> None:
    a, b, gone = new_contact(700000109), new_contact(700000110), new_contact(700000111)
    for c in (a, b, gone):
        await keyed.enqueue(
            bot.deps,
            bot.deps.keys.contact_ref(c["telegram_id"]),
            uuid.UUID(c["uuid"]),
            tk.STAFF_MENU,
        )
    bot.up.rp_in_flow[a["uuid"]] = None
    bot.up.rp_in_flow[b["uuid"]] = "flow-uuid"
    assert await keyed.release_idle(bot.deps) == 1
    assert [s["contacts"] for s in bot.up.rp_starts] == [[a["uuid"]]]
    assert bot.up.rp_starts[0]["exclude_active"] is True
    assert await bot.q("SELECT count(*) FROM keyed_queue") == [(1,)]  # b waits; gone dropped


async def test_unknown_text_keys_cannot_be_queued(bot: Bot) -> None:
    import pytest

    with pytest.raises(ValueError):
        await keyed.enqueue(bot.deps, "x", uuid.uuid4(), "made.up_key")
    with pytest.raises(ValueError):
        await keyed.enqueue(
            bot.deps, "x", uuid.uuid4(), tk.STAFF_MENU, keyboard=[tk.STAFF_MENU] * 9
        )


async def test_stop_deletes_everything_in_order_and_records_each_step(bot: Bot) -> None:
    c = new_contact(700000112)
    await bot.flow(
        "POST",
        "/contacts/location",
        {"request_id": rid(), "contact": c, "kebele_code": "ET01010101", "language": "en"},
    )
    await link_farmer(bot, c)
    await keyed.enqueue(
        bot.deps, bot.deps.keys.contact_ref(c["telegram_id"]), uuid.UUID(c["uuid"]), tk.STAFF_MENU
    )
    bot.up.down.add("rapidpro")
    r = await bot.flow("POST", "/contacts/stop", {"request_id": rid(), "contact": c})
    assert r.json() == {"result": "pending", "text_key": tk.STOP_DONE}
    for table in ("farmer_links", "subscriptions", "keyed_queue", "contacts", "link_requests"):
        assert await bot.q(f"SELECT count(*) FROM {table}") == [(0,)], table  # noqa: S608
    [(steps, state)] = await bot.q("SELECT steps, state FROM stop_requests")
    assert state == "pending" and "rapidpro_contact" not in steps
    assert steps == {
        "link": "done",
        "questions": "done:none",
        "staff_copies": "skipped:rv01_pending",
        "media": "skipped:not_configured",
    }
    bot.up.down.discard("rapidpro")
    from oan_bot_services import stop

    assert await stop.retry_pending(bot.deps) == 1
    assert bot.up.rp_deleted == [c["uuid"]]
    [(steps, state)] = await bot.q("SELECT steps, state FROM stop_requests")
    assert state == "done" and steps["rapidpro_contact"] == "done"
    # A new contact on the same chat that stops again is deleted again.
    c2 = {**new_contact(c["telegram_id"]), "uuid": str(uuid.uuid4())}
    r = await bot.flow("POST", "/contacts/stop", {"request_id": rid(), "contact": c2})
    assert r.json()["result"] == "done" and bot.up.rp_deleted[-1] == c2["uuid"]


async def test_stop_drops_broadcast_copies_not_yet_sent(bot: Bot) -> None:
    from test_broadcasts import farmers_broadcast

    c = new_contact(700000113)
    await link_farmer(bot, c)
    r = await farmers_broadcast(bot, "woreda", "ET010101")
    assert r.json()["recipients"] == 1
    await bot.flow("POST", "/contacts/stop", {"request_id": rid(), "contact": c})
    assert await bot.q("SELECT status, failure_code, chat_id FROM broadcast_recipients") == [
        ("failed", "farmer_stopped", None)
    ]
