# OAN Bot Services

The sender of the Telegram copy of DA web app broadcasts through OAN Messaging (§6.8,
SMS-02 revised), and the bot's back end. Contracts: `telegram/contracts/` and the DA
kit's `bot-services-broadcasts.openapi.yaml`.

Two runtimes, chosen by `OAN_BOT_RAPIDPRO_ENABLED`:

* **false (default): broadcast-only Telegram without RapidPro.** Telegram calls
  `POST /telegram/webhook`; Bot Services stores each update (sealed) before answering,
  and its worker runs a short conversation: `/start` → language (English, Amharic) →
  privacy notice and main menu (Link my Farmer ID, I am OAN staff, Updates on/off, Stop);
  farmer linking with the contact button and an OTP Auth sends to the registered phone;
  staff linking with the DA app code; Updates on/off; Stop. Anything else gets one fixed
  reply ("messages here are not read or answered"). Every reply is a text key from
  `telegram/contracts/text-keys.yaml`, sent through OAN Messaging (purpose
  `transactional`, contract 3.4.0 `reply_markup` for buttons). No RapidPro client is
  built, no RapidPro job runs, and the flow routes are not mounted.
* **true: the RapidPro flows (Phase A, wave 1)**, kept but switched off. The flow routes
  are mounted and the webhook route is not.

Routes: `GET /healthz`, `POST /broadcasts`, `GET /broadcasts/{id}`,
`GET /broadcasts/{id}/telegram-recipients` in both; `POST /telegram/webhook` without
RapidPro; the flow routes of `bot-services.openapi.yaml` (`x-runtime: rapidpro`) with it.

## Run

```bash
python -m oan_bot_services migrate      # its own database, version table bot_alembic_version
python -m oan_bot_services api          # port 8100
python -m oan_bot_services worker       # one replica at a time (advisory lock)
```

## Settings (environment, prefix `OAN_BOT_`)

| Setting | Meaning |
|---|---|
| `ENV` | `local`, `dev`, `staging` or `production` (default). |
| `DATABASE_URL` | `postgresql+psycopg://…` (Secret). |
| `RAPIDPRO_ENABLED` | `false` (default): the reduced runtime. `true`: the RapidPro flows. |
| `TELEGRAM_WEBHOOK_SECRET` | Required when RapidPro is off: the bot's webhook `secret_token`, 16-256 of `A-Z a-z 0-9 _ -` (Secret). The same value is set on APISIX and in `setWebhook`. |
| `TEXT_KEYS_FILE` | Path of `text-keys.yaml`. Default: `telegram/contracts/text-keys.yaml` next to the package; an image must copy that file. Checked at start-up (every key, both languages, buttons at most 30 units). |
| `STOPPED_RETENTION_DAYS` (90), `UPDATE_ID_DAYS` (7), `CONVERSATION_MINUTES` (30), `REPLY_MINUTES` (30) | Retention of stopped or unreachable subscriptions; repeat detection of webhook updates; a conversation step's life; a reply not handed to OAN Messaging by then is dropped. |
| `FLOW_SECRET` | RapidPro runtime only: shared secret in `X-OAN-Flow-Secret` (RapidPro global), at least 16 characters. |
| `CONTACT_REF_KEY`, `FINGERPRINT_KEY` | base64, at least 32 bytes each. `contact_ref` = HMAC-SHA256 of the chat id. |
| `DATA_KEY` | base64, exactly 32 bytes (AES-GCM for the alternate phone and a broadcast's text until it is sent). |
| `BROADCAST_TOKEN_SHA256` | Comma-separated hex SHA-256 of the bearer tokens the DA backend may use on `/broadcasts`. |
| `CLIENT_ID`, `CLIENT_SECRET`, `TOKEN_URL` | Client credentials for the OAN services (Keycloak; `stubs/auth/token` locally). |
| `DA_URL`, `AUTH_URL`, `BENEFICIARY_URL`, `MASTER_DATA_URL`, `MARKETPLACE_URL` | Service base URLs. |
| `RAPIDPRO_URL`, `RAPIDPRO_TOKEN`, `RAPIDPRO_NOTIFY_FLOW`, `RAPIDPRO_STAFF_GROUP` | RapidPro runtime only: RapidPro API, the `notify` flow UUID, the staff group name (default `staff`). Not read when RapidPro is off. |
| `OAN_MSG_URL`, `OAN_MSG_TOKEN` | OAN Messaging and the `bot-services` caller token. Without RapidPro the caller needs purpose `transactional` (replies) besides `alert,broadcast`, and OAN Messaging 3.4.0 (`reply_markup`, cancel route). |
| `CLOCK_URL` | Fake clock (`stubs/clock.md`). **Refused unless `ENV=local`.** |
| `CLIENT_TIMEOUT` (5), `AREA_CACHE_SECONDS` (86400), `SUBSCRIPTION_INACTIVE_DAYS` (365, RapidPro runtime only; no inactivity expiry otherwise), `REPLAY_DAYS` (7), `BROADCAST_BINDING_DAYS` (30, at least 30), `POLL_INTERVAL_SECONDS` (10, at least 10), `BATCH_SIZE` (200), `DEFAULT_BROADCAST_HOURS` (24) | Tuning. |

## What it keeps

| Table | Holds | Kept |
|---|---|---|
| `contacts` | `contact_ref` (lookup key), OAN-owned `subscriber_id`, language, chosen kebele; the RapidPro contact UUID only with RapidPro. No chat id. | Without RapidPro: while the chat is linked or active; 90 days after a Stop with no link, or with its deleted subscription. With RapidPro: until Stop |
| `farmer_links`, `subscriptions` | Linked farmers only: Farmer ID, registry area, fingerprint of the registered phone, the alternate number sealed; the subscription row holds the chat id (TG-33), state (`active`, `stopped`, `unreachable`) and `stopped_at`. | Without RapidPro: until a new link, or 90 days after Stop, a block or a deleted chat (updates on before then restores it; no inactivity expiry). With RapidPro: until unlink, Stop, a new link, or 12 months without activity |
| `telegram_updates` | A received update's minimal fields, sealed (chat id, sender id, text, shared contact) | Until the worker handles it |
| `seen_updates` | `update_id` only | 7 days |
| `conversations` | The chat's step; Farmer ID and link request id sealed | 30 minutes |
| `outbox` | A reply: text key, params, buttons, chat id, stored idempotency key | Until OAN Messaging has it (30 minutes at most) |
| `link_requests`, `link_failures`, `link_locks` | Linking in progress and lockouts | A day / an hour |
| `staff_links` | `contact_ref` → DA ref and role | Until Stop |
| `keyed_queue` | Text keys and parameters for the `notify` flow | Until released or Stop |
| `broadcasts`, `broadcast_recipients`, `broadcast_bindings` | Broadcast, per-recipient status and OAN message id; chat id only until OAN Messaging has the message; text only until every recipient is submitted. A copy no longer eligible at submit time is `failed`/`not_eligible`; one cancelled in OAN Messaging (Updates off or Stop) is `failed`/`cancelled` | 30 days after finishing |
| `request_replays` | The answer to each flow `request_id` | 7 days |

No inbound chat history is kept: an update is deleted once handled, and a code the farmer
types lives only in that sealed update until then; codes are never logged.

Logs carry `contact_ref`, generated ids and fixed words only. A filter also scrubs
anything token-, phone-, chat-id- or Farmer-ID-shaped, and `httpx` request logging is off
(a beneficiary URL holds the Farmer ID).

## Tests

```bash
OAN_MSG_TEST_DATABASE_URL=postgresql+psycopg://postgres:<pw>@127.0.0.1:<port>/postgres \
  uv run pytest tests/bot_services --cov=oan_bot_services
```

Every other service is a fake that validates each request and answer against its contract
file. `test_zz_logs.py` fails if any log line written during the run holds a chat id,
Farmer ID, phone, code, token or message text the tests used. RapidPro tests run with the
switch on (`bot` fixture); the reduced runtime uses the `tg` fixture, which has no RapidPro
setting and fails if anything calls RapidPro.
