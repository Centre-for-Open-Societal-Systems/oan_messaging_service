"""Register the bot's webhook with the secret, as DevOps does with setWebhook (spec §5.3).

Runs inside the stack (tools image); talks to the fake api.telegram.org. Prints only
whether it worked, never the token or the secret.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.request

TOKEN = os.environ["TG_BOT_TOKEN"]
SECRET = os.environ["TG_WEBHOOK_SECRET"]
URL = os.environ.get("TG_WEBHOOK_URL", "https://webhook.example.invalid/telegram/webhook")


def call(method: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(payload or {}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context(cafile="/certs/ca.crt")
    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:  # noqa: S310 (fixed URL)
        return json.loads(resp.read())


def main() -> None:
    out = call(
        "setWebhook",
        {"url": URL, "secret_token": SECRET, "allowed_updates": ["message"], "max_connections": 20},
    )
    info = call("getWebhookInfo")["result"]
    ok = out.get("ok") and info.get("url") == URL and info.get("allowed_updates") == ["message"]
    print("webhook registered" if ok else "webhook registration failed")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
