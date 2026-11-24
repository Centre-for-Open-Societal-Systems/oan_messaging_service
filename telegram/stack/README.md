# Local Telegram stack

OAN Messaging and OAN Bot Services, as released for Telegram broadcasts plus sign-up
(no RapidPro), with a fake Telegram, a fake 8028 SMS gateway, the webhook gateway and the
stubs, all in Docker on one machine. Nothing reaches the internet, the real Telegram,
8028 or any OAN service. Contracts: `telegram/contracts/`.

## Run it

```bash
cd telegram/stack
./init-env.sh     # once: every secret, random, into .env (mode 600, git-ignored)
./up.sh           # build missing images, start, set up by script
cd ../.. && pytest telegram/tests     # the end-to-end tests (about 10 minutes)
cd telegram/stack
./down.sh         # stop, keep data
./reset.sh        # stop and wipe all data (only this project's volumes)
```

`./up.sh --rebuild` rebuilds every image, for example after a code change.

Machine-specific settings, such as a proxy CA for image builds, go in a git-ignored
`compose.override.yaml` next to `compose.yaml`.

## What runs

| Service | Role |
|---|---|
| `oan-db` | PostgreSQL 16: `oan_messaging` (owner and runtime roles) and `oan_bot` |
| `oan-api` | OAN Messaging API over HTTPS (contract 3.4.0), Telegram-only broadcast switch on |
| `oan-worker-sms` | SMS worker, with **no Telegram setting at all**, sending to the fake 8028 over mutual TLS |
| `oan-worker-telegram` | Telegram worker, the only holder of the bot token, Telegram-only broadcast switch on |
| `oan-cleanup` | Retention and purge |
| `bot-services`, `bot-worker` | OAN Bot Services: webhook, conversation, linking, broadcasts (`OAN_BOT_RAPIDPRO_ENABLED=false`) |
| `stubs` | DA backend, Auth, beneficiary API, Master Data, Marketplace, fake clock (`telegram/stubs/`) |
| `fake-telegram` | `api.telegram.org` (network alias) and the `/_test` routes (`tools/fake_telegram.py`) |
| `fake-8028` | `sms8028.example.invalid` with mutual TLS, and `/_test` routes (`tools/fake_8028.py`) |
| `tg-gateway` | Stand-in for the APISIX webhook route: only `POST /telegram/webhook` with the secret header |
| `edge` | Forwards the loopback ports below; nothing else |
| one-shot | `certs` (test CA), `oan-migrate`, `oan-grants`, `bot-migrate`; `oan-setup` and `tg-setup` run by `up.sh` |

`up.sh` registers the callers (`bot-services`: Telegram, `transactional,alert,broadcast`;
`sms-test`: SMS, standing in for the DA backend's SMS copy), issues their tokens into
`.env`, resumes both channels and registers the webhook with the secret and
`allowed_updates: [message]`.

Every image of ours runs as a non-root user with a read-only file system and no
capabilities. Test phone numbers are `0900000xxx`; hosts are `example.invalid` or Docker
service names.

## Ports (host, 127.0.0.1 only)

| Port | Goes to |
|---|---|
| 28301 | Fake Telegram `/_test` routes |
| 28302 | Fake 8028 `/_test` routes |
| 28443 | Webhook gateway (HTTPS, test CA) |
| 28100 | OAN Bot Services (`/broadcasts`, `/healthz`) |
| 28200 | Stubs, including their `/_test` routes |
| 28000 | OAN Messaging API (HTTPS, test CA) |

## Fake Telegram test routes

| Route | What it does |
|---|---|
| `POST /_test/users {telegram_id}` | Creates a fake user (a chat the bot may write to) |
| `POST /_test/updates {telegram_id, text}` or `{telegram_id, contact: {user_id, phone_number}}` | Posts an update to the registered webhook with the secret header |
| `GET /_test/wait?telegram_id=&count=&timeout_ms=` | Waits for the next messages to that chat |
| `GET /_test/sent?telegram_id=` | Every message sent to that chat, with text and buttons |
| `POST /_test/faults {telegram_id, mode}` | `blocked`, `deactivated`, `not_found` (lasting), `rate_limited` (one 429 with `retry_after: 3`), `timeout`, `none` |
| `POST /_test/telegram {down: true}` | Closes the Bot API listener ("connection refused"); `false` opens it again |
| `GET /_test/webhook` | Whether a webhook is registered, with a secret |
| `POST /_test/reset` | Clears users, messages and faults (the webhook stays) |

The fake also enforces Telegram's limits: 30 messages a second for the bot, 1 a second per
chat, 4096 UTF-16 units.

## What the tests do not prove

* Real Telegram: the fake follows the Bot API as documented, not as observed.
* Real upstream services: the DA backend, Auth, the beneficiary API and Master Data are
  stubs that follow the agreed contracts.
* Production scale: a handful of chats per test, not the region-wide load test (§7.5).
* The gateway is a small stand-in for APISIX; the real route and its source-address
  allowlist are tested in the dev environment.
