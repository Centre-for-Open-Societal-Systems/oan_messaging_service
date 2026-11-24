"""The bot's conversation without RapidPro: onboarding, farmer and staff linking, Updates
on/off, Stop and everything else. Driven through the webhook and the worker; replies read
from the fake OAN Messaging (contract 3.4.0)."""

from __future__ import annotations

from datetime import timedelta

from botenv import Tg
from botsupport import Farmer, sensitive

from oan_bot_services import text_keys as tk


async def test_start_language_privacy_notice_and_main_menu(tg: Tg) -> None:
    chat = 700002001
    r = await tg.say(chat, "/start")
    assert r["text"] == tg.text(tk.ONBOARDING_CHOOSE_LANGUAGE)
    r = await tg.say(chat, tg.label(tk.BTN_LANG_AM))
    st = tg.deps.settings
    assert r["text"] == tg.text(tk.ONBOARDING_PRIVACY_NOTICE, "am", retention_days=90, log_days=7)
    assert st.stopped_retention_days == 90 and st.update_id_days == 7
    assert r["reply_markup"] == {
        "keyboard": [
            [{"text": tg.label(tk.BTN_LINK_FARMER_ID, "am")},
             {"text": tg.label(tk.BTN_STAFF, "am")}],
            [{"text": tg.label(tk.BTN_UPDATES_TOGGLE, "am")},
             {"text": tg.label(tk.BTN_STOP_UPDATES, "am")}],
        ],
        "one_time": True,
        "resize": True,
    }
    assert r["purpose"] == "transactional"
    [(language, subscriber)] = await tg.q("SELECT language, subscriber_id FROM contacts")
    assert language == "am" and subscriber is not None
    # Every reply after that is in Amharic.
    r = await tg.say(chat, "hello?")
    assert r["text"] == tg.text(tk.COMMON_NOT_READ, "am")


async def test_anything_else_gets_the_fixed_reply(tg: Tg) -> None:
    chat = 700002002
    for text in ("When will it rain?", None):
        r = await tg.say(chat, text)
        assert r["text"] == tg.text(tk.COMMON_NOT_READ)
        assert r["reply_markup"]["keyboard"][0][0]["text"] == tg.label(tk.BTN_LINK_FARMER_ID)
    r = await tg.say(chat, "/menu")
    assert r["text"] == tg.text(tk.MENU_MAIN)


async def test_a_step_expires_after_thirty_minutes(tg: Tg) -> None:
    chat = 700002003
    await tg.say(chat, tg.label(tk.BTN_LINK_FARMER_ID))
    tg.clock.now += timedelta(minutes=31)
    r = await tg.say(chat, "FR-TEST-2003")
    assert r["text"] == tg.text(tk.COMMON_SESSION_EXPIRED)
    assert await tg.q("SELECT count(*) FROM conversations") == [(0,)]


async def test_farmer_linking_success_creates_link_and_subscription(tg: Tg) -> None:
    chat = 700002004
    await tg.onboard(chat)
    tg.up.farmers["FR-TEST-2004"] = Farmer("FR-TEST-2004", "+251900002004", "ET01010101")
    sensitive("FR-TEST-2004", "+251900002004", "900002004")
    await tg.say(chat, tg.label(tk.BTN_LINK_FARMER_ID))
    r = await tg.say(chat, "FR-TEST-2004")
    assert r["reply_markup"]["keyboard"][0] == [
        {"text": tg.label(tk.BTN_SHARE_PHONE), "request_contact": True}
    ]
    # The farmer types instead of tapping: asked again.
    r = await tg.say(chat, "0900002004")
    assert r["text"] == tg.text(tk.LINK_ASK_CONTACT)
    r = await tg.say(chat, contact={"user_id": chat, "phone_number": "0900002099"})
    assert r["text"] == tg.text(tk.LINK_CODE_SENT, masked_phone="04")
    # Auth got the REGISTERED phone; Bot Services sent no SMS and no text to that number.
    [otp] = tg.up.otps.values()
    assert otp["phone"] == "+251900002004"
    assert all(m["body"]["chat_id"] == chat for m in tg.up.oan_messages.values())
    r = await tg.say(chat, tg.up.otp_code)
    assert r["text"] == tg.text(tk.LINK_DONE)
    [(fid, uuid_, same, linked_phone)] = await tg.q(
        "SELECT farmer_id, contact_uuid, same_number, linked_phone FROM farmer_links")
    assert fid == "FR-TEST-2004" and uuid_ is None and same is False and linked_phone
    [(state, on, sub_chat, lang)] = await tg.q(
        "SELECT state, updates_on, chat_id, language FROM subscriptions")
    assert (state, on, sub_chat, lang) == ("active", True, chat, "en")
    # No code is kept anywhere once handled.
    for table in ("telegram_updates", "conversations", "outbox"):
        assert await tg.q(f"SELECT count(*) FROM {table}") == [(0,)], table  # noqa: S608


async def test_someone_elses_contact_is_refused(tg: Tg) -> None:
    chat = 700002005
    tg.up.farmers["FR-TEST-2005"] = Farmer("FR-TEST-2005", "+251900002005", "ET01010101")
    sensitive("FR-TEST-2005", "+251900002005")
    await tg.say(chat, tg.label(tk.BTN_LINK_FARMER_ID))
    await tg.say(chat, "FR-TEST-2005")
    r = await tg.say(chat, contact={"user_id": 700002999, "phone_number": "+251900002005"})
    assert r["text"] == tg.text(tk.LINK_NOT_OWN_CONTACT)
    r = await tg.say(chat, contact={"phone_number": "+251900002005"})  # no user id at all
    assert r["text"] == tg.text(tk.LINK_NOT_OWN_CONTACT)
    assert tg.up.otps == {}


async def test_unknown_or_malformed_farmer_id(tg: Tg) -> None:
    chat = 700002006
    await tg.say(chat, tg.label(tk.BTN_LINK_FARMER_ID))
    await tg.say(chat, "FR-TEST-NOPE")
    sensitive("FR-TEST-NOPE")
    r = await tg.say(chat, contact={"user_id": chat, "phone_number": "+251900002006"})
    assert r["text"] == tg.text(tk.LINK_NOT_FOUND)
    await tg.say(chat, tg.label(tk.BTN_LINK_FARMER_ID))
    r = await tg.say(chat, "not an id!")
    assert r["text"] == tg.text(tk.LINK_NOT_FOUND)
    assert tg.up.otps == {}


async def _to_code_step(tg: Tg, chat: int, fid: str, phone: str) -> None:
    tg.up.farmers[fid] = Farmer(fid, phone, "ET01010101")
    sensitive(fid, phone, phone[-9:])
    await tg.say(chat, tg.label(tk.BTN_LINK_FARMER_ID))
    await tg.say(chat, fid)
    r = await tg.say(chat, contact={"user_id": chat, "phone_number": phone})
    assert r["text"] == tg.text(tk.LINK_CODE_SENT, masked_phone=phone[-2:])


async def test_wrong_code_then_right_code(tg: Tg) -> None:
    chat = 700002007
    await _to_code_step(tg, chat, "FR-TEST-2007", "+251900002007")
    r = await tg.say(chat, "000000")
    assert r["text"] == tg.text(tk.LINK_CODE_INVALID)
    r = await tg.say(chat, "abc")  # not a code: not counted
    assert r["text"] == tg.text(tk.LINK_CODE_INVALID)
    assert await tg.q("SELECT count(*) FROM link_failures") == [(1,)]
    r = await tg.say(chat, tg.up.otp_code)
    assert r["text"] == tg.text(tk.LINK_DONE)
    assert await tg.q("SELECT count(*) FROM link_failures") == [(0,)]


async def test_expired_code(tg: Tg) -> None:
    chat = 700002008
    await _to_code_step(tg, chat, "FR-TEST-2008", "+251900002008")
    tg.clock.now += timedelta(minutes=11)  # Auth said 600 s; the step (30 min) still runs
    r = await tg.say(chat, tg.up.otp_code)
    assert r["text"] == tg.text(tk.LINK_CODE_EXPIRED)
    assert await tg.q("SELECT count(*) FROM subscriptions") == [(0,)]
    # Auth itself may also say expired.
    chat2 = 700002018
    await _to_code_step(tg, chat2, "FR-TEST-2018", "+251900002018")
    for otp in tg.up.otps.values():
        otp["expired"] = True
    r = await tg.say(chat2, tg.up.otp_code)
    assert r["text"] == tg.text(tk.LINK_CODE_EXPIRED)


async def test_five_wrong_codes_lock_linking_for_an_hour(tg: Tg) -> None:
    chat = 700002009
    await _to_code_step(tg, chat, "FR-TEST-2009", "+251900002009")
    for _ in range(4):
        assert (await tg.say(chat, "000000"))["text"] == tg.text(tk.LINK_CODE_INVALID)
    assert (await tg.say(chat, "000000"))["text"] == tg.text(tk.LINK_LOCKED)
    await tg.say(chat, tg.label(tk.BTN_LINK_FARMER_ID))
    await tg.say(chat, "FR-TEST-2009")
    r = await tg.say(chat, contact={"user_id": chat, "phone_number": "+251900002009"})
    assert r["text"] == tg.text(tk.LINK_LOCKED)
    tg.clock.now += timedelta(hours=1, minutes=1)
    await _to_code_step(tg, chat, "FR-TEST-2009", "+251900002009")


async def test_replacement_links(tg: Tg) -> None:
    a, b = 700002010, 700002011
    await tg.link(a, "FR-TEST-2010", "+251900002010")
    # Same Farmer ID from a new chat: the old chat's link and subscription go.
    await tg.link(b, "FR-TEST-2010", "+251900002010")
    assert await tg.q("SELECT chat_id, farmer_id FROM subscriptions") == [(b, "FR-TEST-2010")]
    assert await tg.q("SELECT contact_ref FROM farmer_links") == [(tg.ref(b),)]
    # Same chat, a new Farmer ID: one link per chat.
    await tg.link(b, "FR-TEST-2011", "+251900002011", onboard=False)
    assert await tg.q("SELECT chat_id, farmer_id FROM subscriptions") == [(b, "FR-TEST-2011")]
    assert await tg.q("SELECT count(*) FROM farmer_links") == [(1,)]


async def test_auth_down_keeps_the_step(tg: Tg) -> None:
    chat = 700002012
    tg.up.farmers["FR-TEST-2012"] = Farmer("FR-TEST-2012", "+251900002012", "ET01010101")
    sensitive("FR-TEST-2012", "+251900002012")
    await tg.say(chat, tg.label(tk.BTN_LINK_FARMER_ID))
    await tg.say(chat, "FR-TEST-2012")
    tg.up.down.add("auth")
    r = await tg.say(chat, contact={"user_id": chat, "phone_number": "+251900002012"})
    assert r["text"] == tg.text(tk.COMMON_UNAVAILABLE)
    tg.up.down.discard("auth")
    r = await tg.say(chat, contact={"user_id": chat, "phone_number": "+251900002012"})
    assert r["text"] == tg.text(tk.LINK_CODE_SENT, masked_phone="12")
    tg.up.down.add("auth")
    r = await tg.say(chat, tg.up.otp_code)
    assert r["text"] == tg.text(tk.COMMON_UNAVAILABLE)
    tg.up.down.discard("auth")
    r = await tg.say(chat, tg.up.otp_code)
    assert r["text"] == tg.text(tk.LINK_DONE)


async def test_staff_linking_success(tg: Tg) -> None:
    chat = 700002013
    tg.up.link_codes["AB12CD34"] = ("DA-TEST-013", "development_agent")
    sensitive("AB12CD34")
    r = await tg.say(chat, tg.label(tk.BTN_STAFF))
    assert r["text"] == tg.text(tk.STAFF_ASK_CODE)
    r = await tg.say(chat, "AB12CD34")
    assert r["text"] == tg.text(tk.STAFF_LINKED, role_name=tk.ROLE_KEYS["development_agent"])
    assert "Development Agent" in r["text"]
    assert await tg.q("SELECT da_ref, role, contact_uuid FROM staff_links") == [
        ("DA-TEST-013", "development_agent", None)]


async def test_staff_wrong_and_expired_codes_then_lockout(tg: Tg) -> None:
    chat = 700002014
    tg.up.link_codes["EXPIRED1"] = "expired"
    await tg.say(chat, tg.label(tk.BTN_STAFF))
    assert (await tg.say(chat, "EXPIRED1"))["text"] == tg.text(tk.STAFF_CODE_EXPIRED)
    assert (await tg.say(chat, "short"))["text"] == tg.text(tk.STAFF_CODE_INVALID)
    for _ in range(3):
        assert (await tg.say(chat, "WRONG123"))["text"] == tg.text(tk.STAFF_CODE_INVALID)
    assert (await tg.say(chat, "WRONG123"))["text"] == tg.text(tk.STAFF_LOCKED)
    tg.up.link_codes["AB12CD99"] = ("DA-TEST-014", "supervisor")
    await tg.say(chat, tg.label(tk.BTN_STAFF))
    assert (await tg.say(chat, "AB12CD99"))["text"] == tg.text(tk.STAFF_LOCKED)
    tg.clock.now += timedelta(hours=1, minutes=1)
    await tg.say(chat, tg.label(tk.BTN_STAFF))
    r = await tg.say(chat, "AB12CD99")
    assert r["text"] == tg.text(tk.STAFF_LINKED, role_name=tk.ROLE_KEYS["supervisor"])


async def test_staff_link_when_the_da_backend_is_down(tg: Tg) -> None:
    chat = 700002015
    await tg.say(chat, tg.label(tk.BTN_STAFF))
    tg.up.down.add("da")
    assert (await tg.say(chat, "AB12CD34"))["text"] == tg.text(tk.COMMON_UNAVAILABLE)
    assert await tg.q("SELECT count(*) FROM link_failures") == [(0,)]


async def test_updates_toggle_needs_a_link(tg: Tg) -> None:
    chat = 700002016
    assert (await tg.say(chat, tg.label(tk.BTN_UPDATES_TOGGLE)))["text"] == tg.text(tk.LINK_INVITE)
    await tg.link(chat, "FR-TEST-2016", "+251900002016")
    assert (await tg.say(chat, tg.label(tk.BTN_UPDATES_TOGGLE)))["text"] == tg.text(tk.UPDATES_OFF)
    assert await tg.q("SELECT updates_on, state, stopped_at FROM subscriptions") == [
        (False, "active", None)]  # kept while reachable, no deletion clock
    assert (await tg.say(chat, tg.label(tk.BTN_UPDATES_TOGGLE)))["text"] == tg.text(tk.UPDATES_ON)
    assert await tg.q("SELECT updates_on FROM subscriptions") == [(True,)]


async def test_farmer_stop_turns_updates_off_and_keeps_the_link(tg: Tg) -> None:
    chat = 700002017
    await tg.link(chat, "FR-TEST-2017", "+251900002017")
    r = await tg.say(chat, tg.label(tk.BTN_STOP_UPDATES))
    assert r["text"] == tg.text(tk.STOP_UPDATES_STOPPED, retention_days=90)
    assert r["reply_markup"] == {"remove_keyboard": True}
    [(on, state, stopped_at)] = await tg.q(
        "SELECT updates_on, state, stopped_at FROM subscriptions")
    assert (on, state, stopped_at) == (False, "stopped", tg.clock.now)
    assert await tg.q("SELECT count(*) FROM farmer_links") == [(1,)]
    # /stop again keeps the first time (the retention clock does not restart).
    tg.clock.now += timedelta(days=1)
    await tg.say(chat, "/stop")
    assert (await tg.q("SELECT stopped_at FROM subscriptions"))[0][0] == stopped_at


async def test_staff_stop_keeps_work_messages(tg: Tg) -> None:
    chat = 700002019
    tg.up.link_codes["STAFF019"] = ("DA-TEST-019", "development_agent")
    await tg.say(chat, tg.label(tk.BTN_STAFF))
    await tg.say(chat, "STAFF019")
    r = await tg.say(chat, "/stop")
    assert r["text"] == tg.text(tk.STOP_STAFF)
    assert len(tg.up.replies(chat)) == 3  # ask code, linked, the staff Stop reply only
    assert await tg.q("SELECT count(*) FROM staff_links") == [(1,)]


async def test_stop_without_a_link_marks_the_chat_for_deletion(tg: Tg) -> None:
    chat = 700002020
    await tg.onboard(chat)
    r = await tg.say(chat, "/stop")
    assert r["text"] == tg.text(tk.STOP_UPDATES_STOPPED, retention_days=90)
    assert await tg.q("SELECT stopped_at FROM contacts") == [(tg.clock.now,)]
    await tg.say(chat, "/start")  # the chat came back
    assert await tg.q("SELECT stopped_at FROM contacts") == [(None,)]
