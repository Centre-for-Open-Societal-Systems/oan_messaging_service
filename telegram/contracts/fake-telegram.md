# Fake Telegram server

A small HTTPS server that stands in for `api.telegram.org`, owned by worker 1a. Nothing in
the stack can reach the real Telegram.

## How courier and OAN Messaging reach it

- Courier has the Telegram address fixed in its code (`const apiURL =
  "https://api.telegram.org"`, courier `handlers/telegram/handler.go`). So in the Docker
  network the fake server carries the **network alias `api.telegram.org`**. Its
  certificate is issued by the stack's own test CA, and courier trusts that CA through
  `SSL_CERT_FILE`. The stack has no route to the internet, so the real host can never be
  reached by mistake.
- OAN Messaging's Telegram adapter takes its address from configuration (`api_base_url`,
  https only). It uses `https://api.telegram.org` the same way and trusts the same CA.
- The fake server accepts any bot token of the form `<digits>:<35 characters>`. The stack
  generates one test token at start; it is never committed.

## Bot API methods it answers

| Method | Behaviour |
|---|---|
| `getMe` | The test bot (`id`, `is_bot: true`, `username: oan_test_bot`) |
| `setWebhook`, `deleteWebhook`, `getWebhookInfo` | Records the URL and `secret_token`; `getWebhookInfo` reports them, plus `pending_update_count` and `last_error_message` |
| `sendMessage`, `sendPhoto`, `sendDocument`, `sendAudio`, `sendVoice`, `sendVideo`, `sendChatAction` | Records the call (chat id, text, `reply_markup`, media URL, `link_preview_options`), returns a Telegram-shaped success with a new `message_id`. For a media URL, it fetches the URL, so the Garage media route is tested too. |
| `getFile`, then `GET /file/bot<token>/<path>` | Serves files that the test attached to an incoming update |

Every answer has Telegram's shape (`{"ok": true, "result": …}` or `{"ok": false,
"error_code": …, "description": …}`).

## How a test plays a farmer (`/_test` routes)

`/_test` routes are on a separate plain-HTTP port (8301), reachable only inside the
Docker network and from the host for tests.

| Route | What it does |
|---|---|
| `POST /_test/users` `{telegram_id, first_name, language_code}` | Creates a fake Telegram user |
| `POST /_test/updates` `{telegram_id, text}` or `{telegram_id, contact: {user_id, phone_number}}` or `{telegram_id, photo|voice|document: <test file>}` | Builds a Telegram update (new `update_id` and `message_id`) and posts it to the registered webhook with the `X-Telegram-Bot-Api-Secret-Token` header. Returns courier's HTTP status. |
| `GET /_test/sent?telegram_id=&after=` | Messages the bot sent to that chat, in order, with text and keyboard buttons |
| `GET /_test/wait?telegram_id=&count=&timeout_ms=` | Waits until that many new messages arrive (default timeout 10 s), so tests need no sleeps |
| `POST /_test/faults` `{telegram_id, mode}` | Next sends to that chat fail: `blocked` (403 "bot was blocked by the user"), `deactivated` (403 "user is deactivated"), `not_found` (400 "chat not found"), `rate_limited` (429 with `retry_after: 3`), `timeout` (hold the request 30 s, then close), `none` |
| `POST /_test/reset` | Clears users, recorded messages and faults |

## Limits it enforces

- More than 30 sends a second for the bot, or more than 1 a second to one chat, returns
  429 with `retry_after`, as Telegram does. Tests use this to check pacing (TG-08).
- Text over 4096 UTF-16 units returns 400.
- The webhook call to courier carries `allowed_updates` behaviour: only `message` updates
  are sent.

## What it records for privacy checks

The recorded messages hold full content, because tests must read them. They live in
memory only and are cleared on reset. The server's own log prints the method name, the
`contact_ref`-style hash of the chat id and the status, never the text, chat id or token.
