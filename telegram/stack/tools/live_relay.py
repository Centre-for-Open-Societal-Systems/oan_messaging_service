"""Live relay between a real Telegram test bot and the local Telegram stack.

For a live check with a throwaway test bot (made with BotFather and deleted afterwards).
Run it next to a running stack (telegram/stack/up.sh) on the same machine.

Inbound: polls the real Bot API (getUpdates) and posts each private-chat update, unchanged,
to the stack's webhook gateway with the secret header, so OAN Bot Services handles it as
it would a webhook call. An update is confirmed to Telegram (the offset moves past it)
only after the gateway accepted it. When you share your contact while linking, it prints
the code the stand-in Auth service "sent", for you to type in Telegram.
Outbound: whatever OAN Messaging sends to the fake Telegram for your chat (text and reply
buttons) is sent on to your real chat.

Rules it keeps:
  * One loop, one Telegram request in flight at any time.
  * It never changes the bot: it checks that no webhook is set and stops if one is.
  * On the first "too many requests" (429) it stops and prints how long to wait. It also
    stops on any other refusal or a connection error. It never retries Telegram.
  * The token is read from a file and never printed. Message text is never printed.

Usage (from telegram/stack; the token never appears on screen or in shell history):
    read -rs TOK && printf %s "$TOK" > ~/.oan-relay-token && chmod 600 ~/.oan-relay-token
    set -a; . ./.env; set +a
    python3 -m venv /tmp/oan-relay && /tmp/oan-relay/bin/pip -q install httpx
    /tmp/oan-relay/bin/python tools/live_relay.py ~/.oan-relay-token
Afterwards: Ctrl-C, ./down.sh, rm ~/.oan-relay-token, and /deletebot in BotFather.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

# OAN Bot Services derives the Auth link request id from the chat and the update.
LINK_NS = uuid.UUID("6f1d8c3e-4b2a-4e0f-9c71-0a3b5d7e9f11")
POLL_SECONDS = 3  # getUpdates long-poll; keeps replies quick with one request in flight


class Stop(Exception):
    """Telegram refused or could not be reached: stop the relay, never retry."""


def log(*a: object) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Telegram:
    """The real Bot API, one call at a time. Any refusal raises Stop."""

    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._c = client or httpx.Client(timeout=POLL_SECONDS + 30)

    def call(self, method: str, **payload: object) -> Any:
        try:
            r = self._c.post(f"{self._base}/{method}", json=payload)
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Never include the exception text: it can carry the URL, and so the token.
            raise Stop(f"{method}: Telegram could not be reached ({type(exc).__name__})") from None
        if body.get("ok"):
            return body["result"]
        if r.status_code == 429:
            wait = (body.get("parameters") or {}).get("retry_after")
            raise Stop(
                f"{method}: Telegram says too many requests; wait {wait} s before trying again"
            )
        raise Stop(f"{method}: refused ({r.status_code}) {str(body.get('description', ''))[:80]}")


class Stack:
    """The local stack: fake Telegram test routes, stubs and the webhook gateway."""

    def __init__(self, secret: str, ref_key: bytes, clients: dict[str, httpx.Client] | None = None):
        c = clients or {}
        self.fake = c.get("fake") or httpx.Client(
            base_url="http://127.0.0.1:28301", timeout=10, trust_env=False
        )
        self.stubs = c.get("stubs") or httpx.Client(
            base_url="http://127.0.0.1:28200", timeout=10, trust_env=False
        )
        self.gw = c.get("gw") or httpx.Client(
            base_url="https://127.0.0.1:28443",
            verify=False,  # noqa: S501 (the stack's throwaway test CA, loopback port)
            trust_env=False,
            timeout=15,
        )
        self._secret = secret
        self._ref_key = ref_key

    def add_chat(self, chat_id: int) -> int:
        """Create the chat at the fake Telegram; return the last message id already there."""
        self.fake.post("/_test/users", json={"telegram_id": chat_id, "first_name": "Live"})
        return max((m["message_id"] for m in self.sent(chat_id)), default=0)

    def sent(self, chat_id: int) -> list[dict]:
        return self.fake.get("/_test/sent", params={"telegram_id": chat_id}).json()["messages"]

    def deliver(self, update: dict) -> int:
        try:
            return self.gw.post(
                "/telegram/webhook",
                json=update,
                headers={
                    "Host": "webhook.example.invalid",
                    "X-Telegram-Bot-Api-Secret-Token": self._secret,
                },
            ).status_code
        except httpx.HTTPError:
            return 0

    def code(self, chat_id: int, update_id: int) -> str | None:
        ref = hmac.new(self._ref_key, str(chat_id).encode(), hashlib.sha256).hexdigest()
        lrid = uuid.uuid5(LINK_NS, f"{ref}:{update_id}")
        for _ in range(10):
            r = self.stubs.get("/auth/_test/codes", params={"link_request_id": str(lrid)})
            if r.status_code == 200:
                return r.json()["code"]
            time.sleep(1)
        return None


class Relay:
    def __init__(self, tg: Telegram, stack: Stack) -> None:
        self.tg = tg
        self.stack = stack
        self.offset = 0
        self.chats: dict[int, int] = {}  # chat id -> last message id sent on

    def start(self) -> None:
        me = self.tg.call("getMe")
        hook = self.tg.call("getWebhookInfo")
        if hook.get("url"):
            raise Stop(
                "a webhook is set on this bot; the relay needs it empty and will not change it"
            )
        log(f"bot ready: @{me['username']} - press Start in Telegram")

    def poll_once(self) -> None:
        updates = self.tg.call(
            "getUpdates", offset=self.offset, timeout=POLL_SECONDS, allowed_updates=["message"]
        )
        for up in updates:
            msg = up.get("message") or {}
            chat = msg.get("chat") or {}
            if chat.get("type") != "private":
                self.offset = up["update_id"] + 1
                continue
            cid = int(chat["id"])
            if cid not in self.chats:
                self.chats[cid] = self.stack.add_chat(cid)
            kind = "contact" if "contact" in msg else ("text" if "text" in msg else "other")
            status = self.stack.deliver(up)
            log("update in:", kind, "->", status or "no answer")
            if status != 200:
                # Not confirmed to Telegram: the same update comes back on the next poll.
                return
            self.offset = up["update_id"] + 1
            if kind == "contact":
                code = self.stack.code(cid, up["update_id"])
                log(
                    ">>> code from the stand-in Auth service:",
                    code or "none (wrong Farmer ID or contact?)",
                )

    def send_once(self) -> None:
        for cid, last in list(self.chats.items()):
            for m in self.stack.sent(cid):
                if m["message_id"] <= last:
                    continue
                payload: dict = {"chat_id": cid, "text": m["text"]}
                if m.get("reply_markup"):
                    payload["reply_markup"] = m["reply_markup"]
                if m.get("link_preview_options"):
                    payload["link_preview_options"] = m["link_preview_options"]
                self.tg.call("sendMessage", **payload)
                log("message out:", "with buttons" if m.get("buttons") else "text")
                self.chats[cid] = m["message_id"]

    def run(self) -> None:
        self.start()
        while True:
            self.send_once()
            self.poll_once()


def main() -> int:
    token = Path(sys.argv[1]).expanduser().read_text().strip()
    relay = Relay(
        Telegram(token),
        Stack(os.environ["TG_WEBHOOK_SECRET"], base64.b64decode(os.environ["CONTACT_REF_KEY"])),
    )
    try:
        relay.run()
    except Stop as exc:
        log("stopped:", exc)
        return 2
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
