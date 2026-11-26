# Fake Telegram server

A small HTTPS server that stands in for `api.telegram.org`, owned by worker 1a. Nothing in
the stack can reach the real Telegram.

## How OAN Messaging reaches it

- OAN Messaging's `worker-telegram` is the only part that calls Telegram. Its adapter
  takes the address from `OAN_MSG_TELEGRAM_API_BASE_URL` (https only). In the Docker
  network the fake server carries the **network alias `api.telegram.org`**, with a
  certificate from the stack's own test CA, which the worker trusts. The stack has no route
  to the internet, so the real host can never be reached by mistake.
- OAN Bot Services never calls Telegram; it has no bot token.
- The fake server accepts any bot token of the form `<digits>:<35 characters>`. The stack
  generates one test token at start; it is never committed.

## Bot API methods it answers

| Method | Behaviour |
|---|---|
| `getMe` | The test bot (`id`, `is_bot: true`, `username: oan_test_bot`) |
| `setWebhook`, `deleteWebhook`, `getWebhookInfo` | Records the URL and `secret_token`; `getWebhookInfo` reports them, plus `pending_update_count` and `last_error_message` |
| `sendMessage` | Records the call (chat id, text, `reply_markup` with its keyboard buttons or `remove_keyboard`, `link_preview_options`), returns a Telegram-shaped success with a new `message_id`. Media methods are not needed in this release |

Every answer has Telegram's shape (`{"ok": true, "result": …}` or `{"ok": false,
"error_code": …, "description": …}`).

## How a test plays a farmer (`/_test` routes)

`/_test` routes are on a separate plain-HTTP port (8301), reachable only inside the
Docker network and from the host for tests.

| Route | What it does |
|---|---|
| `POST /_test/users` `{telegram_id, first_name, language_code}` | Creates a fake Telegram user |
| `POST /_test/updates` `{telegram_id, text}` or `{telegram_id, contact: {user_id, phone_number}}` | Builds a private-chat Telegram update (new `update_id` and `message_id`) and posts it to the registered webhook, OAN Bot Services' `POST /telegram/webhook`, with the `X-Telegram-Bot-Api-Secret-Token` header. A button tap is the button's text. Returns Bot Services' HTTP status |
| `GET /_test/sent?telegram_id=&after=` | Messages sent to that chat, in order, with text and the reply keyboard's button rows (or `remove_keyboard`) |
| `GET /_test/wait?telegram_id=&count=&timeout_ms=` | Waits until that many new messages arrive (default timeout 10 s), so tests need no sleeps |
| `POST /_test/faults` `{telegram_id, mode}` | Next sends to that chat fail: `blocked` (403 "bot was blocked by the user"), `deactivated` (403 "user is deactivated"), `not_found` (400 "chat not found"), `rate_limited` (429 with `retry_after: 3`), `timeout` (hold the request 30 s, then close), `none` |
| `POST /_test/reset` | Clears users, recorded messages and faults |

## Limits it enforces

- More than 30 sends a second for the bot, or more than 1 a second to one chat, returns
  429 with `retry_after`, as Telegram does. Tests use this to check pacing (TG-08).
- Text over 4096 UTF-16 units returns 400.
- Only `message` updates are posted to the webhook (`allowed_updates: [message]`).

## What it records for privacy checks

The recorded messages hold full content, because tests must read them. They live in
memory only and are cleared on reset. The server's own log prints the method name, the
`contact_ref`-style hash of the chat id and the status, never the text, chat id or token.
