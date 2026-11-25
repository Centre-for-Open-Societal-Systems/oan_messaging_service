"""3. Staff linking with a code from the DA app (DA backend stub, R2); wrong code."""

from __future__ import annotations

from conftest import start


def test_staff_link_with_a_code(tg, stubs, new_chat):
    tid = new_chat()
    start(tg, tid)
    ask = tg.say(tid, "I am OAN staff")[0]
    assert ask["text"] == "Please type the 8-character code from the DA app."
    code = stubs.staff_code("DA-TEST-006")
    done = tg.say(tid, code)[0]
    assert done["text"] == "You are linked as Development Agent."
    # Staff Stop keeps work messages on.
    stop = tg.say(tid, "Stop updates")[0]
    assert stop["text"] == (
        "Your work messages stay on. Your supervisor or the DA app can unlink this account."
    )


def test_wrong_staff_code(tg, new_chat):
    tid = new_chat()
    start(tg, tid)
    tg.say(tid, "I am OAN staff")
    assert tg.say(tid, "WRONG234")[0]["text"] == "That code is not correct."
    assert tg.say(tid, "abc")[0]["text"] == "That code is not correct."
