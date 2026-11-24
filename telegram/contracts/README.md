# Telegram Phase A: contracts (frozen)

These files are the agreement between the parts of the local Telegram stack. They are
written before any code and then **frozen**: a worker who needs a change stops and asks
the control session. It changes the file in its own PR first, then the workers rebase.

Source of truth: Channels Specification v2.0, chapters 5 and 6 (`docs/channels-spec/`),
with SMS-02 revised on 23 November 2026. Where a file here and the spec disagree, the spec
wins. Report the difference.

## What Phase A is

Everything runs on one machine in Docker. Nothing reaches the real Telegram, 8028 or any
OAN service. A test plays the farmer, and a fake Telegram server records what the bot
sends. Phase A covers spec steps T1 and T2 (§8.1):

- the platform: RapidPro, Garage, the Bot Services skeleton and the stubs;
- the onboarding flows: language, privacy notice, Link now / Later, Main menu, global
  commands, idle, and Stop with deletion.

It also covers the broadcast path through OAN Messaging, from end to end, against the
stubs.

## Files

| File | What it fixes |
|---|---|
| `bot-services.openapi.yaml` | The routes RapidPro flows call on Bot Services (§5.8), for wave 1 |
| `../../integrations/kits/oan_da_app_services/broadcast-channels/docs/bot-services-broadcasts.openapi.yaml` | `POST /broadcasts` and its reads, as the DA backend calls them. **Unchanged; reused as is.** |
| `stubs/da-backend.openapi.yaml` | R1, R2, R3 and R4 (§6.4.5) |
| `stubs/auth.openapi.yaml` | Linking OTP: issue and verify (§6.6) |
| `../../integrations/kits/oan_da_app_services/broadcast-channels/docs/beneficiary-api.openapi.yaml` | Beneficiary API (ID-01). **Reused as is.** |
| `stubs/master-data.openapi.yaml` | Administrative areas (§5.6) |
| `stubs/marketplace.openapi.yaml` | Commodities, markets and prices (§6.2) |
| `stubs/clock.md` | The fake clock |
| `fake-telegram.md` | What the fake Telegram server does, and how a test drives it |
| `oan-messaging-caller.md` | Bot Services as an OAN Messaging Telegram caller (SMS-02 revised) |
| `text-keys.yaml` | Every text key Phase A flows and Bot Services use |
| `versions.md` | Pinned versions of every image |

## Folders and owners

| Folder | Owner | Notes |
|---|---|---|
| `telegram/contracts/` | Control session | Frozen. |
| `telegram/stack/` | Worker 1a | Docker Compose stack, fake Telegram, certificates, start and reset scripts |
| `oan_bot_services/` (its migrations inside it), `tests/bot_services/` | Worker 1b | The Bot Services package, its own database migrations and its tests |
| `oan_messaging/` (small changes listed in `oan-messaging-caller.md` only) | Worker 1b | Nothing else in `oan_messaging/` changes |
| `telegram/stubs/` | Worker 1c | One small server for all stubs, plus the fake clock |
| `telegram/flows/`, `telegram/tests/` | Control session, after 1a to 1c | Flows exported from RapidPro, and the end-to-end test driver |

Two workers never edit the same file. `pyproject.toml` and `uv.lock` belong to worker 1b;
the others ask 1b, through the control session, if they need a dependency.

## Rules every part follows

- `contact_ref` = HMAC-SHA256 of the chat id with the `contact_ref` key. It is the only
  farmer identifier in logs, metrics and calls to other services (§5.4). The exceptions are
  the beneficiary API and Auth during linking, and the chat id sent to OAN Messaging for a
  broadcast.
- No message text, full phone number, chat id, Farmer ID, code or token in logs.
- Test phone numbers are `0900000xxx` only. Hosts are `example.invalid` or Docker
  service names. Secrets are generated on the machine by the stack's init script and never
  committed.
- Errors to flows are `{error, text_key}`. Calls that change state carry `request_id`, so
  a repeat is harmless (§5.8). Every flow call answers within 5 seconds.
- Bot Services returns **text keys and parameters**, never sentences (TG-02).
