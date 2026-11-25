"""Live relay between a real Telegram test bot and the local Telegram stack.

For a live check with a throwaway test bot (made with BotFather and deleted afterwards).
Run it next to a running stack (telegram/stack/up.sh) on the same machine.

Inbound: polls the real Bot API (getUpdates) and posts each private-chat update, unchanged,
to the stack's webhook gateway with the secret header, so OAN Bot Services handles it as
it would a webhook call. When you share your contact while linking, it prints the code
the stand-in Auth service "sent", for you to type in Telegram.
Outbound: whatever OAN Messaging sends to the fake Telegram for your chat (text and reply
buttons) is sent on to your real chat.

Usage (from telegram/stack; the token never appears on screen or in shell history):
    read -rs TOK && printf %s "$TOK" > ~/.oan-relay-token && chmod 600 ~/.oan-relay-token
    set -a; . ./.env; set +a
    python3 -m venv /tmp/oan-relay && /tmp/oan-relay/bin/pip -q install httpx
    /tmp/oan-relay/bin/python tools/live_relay.py ~/.oan-relay-token
Afterwards: Ctrl-C, ./down.sh, rm ~/.oan-relay-token, and /deletebot in BotFather.

The token is read from the file and never printed. It waits as long as Telegram asks
after a "too many requests" answer, and never polls faster than one request at a time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

TOKEN = Path(sys.argv[1]).expanduser().read_text().strip()
SECRET = os.environ["TG_WEBHOOK_SECRET"]
REF_KEY = base64.b64decode(os.environ["CONTACT_REF_KEY"])
# OAN Bot Services derives the Auth link request id from the chat and the update.
LINK_NS = uuid.UUID("6f1d8c3e-4b2a-4e0f-9c71-0a3b5d7e9f11")
API = f"https://api.telegram.org/bot{TOKEN}"

real = httpx.Client(timeout=60)
fake = httpx.Client(base_url="http://127.0.0.1:28301", timeout=10, trust_env=False)
stubs = httpx.Client(base_url="http://127.0.0.1:28200", timeout=10, trust_env=False)
gw = httpx.Client(
    base_url="https://127.0.0.1:28443",
    verify=False,  # noqa: S501 (the stack's throwaway test CA, loopback port)
    trust_env=False,
    timeout=15,
    headers={"Host": "webhook.example.invalid", "X-Telegram-Bot-Api-Secret-Token": SECRET},
)
chats: dict[int, int] = {}  # chat id -> last relayed message id
lock = threading.Lock()


def log(*a: object) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def call(method: str, **payload: object) -> dict:
    """One request at a time; on 429 wait retry_after (+1 s), then try again."""
    while True:
        try:
            r = real.post(f"{API}/{method}", json=payload)
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log(method, "error", type(exc).__name__, "- waiting 10 s")
            time.sleep(10)
            continue
        if body.get("ok"):
            return body
        wait = (body.get("parameters") or {}).get("retry_after")
        log(method, "failed", r.status_code, str(body.get("description", ""))[:80])
        if r.status_code in (429, 409):
            time.sleep((wait or 10) + 1)
            continue
        return body


def print_code(chat_id: int, update_id: int) -> None:
    ref = hmac.new(REF_KEY, str(chat_id).encode(), hashlib.sha256).hexdigest()
    lrid = uuid.uuid5(LINK_NS, f"{ref}:{update_id}")
    for _ in range(20):
        r = stubs.get("/auth/_test/codes", params={"link_request_id": str(lrid)})
        if r.status_code == 200:
            log(">>> code sent by the stand-in Auth service:", r.json()["code"])
            return
        time.sleep(1)
    log("no code found (wrong Farmer ID, or not your own contact?)")


def inbound() -> None:
    call("deleteWebhook")
    me = call("getMe")
    if not me.get("ok"):
        sys.exit("the token was refused")
    log("bot ready: @" + me["result"]["username"] + " - press Start in Telegram")
    offset = 0
    while True:
        ups = call("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
        for up in ups.get("result", []):
            offset = up["update_id"] + 1
            msg = up.get("message") or {}
            chat = msg.get("chat") or {}
            if chat.get("type") != "private":
                continue
            cid = int(chat["id"])
            with lock:
                if cid not in chats:
                    fake.post("/_test/users", json={"telegram_id": cid, "first_name": "Live"})
                    sent = fake.get("/_test/sent", params={"telegram_id": cid}).json()
                    chats[cid] = max((m["message_id"] for m in sent["messages"]), default=0)
            kind = "contact" if "contact" in msg else ("text" if "text" in msg else "other")
            status = gw.post("/telegram/webhook", json=up).status_code
            log("update in:", kind, "->", status)
            if kind == "contact" and status == 200:
                threading.Thread(
                    target=print_code, args=(cid, up["update_id"]), daemon=True
                ).start()


def outbound() -> None:
    while True:
        with lock:
            items = list(chats.items())
        for cid, last in items:
            try:
                sent = fake.get("/_test/sent", params={"telegram_id": cid}).json()["messages"]
            except httpx.HTTPError:
                continue
            for m in sent:
                if m["message_id"] <= last:
                    continue
                payload: dict = {"chat_id": cid, "text": m["text"]}
                if m.get("reply_markup"):
                    payload["reply_markup"] = m["reply_markup"]
                if m.get("link_preview_options"):
                    payload["link_preview_options"] = m["link_preview_options"]
                ok = call("sendMessage", **payload).get("ok")
                log(
                    "message out:",
                    (m["text"] or "")[:40].replace("\n", " "),
                    "ok" if ok else "FAILED",
                )
                last = m["message_id"]
                with lock:
                    chats[cid] = last
        time.sleep(1)


threading.Thread(target=outbound, daemon=True).start()
inbound()
