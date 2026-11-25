"""1. /start, language, privacy notice and the main menu; the webhook's secret header."""

from __future__ import annotations

import httpx

from conftest import BOT, GATEWAY

MENU = [["Link my Farmer ID", "I am OAN staff"], ["Updates on/off", "Stop updates"]]


def test_start_language_notice_and_menu(tg, new_chat):
    tid = new_chat()
    hello = tg.say(tid, "/start")[0]
    assert hello["text"] == "Welcome to OpenAgriNet. Please choose your language."
    assert hello["buttons"] == [["English", "አማርኛ"]]
    notice = tg.say(tid, "English")[0]
    assert notice["text"].startswith("Before we start: your privacy")
    assert "90 days" in notice["text"]
    assert notice["buttons"] == MENU
    # Sent through OAN Messaging: plain text, no link previews, reply buttons.
    assert notice["parse_mode"] is None
    assert notice["link_preview_options"] == {"is_disabled": True}
    # Anything typed outside the buttons gets the fixed reply and the menu again.
    other = tg.say(tid, "What is the price of teff?")[0]
    assert other["text"] == "Messages here are not read or answered. Please use the buttons below."
    assert other["buttons"] == MENU


def test_amharic_choice_uses_the_amharic_texts(tg, new_chat):
    tid = new_chat()
    tg.say(tid, "/start")
    notice = tg.say(tid, "አማርኛ")[0]
    # The privacy notice has its owner-supplied Amharic text.
    assert notice["text"].startswith("ከመጀመራችን በፊት፡ ስለ ግላዊነትዎ")


def test_webhook_needs_the_secret_at_the_gateway_and_in_bot_services():
    update = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 0,
            "text": "/start",
            "chat": {"id": 7000000001, "type": "private"},
            "from": {"id": 7000000001, "is_bot": False},
        },
    }
    gw = httpx.Client(base_url=GATEWAY, verify=False, headers={"Host": "webhook.example.invalid"})
    assert gw.post("/telegram/webhook", json=update).status_code == 401
    assert (
        gw.post(
            "/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "x" * 32}
        ).status_code
        == 401
    )
    assert gw.get("/telegram/webhook").status_code == 404
    assert gw.post("/broadcasts", json={}).status_code == 404
    # Bot Services checks the header again (defence in depth).
    direct = httpx.post(
        f"{BOT}/telegram/webhook",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": "y" * 32},
    )
    assert direct.status_code == 401
