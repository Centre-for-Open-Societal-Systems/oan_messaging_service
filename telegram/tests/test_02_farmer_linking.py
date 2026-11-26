"""2. Farmer linking: own contact and the OTP from Auth; wrong code; someone else's
contact; lockout after 5 wrong codes."""

from __future__ import annotations

from conftest import link_farmer, share_contact, start


def test_link_with_own_contact_and_the_code(tg, stubs, new_chat):
    tid = new_chat()
    start(tg, tid)
    tg.say(tid, "Link my Farmer ID")
    ask = tg.say(tid, "FR-TEST-0001")[0]
    assert ask["text"] == "Please tap Share my phone number."
    assert ask["reply_markup"]["keyboard"][0][0] == {
        "text": "Share my phone number",
        "request_contact": True,
    }
    sent = share_contact(tg, stubs, tid)
    # Auth sends the code to the registered phone, never to the shared one.
    assert sent["farmer_id"] == "FR-TEST-0001"
    assert sent["phone"] in ("+251900000001", "0900000001", "251900000001")
    wrong = tg.say(tid, "000000" if sent["code"] != "000000" else "111111")[0]
    assert wrong["text"] == "That code is not correct. Please try again."
    done = tg.say(tid, sent["code"])[0]
    assert (
        done["text"] == "Your Farmer ID is linked. You will receive official updates for your area."
    )


def test_someone_elses_contact_is_refused(tg, new_chat):
    tid, other = new_chat(), new_chat()
    start(tg, tid)
    tg.say(tid, "Link my Farmer ID")
    tg.say(tid, "FR-TEST-0025")
    reply = tg.say(tid, contact={"user_id": other, "phone_number": "+251900000998"})[0]
    assert reply["text"] == "Please share your own phone number using the button."


def test_unknown_farmer_id_is_not_linked(tg, new_chat):
    tid = new_chat()
    start(tg, tid)
    tg.say(tid, "Link my Farmer ID")
    tg.say(tid, "FR-TEST-9999")
    reply = tg.say(tid, contact={"user_id": tid, "phone_number": "+251900000997"})[0]
    assert reply["text"] == "We could not link this Farmer ID. Please visit your kebele office."


def test_five_wrong_codes_lock_linking(tg, stubs, new_chat):
    tid = new_chat()
    start(tg, tid)
    tg.say(tid, "Link my Farmer ID")
    tg.say(tid, "FR-TEST-0013")
    code = share_contact(tg, stubs, tid)["code"]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(4):
        assert tg.say(tid, wrong)[0]["text"] == "That code is not correct. Please try again."
    locked = tg.say(tid, wrong)[0]
    assert locked["text"] == "Linking is paused for one hour after too many wrong codes."
    # Even the right code does not link now.
    tg.say(tid, "Link my Farmer ID")
    tg.say(tid, "FR-TEST-0013")
    again = tg.say(tid, contact={"user_id": tid, "phone_number": "+251900000999"})[0]
    assert again["text"] == "Linking is paused for one hour after too many wrong codes."


def test_linking_helper_links(tg, stubs, new_chat):
    """The journey the broadcast tests use, end to end."""
    link_farmer(tg, stubs, new_chat(), "FR-TEST-0007")
