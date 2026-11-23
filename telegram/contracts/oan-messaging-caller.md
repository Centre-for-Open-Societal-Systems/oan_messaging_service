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

## SMS must not change (rule for every OAN Messaging change)

OAN Messaging, the connector and the module kits are already delivered. Telegram is
added next to SMS, never through it:

1. **Separate Telegram worker.** Helm gets an optional second worker Deployment,
   `worker-telegram`, with `OAN_MSG_WORKER_CHANNELS=telegram` and the bot token. It is off by
   default. The existing worker keeps `channels: [sms]` and gets no Telegram token, so a
   missing or wrong bot token can never stop SMS. The claim query already filters by the
   worker's channels (`claim_due`), so SMS workers never pick up Telegram messages.
2. **Existing values render the same.** `helm template` with today's `values.yaml`,
   `values-pilot.yaml` and every file in `ci/` must give the same output as before, except
   new objects that are off by default. Add a regression check for this.
3. **SMS code paths untouched.** No change to the SMS adapter, batching, SMS pacing,
   receipts, the connector or the API contract (3.2.0). The Telegram pace settings only
   change the Telegram limit.
4. **Full existing suite passes unchanged.** No existing test is edited to make it pass. The only exception is chart tests that gain the new `channel` field (see Statistics below); they change no number.
   Approved during worker 1b (23 Nov 2026), each changing one value and no behaviour:
   - `tests/adapters/test_telegram.py` and `tests/integration/test_int_pipeline.py`: the
     pinned `sendMessage` body gains `"link_preview_options": {"is_disabled": true}`;
   - `tests/integration/test_int_process.py`: the expected migration head goes from
     `"0006"` to `"0007"`.
   In the end no chart test needed an edit.

## Statistics: all channels, labelled (decided 23 Nov 2026)

The one visible change to a delivered feature, chosen by the owner: charts keep counting
every channel (as today), and **every row shows its channel**, so readers can tell SMS
from Telegram once Telegram broadcasts start.

- Every chart row that counts messages carries `channel` (`sms` or `telegram`). Where a
  chart today has one row per key (for example, per caller or per purpose), it gets one row
  per key and channel. The `channel` filter keeps working.
- `msgKpis` and `msgTrend` give totals per channel as well as the overall total.
  **Shape (decided 23 Nov 2026, built in PR #23):** `msgKpis` stays one row, labelled
  `channel: "all"`, so Reports Central's `data[0]` is still the total. It adds `by_channel`,
  which always lists `sms` and `telegram` (zeros when there is no traffic). With the
  `channel` filter, "all" is the filtered set and `by_channel` lists only the filtered
  channels. `msgTrend` has the same shape per period. Every other chart gives one row per
  key and channel.
- Broadcast campaigns (`stats_campaigns`) get a `channel` column (a migration). The
  Telegram copy (`bc:<broadcast_id>:…`) and the SMS copy of one broadcast show as two
  labelled rows.
- This adds response fields, so the contract goes to **3.3.0** (minor; nothing removed or
  renamed). Update `docs/openapi.yaml`, the statistics doc and
  `docs/integrations/reports-central.md` (tell Reports Central that totals now include
  Telegram and how to split them).
- Existing chart tests keep passing; where an expected row gains `channel`, the test adds
  the field and changes no number.
