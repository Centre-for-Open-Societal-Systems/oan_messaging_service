# Bot Services as an OAN Messaging Telegram caller

SMS-02 revised (23 November 2026): the Telegram copy of each area broadcast is sent by OAN
Messaging. Bot Services chooses the recipients and submits one message per recipient. The
DA-facing `POST /broadcasts` contract does not change.

## Caller registration (OAN operator, done by the stack's init script)

```
python -m oan_messaging.runtime caller --caller bot-services --channels telegram \
    --purposes alert,broadcast --bot oan
python -m oan_messaging.runtime issue-credential --caller bot-services
```

The worker reads the bot token from `OAN_MSG_TELEGRAM_BOT_TOKEN__OAN` (a Kubernetes Secret
in clusters; generated on the machine in the stack). Courier holds the same token in its
channel configuration. One rotation runbook updates both places (§5.3).

## One message

`POST /api/v1/messages/telegram` with `Authorization: Bearer <bot-services token>`:

| Field | Value |
|---|---|
| `Idempotency-Key` header | A fresh UUID4 per recipient, stored with the recipient row **before** the call, so a retry reuses it |
| `chat_id` | From the subscription row (farmers, TG-33) or R1 `telegram_id` (Development Agents) |
| `text` | `title` + newline + newline + `body`, plain text (TG-34). If it is over 4096 UTF-16 units, Bot Services refuses the broadcast with `validation_failed` (field `body`) before sending anything |
| `purpose` | `Emergency`, `Weather` → `alert`; `Knowledge`, `Informational` → `broadcast` (SMS-06) |
| `expires_at` | The broadcast's `expires_at`; if absent, Bot Services sets 24 hours ahead (inside OAN's horizon) |
| `source_reference` | `bc:<broadcast_id>:<n>`, where `n` is the recipient's number in the broadcast. Never a Farmer ID, chat id or name |

## Order (TG-35)

Bot Services submits round-robin across the woredas of the broadcast: one recipient from
woreda A, one from B, and so on. OAN Messaging sends oldest first within a purpose, so
every woreda makes progress.

## Reading results

Bot Services polls `GET /api/v1/messages/{id}` for recipients not yet final, at most once
every 10 seconds per broadcast, in pages. Status names are passed through unchanged to
`GET /broadcasts/{id}` counts.

| OAN result | Bot Services does |
|---|---|
| `sent` | Counted `sent`; the Farmer ID goes into `telegram-recipients` |
| `failed`, failure code `recipient_blocked_bot` or `chat_not_found` (the adapter maps "blocked by the user", "kicked", "deactivated", "chat not found") | Counted `failed`; subscription marked `unreachable`, so later broadcasts skip it; for staff, R3 is called (TG-09) |
| `failed`, other reason | Counted `failed` |
| `expired` | Counted `expired`; gets the SMS copy |
| `unknown` | Counted `unknown`; never resent; gets the SMS copy |

The failure codes come from `FailureCode` in OAN Messaging (`oan_messaging/adapters/telegram.py:425-444`).
Bot Services maps them in one table, with a test per code; check the exact string values in
`FailureCode` and in the message view the API returns.

## Small changes to OAN Messaging (worker 1b, nothing else in `oan_messaging/`)

1. **Telegram pace from configuration.** Today it is fixed at 25 a second with 2 reserved
   for OTP (`TELEGRAM_DEFAULT`, `worker/rate_limit.py:195`). Add
   `OAN_MSG_TELEGRAM_RATE_PER_SECOND` (default **20**, TG-08; refuse above 30) and
   `OAN_MSG_TELEGRAM_OTP_RESERVE` (default 0: there is no Telegram OTP caller). Add both to
   Helm values.
2. **Link previews off** on every Telegram send (`link_preview_options: {"is_disabled": true}`)
   (§6.8.4). This is internal to the adapter; the API contract is unchanged.
3. **Telegram API address from configuration**, `OAN_MSG_TELEGRAM_API_BASE_URL` (default
   `https://api.telegram.org`, https only, as the adapter already checks), so other
   environments can point it elsewhere if needed. The stack uses the default name.
4. Tests for each change. Contract version stays **3.2.0**: no request, response or status
   changes.
