# OAN Bot Services (Phase A, wave 1)

The back end the Telegram bot's RapidPro flows call (Channels Specification v2.0 §5.8),
and the sender of the Telegram copy of DA web app broadcasts through OAN Messaging (§6.8,
SMS-02 revised). Contracts: `telegram/contracts/` (frozen) and the DA kit's
`bot-services-broadcasts.openapi.yaml`.

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
| `FLOW_SECRET` | Shared secret in `X-OAN-Flow-Secret` (RapidPro global), at least 16 characters. |
| `CONTACT_REF_KEY`, `FINGERPRINT_KEY` | base64, at least 32 bytes each. `contact_ref` = HMAC-SHA256 of the chat id. |
| `DATA_KEY` | base64, exactly 32 bytes (AES-GCM for the alternate phone and a broadcast's text until it is sent). |
| `BROADCAST_TOKEN_SHA256` | Comma-separated hex SHA-256 of the bearer tokens the DA backend may use on `/broadcasts`. |
| `CLIENT_ID`, `CLIENT_SECRET`, `TOKEN_URL` | Client credentials for the OAN services (Keycloak; `stubs/auth/token` locally). |
| `DA_URL`, `AUTH_URL`, `BENEFICIARY_URL`, `MASTER_DATA_URL`, `MARKETPLACE_URL` | Service base URLs. |
| `RAPIDPRO_URL`, `RAPIDPRO_TOKEN`, `RAPIDPRO_NOTIFY_FLOW`, `RAPIDPRO_STAFF_GROUP` | RapidPro API, the `notify` flow UUID, the staff group name (default `staff`). |
| `OAN_MSG_URL`, `OAN_MSG_TOKEN` | OAN Messaging and the `bot-services` caller token. |
| `CLOCK_URL` | Fake clock (`stubs/clock.md`). **Refused unless `ENV=local`.** |
| `CLIENT_TIMEOUT` (5), `AREA_CACHE_SECONDS` (86400), `SUBSCRIPTION_INACTIVE_DAYS` (365), `REPLAY_DAYS` (7), `BROADCAST_BINDING_DAYS` (30, at least 30), `POLL_INTERVAL_SECONDS` (10, at least 10), `BATCH_SIZE` (200), `DEFAULT_BROADCAST_HOURS` (24) | Tuning. |

## What it keeps

| Table | Holds | Kept |
|---|---|---|
| `contacts` | RapidPro contact UUID, `contact_ref`, chosen kebele, language. No chat id. | Until Stop |
| `farmer_links`, `subscriptions` | Linked farmers only: Farmer ID, registry area, fingerprint of the registered phone, the alternate number sealed; the subscription row holds the chat id (TG-33). | Until unlink, Stop, a new link, or 12 months without activity |
| `link_requests`, `link_failures`, `link_locks` | Linking in progress and lockouts | A day / an hour |
| `staff_links` | `contact_ref` → DA ref and role | Until Stop |
| `keyed_queue` | Text keys and parameters for the `notify` flow | Until released or Stop |
| `broadcasts`, `broadcast_recipients`, `broadcast_bindings` | Broadcast, per-recipient status and OAN message id; chat id only until OAN Messaging has the message; text only until every recipient is submitted | 30 days after finishing |
| `request_replays` | The answer to each flow `request_id` | 7 days |

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
Farmer ID, phone, code or token the tests used.
