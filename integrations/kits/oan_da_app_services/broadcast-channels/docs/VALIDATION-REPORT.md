# DA broadcast: validation against the Channels Specification

**Checked:** 20 November 2026; rebased and checked again 23 November 2026; SMS and Telegram
made independent 24 November 2026
**Code:** `oan_da_app_services` `develop` `656985b` and `oan_da_app_ui` `develop` `b99685e`
(first checked on `2ae3d13` and `4e9dd4d`)
**Against:** Channels Specification v2.0 §6.8 (area broadcasts) and §8.4 (ID-03, ID-09), and
the DA team's own notes (`docs/api-da-screens.md`, `docs/data-model.md`)

The rule for this package was to fit what is built and change nothing that works, except
real errors or oversights. Each finding says which it is and what the package does about it.

## What is built and works

| Area | Where | State |
|---|---|---|
| In-app broadcast to agents | `services/notifications.py` `broadcast()`, `POST /notifications/broadcast` | Works. Scopes are all, region, zone, woreda or kebele. `client_op_id` is unique at the database, the write is atomic, and each send is audited |
| Who may target what | `_assert_broadcast_scope_allowed(scope, scope_value)` (`services/notifications.py:229`) | Works. Since `656985b` it has no role branches: a sender may target an area they may read (read on DA Region / Zone / Woreda / Kebele under their User Permissions), and "all" needs no area User Permission. Who may send at all is DA Broadcast `create` (Role Permission Manager) |
| Agents of a kebele | `_kebele_recipients` | Works. An Effective `DA Kebele Assignment` plus an active Development Agent grant |
| Broadcast screen | `BroadcastWorkspace.tsx` | Composes, picks the area, and sends in-app to agents |

The package reuses all of these unchanged.

## Findings

| # | Finding | Where | Kind | In this package |
|---|---|---|---|---|
| V-01 | **"All farmers" is offered to every role that can send** (DA, Supervisor, Communications Officer, Admin). The DA team's notes say farmer broadcasts belong to the Communications Officer ("FSD gives broadcast to the Communications Officer", `docs/api-da-screens.md:592`) | `BroadcastWorkspace.tsx:153` | Oversight | UI patch: offered to the Communications Officer and the Admin only |
| V-02 | **The SMS segment counter divides by 160 for every text.** One Amharic letter makes a part 70 characters, so Amharic messages show too few segments | `BroadcastWorkspace.tsx:159`, `SmsPreview.tsx:3`, `SendToFarmersModal.tsx:54` | Error | UI patch: counts as OAN Messaging does (70/67 with Amharic, 160/153 otherwise). Warns when the SMS turns multi-part; blocks beyond 3 parts |
| V-03 | **The screen always requires a woreda.** The backend accepts region and zone, and farmer broadcasts may cover a whole region or zone | `BroadcastWorkspace.tsx` (Woreda field `required`) | Gap | UI patch: for All farmers, Zone and Woreda are optional, and the deepest level chosen is the area. Broadcasts to agents are unchanged |
| V-04 | **SMS and Telegram boxes were disabled** ("not wired yet") | `BroadcastWorkspace.tsx` channels fieldset | Gap (ID-09) | Add-on app and UI patch wire them |
| V-05 | **A Supervisor without the DA role gets 403.** The screen lets a Supervisor send (`canDispatch`), the scope rule has a Supervisor branch, but the route admits only Communications Officer, Admin and DA | `api/v1/notifications.py:74` | Mismatch | **Not changed.** The add-on route admits the same roles. The DA team decides whether Supervisors need the DA role or the route should admit them |
| V-06 | **Evidence files are collected but never sent or stored** | `BroadcastWorkspace.tsx:176` | Gap | **Not changed.** Spec §6.8 sends them on Telegram as media. Needs storage on the DA side first |
| V-07 | **The Knowledge Base "Send to farmers" dialog is a mock.** It uses fixed woredas and sizes and sends nothing | `SendToFarmersModal.tsx` (`BROADCAST_WOREDAS`, `audienceSize`) | Gap (FR-11) | Only its segment counter is fixed (V-02). Wiring it to `POST /broadcasts` is a follow-up |
| V-08 | **Gathering SMS is not sent.** The after-commit job is an empty placeholder, so rows stay Queued | `services/gatherings.py:103` | Gap | **Not changed.** It can use `queue_sms` as the visit kit does |
| V-09 | **No farmer list by area code with phones.** `DA Farmer Reference` holds kebele *names*. The registry client filters by names, and Dev ignored the filter | `integrations/farmer_registry/http.py:553` | Gap (ID-01/04) | The add-on reads the proposed beneficiary API ([`BENEFICIARY-API-REQUIREMENTS.md`](BENEFICIARY-API-REQUIREMENTS.md)); a stub serves it until it is live |
| V-10 | **The DA backend parts of ID-03 are still missing:** Telegram link (D4), routing mode (D5), routes R1–R4 | none | Gap (ID-03) | Not in this package. Telegram staff features wait on them; Bot Services returns agents not on Telegram meanwhile |
| V-11 | **`DA Broadcast` records neither the recipient group nor the channels** | `doctype/da_broadcast` | Design | **Not changed.** The add-on keeps them in its own `DA Broadcast Dispatch`, linked one-to-one |
| V-12 | **Area code formats differ:** `DA Region/Zone/Woreda/Kebele` use bare P-codes (`ET04080101`), `DA Administrative Area` examples use `ET04-W01-K03`, and the new Dev catalogue uses `kebele-ET04…` | doctype descriptions; `oan-helm` commons-services values | Risk | The add-on checks the area code against `DA Region/Zone/Woreda/Kebele`. One format must be settled with the registry (B-1) |

## Rebase on `656985b` (23 November 2026)

`ScopeContext` left `da_services/utils/permissions.py`, and `broadcast(...)` and
`_assert_broadcast_scope_allowed(scope, scope_value)` no longer take it, so the add-on failed
on install. The add-on now uses the DA backend's permission code the way the backend does:

- `POST /broadcasts` checks `require_permission("DA Broadcast", "create")`, as
  `POST /notifications/broadcast` does; `GET /broadcasts/<id>` checks read on DA Broadcast.
- The area is checked by the backend's own `_assert_broadcast_scope_allowed`, unchanged.
- The add-on keeps one rule of its own: farmer broadcasts are for the Communications Officer
  and the Admin (OAN Administrator, System Manager, Administrator).

What changed for senders (the DA backend's decision, accepted on 23 November).
Each level is checked on its own DocType, and User Permissions are created only at the
levels a grant names, so a grant limits that level and the levels below it:

| Sender | Before (`2ae3d13`) | Now (`656985b`) |
|---|---|---|
| Communications Officer, Region-scoped | Only inside their Region | Their Region, and the Zones in it. A Woreda or Kebele is checked on its own DocType, which links only to its Zone or Woreda, so the Region grant does not restrict it |
| Communications Officer, Zone- or Woreda-scoped | Only inside their area | Their area; a Region (and, for a Zone grant, a Kebele) is not restricted by it |
| Development Agent | Own Kebele only | Own Kebele; a Woreda, Zone or Region limited only by the User Permissions their grant gives (none: not limited) |
| Supervisor or Executive | Own Woreda only | Only with a role that has DA Broadcast `create` |
| An area code that does not exist | Refused | Passes the area check; the add-on refuses it (not in Master Data) |

Rechecked: a Region-scoped Communications Officer can send to their Region and to a
Woreda inside it, and is refused for another Region (test
`test_region_scoped_comms_under_the_da_backend_rules`). Wider reach across levels follows
the backend's rule above.

Fixed in this round:

- A farmer whose SMS was not queued was counted and lost; the cursor moved on. Now kept on a
  retry list (Farmer ID or DA ID, language, row number; never the phone), retried with a
  fresh lookup and backoff, then counted as not sent. Done only when the list is empty or spent.
- The scheduler retried every minute after an upstream error. Now it waits
  (`next_run_at`: 1, 2, 4 … minutes, at most 60).
- `source_reference` held the Farmer ID. It now holds the recipient's row number in the
  broadcast (spec 03-sms-service.md: no personal data), the same after a restart or retry.
- Telegram: the fallback wait was checked after `progress()`, so any error there
  skipped it; a failed `create()` left the dispatch Queued for good; and a Telegram 4xx
  failed the whole dispatch, so no SMS went out. Now the wait is checked first and counts
  from the start of the dispatch, and a Telegram failure never stops the SMS copy.
- Double send: the screen never passed `client_op_id`, so each click made a new one. The UI
  patch now creates one per form, reuses it on every Send of that form, and ignores a second
  click while a send is in progress.

## SMS and Telegram independent (24 November 2026)

From this release SMS and Telegram are sent independently. This replaces
the Telegram-first rule of spec §6.8.3 (Telegram, then SMS after 15 minutes to those not
reached on Telegram).

| What | Before | Now |
|---|---|---|
| Order | Telegram first; SMS waited for it, up to `sms_fallback_wait_minutes` (15) | One background job per ticked channel. SMS starts at once; neither waits for the other |
| Who gets the SMS | Farmers minus those Bot Services reached on Telegram; agents not on Telegram | Every farmer of the area with a phone; every agent of the area minus the sender. Someone on both channels may get both copies |
| `telegram-recipients` | Read to drop SMS copies | Not used by the add-on (still served by Bot Services and the stub) |
| Telegram down or 503, SMS ticked | SMS waited for the fallback wait | SMS goes at once. `telegram_error` and `telegram_retry_count` recorded, retried on `telegram_next_run_at` (1, 2, 4 … minutes, at most 60) |
| Telegram 4xx, SMS ticked | Telegram failed; SMS at once to everyone | Telegram failed; SMS as usual |
| Telegram not taken before `expires_at` | (not handled) | Telegram marked failed, so the broadcast can close |
| Done | When the SMS pass ended | When every ticked channel is finished (SMS pass and retries over; Telegram finished or failed). A row lock lets exactly one job close it |
| `sms_fallback_wait_minutes` | Setting, 15 | Retired and removed from the form; nothing reads it |
| Status | `telegram.failed` flag, counts | Same, plus `telegram.error`. `sms.skipped_on_telegram` stays, always 0 |

Unchanged: roles and area rules (region allowed for the Communications Officer and the OAN
Administrator), `client_op_id` idempotency and the Bot Services `Idempotency-Key`, the
beneficiary API contract, SMS-03/SMS-06/SMS-09 rules, the retry list, `POST /broadcasts`
and `GET /broadcasts/<id>` (one key added).

Telegram status words: the Telegram counts are OAN Messaging's statuses (accepted, sending,
sent, failed, expired, unknown). Telegram ends at **sent**; nothing says "delivered" or
"read" for Telegram. The UI patch's Telegram-first wording is replaced (see README).

Found while testing, not changed (as built): in `GET /broadcasts/<id>` the Telegram block's
`failed` key is first the channel flag and is then overwritten by Bot Services' `failed`
count of copies. With counts present, `failed` is the count. The flag is on the dispatch
(`telegram_failed`) and in `telegram.error`. Changing the key would change the read
contract, so it is left for the DA team to decide.

## Dependencies outside the DA team

| Item | Owner | Effect until done |
|---|---|---|
| Beneficiary API routes (ID-01, ID-04) | Farmer registry / DPI | Farmer SMS and the Telegram linking work only against the stub |
| Bot Services `/broadcasts` | OAN messaging (Telegram, wave 1) | Telegram copies work only against the stub |
| `OAN_MSG_BROADCAST_ENABLED` on each environment (SMS-03 ageing is built) | OAN messaging, DevOps | `Knowledge` and `Informational` SMS (purpose `broadcast`) are refused while it is off. `Emergency` and `Weather` (`alert`) are sent now |
| 8028 answers (ID-12) | Delivery team | Multi-part SMS: 8028's guide v1.1 sets no limit; we cap at 3 parts |
