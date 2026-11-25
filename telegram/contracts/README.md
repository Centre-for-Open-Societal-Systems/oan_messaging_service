# Telegram: contracts (broadcasts plus sign-up)

These files are the agreement between the parts of the local Telegram stack. A worker who
needs a change stops and asks the control session, which changes the file in its own PR
first.

Source of truth: Channels Specification v2.0, chapters 5 and 6 (`docs/channels-spec/`),
as revised on 24 November 2026 for Telegram broadcasts plus sign-up (TG-36 to TG-46). Where a file
here and the spec disagree, the spec wins. Report the difference.

## Scope of this release

RapidPro is not used (TG-36). OAN Bot Services receives the webhook (`POST
/telegram/webhook`) and runs a short button conversation: language, privacy notice, main
menu, farmer linking, staff linking, Updates on/off and Stop. It sends every reply, and
every area broadcast copy, through OAN Messaging. Market prices, questions to Development
Agents, pest reports, media and the weekly report are deferred.

The local stack runs on one machine in Docker. Nothing reaches the real Telegram, 8028 or
any OAN service. A test plays the farmer, and a fake Telegram server records what OAN
Messaging sends. There is no RapidPro, Garage, DynamoDB, Elasticsearch or Valkey.

## Files

| File | What it fixes |
|---|---|
| `bot-services.openapi.yaml` | The routes RapidPro flows would call on Bot Services. **Not mounted in this release** (`OAN_BOT_RAPIDPRO_ENABLED=false`, TG-43); kept for a later release |
| `../../integrations/kits/oan_da_app_services/broadcast-channels/docs/bot-services-broadcasts.openapi.yaml` | `POST /broadcasts` and its reads, as the DA backend calls them. **Unchanged; reused as is.** |
| `stubs/da-backend.openapi.yaml` | R1, R2, R3 and R4 (§6.4.5); this release uses the staff linking and roster routes |
| `stubs/auth.openapi.yaml` | Linking OTP: issue and verify (§6.6) |
| `../../integrations/kits/oan_da_app_services/broadcast-channels/docs/beneficiary-api.openapi.yaml` | Beneficiary API (ID-01). **Reused as is.** |
| `stubs/master-data.openapi.yaml` | Administrative areas (§5.6) |
| `stubs/marketplace.openapi.yaml` | Commodities, markets and prices (§6.2). Not used in this release |
| `stubs/clock.md` | The fake clock |
| `fake-telegram.md` | What the fake Telegram server does, and how a test drives it |
| `oan-messaging-caller.md` | Bot Services as an OAN Messaging Telegram caller: replies with buttons, broadcast copies, cancel (contract 3.4.0) |
| `text-keys.yaml` | Every bot text, English and Amharic (Amharic is a release gate, TG-39) |
| `versions.md` | Pinned versions of every image in the local stack |

## Folders and owners

| Folder | Owner | Notes |
|---|---|---|
| `telegram/contracts/` | Control session | Frozen. |
| `telegram/stack/` | Worker 1a | Docker Compose stack, fake Telegram, certificates, start and reset scripts |
| `oan_bot_services/` (its migrations inside it), `tests/bot_services/` | Worker 1b | The Bot Services package, its own database migrations and its tests |
| `oan_messaging/` (small changes listed in `oan-messaging-caller.md` only) | Worker 1b | Nothing else in `oan_messaging/` changes |
| `telegram/stubs/` | Worker 1c | One small server for all stubs, plus the fake clock |
| `telegram/tests/` | Control session, after 1a to 1c | The end-to-end test driver |

Two workers never edit the same file. `pyproject.toml` and `uv.lock` belong to worker 1b;
the others ask 1b, through the control session, if they need a dependency.

## Rules every part follows

- `contact_ref` = HMAC-SHA256 of the chat id with the `contact_ref` key. It is the only
  farmer identifier in logs, metrics and calls to other services (§5.4). The exceptions are
  the beneficiary API and Auth during linking, and the chat id sent to OAN Messaging for a
  reply or a broadcast copy.
- No message text, full phone number, chat id, Farmer ID, code or token in logs.
- Test phone numbers are `0900000xxx` only. Hosts are `example.invalid` or Docker
  service names. Secrets are generated on the machine by the stack's init script and never
  committed.
- Every bot text comes from a key in `text-keys.yaml`; no sentences in code (TG-39).
- A repeated Telegram `update_id` changes nothing; every OAN Messaging call carries a
  stored idempotency key, so a retry is harmless.
