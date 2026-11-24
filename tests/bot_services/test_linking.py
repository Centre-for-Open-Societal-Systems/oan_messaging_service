"""Staff linking (§6.5) and farmer linking (§6.6)."""

from __future__ import annotations

import uuid

from botenv import Bot, link_farmer
from botsupport import Farmer, new_contact, rid, sensitive

from oan_bot_services import text_keys as tk

PHONE = "+251900000121"
sensitive(PHONE, PHONE[1:], "0900000121", "900000121")


def code(n: int) -> str:
    value = f"LNK{n:05d}"
    sensitive(value)
    return value


async def staff(bot: Bot, c: dict, value: str) -> dict:
    r = await bot.flow("POST", "/staff/link", {"request_id": rid(), "contact": c, "code": value})
    assert r.status_code == 200
    return dict(r.json())


async def test_staff_link_calls_r2_adds_group_and_returns_role_key(bot: Bot) -> None:
    c = new_contact(700000201)
    bot.up.link_codes[code(1)] = ("DA-TEST-001", "development_agent")
    out = await staff(bot, c, code(1))
    assert out == {
        "result": "linked",
        "role": "development_agent",
        "text_key": tk.STAFF_LINKED,
        "params": {"role_name": "role.development_agent"},
    }
    assert bot.up.rp_groups[c["uuid"]] == {"staff"}
    assert await bot.q("SELECT da_ref, role, group_added FROM staff_links") == [
        ("DA-TEST-001", "development_agent", True)
    ]


async def test_staff_group_add_is_retried_when_rapidpro_is_down(bot: Bot) -> None:
    from oan_bot_services import jobs

    c = new_contact(700000202)
    bot.up.link_codes[code(2)] = ("SUP-TEST-001", "supervisor")
    bot.up.down.add("rapidpro")
    assert (await staff(bot, c, code(2)))["result"] == "linked"
    assert await bot.q("SELECT group_added FROM staff_links") == [(False,)]
    bot.up.down.discard("rapidpro")
    assert await jobs.retry_rapidpro_sync(bot.deps) == 1
    assert bot.up.rp_groups[c["uuid"]] == {"staff"}


async def test_staff_wrong_and_expired_codes_lock_after_five_in_an_hour(bot: Bot) -> None:
    c = new_contact(700000203)
    bot.up.link_codes[code(3)] = "expired"
    assert (await staff(bot, c, code(3)))["text_key"] == tk.STAFF_CODE_EXPIRED
    for n in range(4, 7):
        assert (await staff(bot, c, code(n))) == {
            "result": "invalid",
            "text_key": tk.STAFF_CODE_INVALID,
        }
    assert (await staff(bot, c, code(7))) == {"result": "locked", "text_key": tk.STAFF_LOCKED}
    bot.up.link_codes[code(8)] = ("DA-TEST-002", "development_agent")
    calls = len(bot.up.calls)
    assert (await staff(bot, c, code(8)))["result"] == "locked"
    assert len(bot.up.calls) == calls  # locked: R2 is not called
    bot.clock.now += __import__("datetime").timedelta(minutes=61)
    assert (await staff(bot, c, code(8)))["result"] == "linked"


async def test_staff_link_when_da_backend_is_down(bot: Bot) -> None:
    bot.up.down.add("da")
    r = await bot.flow(
        "POST",
        "/staff/link",
        {"request_id": rid(), "contact": new_contact(700000204), "code": code(9)},
    )
    assert r.status_code == 503 and r.json()["text_key"] == tk.COMMON_UNAVAILABLE


async def test_staff_inbound_skeleton_answers_the_staff_menu(bot: Bot) -> None:
    r = await bot.flow(
        "POST",
        "/staff/inbound",
        {
            "request_id": rid(),
            "contact": new_contact(700000205),
            "text": "hello",
            "attachments": ["https://media.example.invalid/a.jpg"],
        },
    )
    assert r.json() == {"text_key": tk.STAFF_MENU, "params": {}, "keyboard": []}


async def start(
    bot: Bot,
    c: dict,
    farmer_id: str = "FR-TEST-0101",
    phone: str = PHONE,
    user_id: int | None = None,
) -> dict:
    r = await bot.flow(
        "POST",
        "/farmer-link/start",
        {
            "request_id": rid(),
            "contact": c,
            "farmer_id": farmer_id,
            "shared_contact": {"user_id": user_id or c["telegram_id"], "phone_number": phone},
        },
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


async def verify(bot: Bot, c: dict, link_request_id: str, value: str) -> dict:
    sensitive(value)
    r = await bot.flow(
        "POST",
        "/farmer-link/verify",
        {"request_id": rid(), "contact": c, "link_request_id": link_request_id, "code": value},
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


async def test_farmer_link_sends_otp_to_the_registered_phone_and_links(bot: Bot) -> None:
    sensitive("FR-TEST-0101")
    bot.up.farmers["FR-TEST-0101"] = Farmer("FR-TEST-0101", PHONE, "ET01010101")
    c = new_contact(700000206)
    out = await start(bot, c, phone="0900000999")  # alternate Telegram number
    sensitive("0900000999", "+251900000999", "900000999")
    assert out["result"] == "code_sent" and out["params"] == {"masked_phone": "21"}
    assert out["text_key"] == tk.LINK_CODE_SENT
    [otp] = bot.up.otps.values()
    assert otp["phone"] == PHONE  # always the registered phone
    done = await verify(bot, c, out["link_request_id"], bot.up.otp_code)
    assert done == {"result": "linked", "ask_registry_kebele": False, "text_key": tk.LINK_DONE}
    assert bot.up.rp_fields[c["uuid"]] == {"linked": "yes"}
    [(farmer_id, kebele, chat_id, updates)] = await bot.q(
        "SELECT farmer_id, kebele_code, chat_id, updates_on FROM subscriptions"
    )
    assert (farmer_id, kebele, chat_id, updates) == ("FR-TEST-0101", "ET01010101", 700000206, True)
    [(sealed, same)] = await bot.q("SELECT linked_phone, same_number FROM farmer_links")
    assert same is False and b"900000999" not in bytes(sealed)
    assert bot.deps.keys.open(bytes(sealed), "linked_phone") == "+251900000999"
    # The code works once.
    again = await verify(bot, c, out["link_request_id"], bot.up.otp_code)
    assert again["result"] == "invalid"


async def test_farmer_link_same_number_stores_no_extra_phone(bot: Bot) -> None:
    c = new_contact(700000207)
    await link_farmer(bot, c, "FR-TEST-0102", "+251900000122")
    assert await bot.q("SELECT linked_phone IS NULL, same_number FROM farmer_links") == [
        (True, True)
    ]


async def test_registry_kebele_differs_from_the_chosen_one(bot: Bot) -> None:
    c = new_contact(700000208)
    await bot.flow(
        "POST",
        "/contacts/location",
        {"request_id": rid(), "contact": c, "kebele_code": "ET01010201", "language": "en"},
    )
    sensitive("FR-TEST-0103")
    bot.up.farmers["FR-TEST-0103"] = Farmer("FR-TEST-0103", PHONE, "ET01010101")
    out = await start(bot, c, "FR-TEST-0103")
    done = await verify(bot, c, out["link_request_id"], bot.up.otp_code)
    assert done["ask_registry_kebele"] is True
    assert done["params"] == {"kebele_name": "Area ET01010101"}
    assert await bot.q("SELECT kebele_code FROM subscriptions") == [("ET01010101",)]


async def test_someone_elses_contact_unknown_id_and_no_phone(bot: Bot) -> None:
    c = new_contact(700000209)
    assert (await start(bot, c, user_id=700000999))["result"] == "not_own_contact"
    sensitive("FR-TEST-0104", "FR-TEST-0105", "FR-TEST-0106", "FR-NOPE-0001")
    bot.up.farmers["FR-TEST-0104"] = Farmer("FR-TEST-0104", None, "ET01010101")
    bot.up.farmers["FR-TEST-0105"] = Farmer("FR-TEST-0105", PHONE, "ET01010101", "ARCHIVED")
    for fid in ("FR-NOPE-0001", "FR-TEST-0104", "FR-TEST-0105"):
        assert await start(bot, c, fid) == {"result": "not_found", "text_key": tk.LINK_NOT_FOUND}
    assert bot.up.otps == {}


async def test_auth_rate_limit_and_outages(bot: Bot) -> None:
    sensitive("FR-TEST-0107")
    bot.up.farmers["FR-TEST-0107"] = Farmer("FR-TEST-0107", PHONE, "ET01010101")
    c = new_contact(700000210)
    for _ in range(3):
        assert (await start(bot, c, "FR-TEST-0107"))["result"] == "code_sent"
    assert await start(bot, c, "FR-TEST-0107") == {
        "result": "rate_limited",
        "text_key": tk.LINK_RATE_LIMITED,
    }
    bot.up.down.add("beneficiary")
    r = await bot.flow(
        "POST",
        "/farmer-link/start",
        {
            "request_id": rid(),
            "contact": c,
            "farmer_id": "FR-TEST-0107",
            "shared_contact": {"user_id": c["telegram_id"], "phone_number": PHONE},
        },
    )
    assert r.status_code == 503 and r.json()["error"] == "unavailable"


async def test_five_wrong_codes_lock_farmer_linking_for_an_hour(bot: Bot) -> None:
    from datetime import timedelta

    sensitive("FR-TEST-0108", "111111")
    bot.up.farmers["FR-TEST-0108"] = Farmer("FR-TEST-0108", PHONE, "ET01010101")
    c = new_contact(700000211)
    lr = (await start(bot, c, "FR-TEST-0108"))["link_request_id"]
    for _ in range(4):
        assert (await verify(bot, c, lr, "111111"))["result"] == "invalid"
    assert await verify(bot, c, lr, "111111") == {"result": "locked", "text_key": tk.LINK_LOCKED}
    assert (await verify(bot, c, lr, bot.up.otp_code))["result"] == "locked"
    assert (await start(bot, c, "FR-TEST-0108"))["result"] == "locked"
    bot.clock.now += timedelta(minutes=61)
    lr = (await start(bot, c, "FR-TEST-0108"))["link_request_id"]
    assert (await verify(bot, c, lr, bot.up.otp_code))["result"] == "linked"


async def test_expired_code_and_unknown_request(bot: Bot) -> None:
    from datetime import timedelta

    sensitive("FR-TEST-0109")
    bot.up.farmers["FR-TEST-0109"] = Farmer("FR-TEST-0109", PHONE, "ET01010101")
    c = new_contact(700000212)
    lr = (await start(bot, c, "FR-TEST-0109"))["link_request_id"]
    bot.clock.now += timedelta(minutes=11)
    assert await verify(bot, c, lr, bot.up.otp_code) == {
        "result": "expired",
        "text_key": tk.LINK_CODE_EXPIRED,
    }
    lr2 = (await start(bot, c, "FR-TEST-0109"))["link_request_id"]
    next(iter(o for k, o in bot.up.otps.items() if k == lr2))["expired"] = True
    assert (await verify(bot, c, lr2, bot.up.otp_code))["result"] == "expired"
    other = new_contact(700000213)
    assert (await verify(bot, other, lr2, bot.up.otp_code))["result"] == "invalid"
    assert (await verify(bot, c, str(uuid.uuid4()), bot.up.otp_code))["result"] == "invalid"


async def test_a_new_link_from_another_account_replaces_the_old_one(bot: Bot) -> None:
    old, new = new_contact(700000214), new_contact(700000215)
    await link_farmer(bot, old, "FR-TEST-0110", "+251900000130")
    await link_farmer(bot, new, "FR-TEST-0110", "+251900000130")
    assert await bot.q("SELECT chat_id FROM subscriptions") == [(700000215,)]
    assert await bot.q("SELECT count(*) FROM farmer_links") == [(1,)]


async def test_unlink(bot: Bot) -> None:
    c = new_contact(700000216)
    body = {"contact": c}
    r = await bot.flow("DELETE", "/farmer-link", {"request_id": rid(), **body})
    assert r.json() == {"result": "not_linked", "text_key": tk.LINK_NONE}
    await link_farmer(bot, c, "FR-TEST-0111", "+251900000131")
    r = await bot.flow("DELETE", "/farmer-link", {"request_id": rid(), **body})
    assert r.json() == {"result": "unlinked", "text_key": tk.LINK_REMOVED}
    assert bot.up.rp_fields[c["uuid"]] == {"linked": "no"}
    assert await bot.q("SELECT count(*) FROM subscriptions") == [(0,)]


async def test_linked_field_is_retried_when_rapidpro_is_down(bot: Bot) -> None:
    from oan_bot_services import jobs

    c = new_contact(700000217)
    bot.up.down.add("rapidpro")
    await link_farmer(bot, c, "FR-TEST-0112", "+251900000132")
    assert await bot.q("SELECT rapidpro_synced FROM farmer_links") == [(False,)]
    bot.up.down.discard("rapidpro")
    assert await jobs.retry_rapidpro_sync(bot.deps) == 1
    assert bot.up.rp_fields[c["uuid"]] == {"linked": "yes"}
