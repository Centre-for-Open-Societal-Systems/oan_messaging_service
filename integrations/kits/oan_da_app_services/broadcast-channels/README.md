# DA Services kit: broadcasts on SMS and Telegram

| | |
|---|---|
| **For** | The DA lead developer: validate, then integrate into `oan_da_app_services` and `oan_da_app_ui` |
| **Base** | `oan_da_app_services` `develop` `656985b`; `oan_da_app_ui` `develop` `b99685e` (rebased 23 November 2026; earlier base `2ae3d13` / `4e9dd4d`) |
| **Backend** | `app/da_broadcast_channels/`, a Frappe app installed next to `da_services`. **`da_services` is not changed** |
| **UI** | `ui/0001-feat-broadcast-send-on-SMS-and-Telegram-All-farmers-.patch` (6 files) |
| **Upstream stand-ins** | `stubs/stub_server.py` (beneficiary API and Bot Services, standard library only) |
| **Read first** | [`docs/VALIDATION-REPORT.md`](docs/VALIDATION-REPORT.md), then [`docs/BENEFICIARY-API-REQUIREMENTS.md`](docs/BENEFICIARY-API-REQUIREMENTS.md) |
| **Spec** | Channels Specification v2.0 §6.8 and §8.4 (ID-09) |

## What it does

The DA app's Broadcast screen gets working **SMS** and **Telegram** boxes. **All farmers**
now belongs to the Communications Officer and the Admin only, as the FSD says.

| Sender | Recipients | Area | Channels |
|---|---|---|---|
| Communications Officer, Admin | All farmers | A whole region, zone or woreda the sender may read | SMS, Telegram |
| Anyone with DA Broadcast `create` | All agents | An area the sender may read (the DA backend's rule) | In-app, SMS, Telegram |

Who may send at all, and which area, is the DA backend's own rule since `656985b`: DA
Broadcast `create` in Role Permission Manager (Communications Officer, Development Agent,
OAN Administrator, System Manager), and read on the area's DocType under the sender's User
Permissions. The add-on calls that rule unchanged. Its only rule of its own is that farmer
broadcasts are for the Communications Officer and the Admin.

- **In-app only to agents** still goes to `POST /notifications/broadcast`, untouched.
- **Anything with SMS or Telegram** goes to the add-on's `POST /api/v1/da-services/broadcasts`.
  It records the broadcast in `DA Broadcast` (same history and `client_op_id` rule) and its
  delivery in `DA Broadcast Dispatch`, then sends in the background.
  **SMS and Telegram are sent independently** (since 24 November 2026; this replaces the
  Telegram-first rule of spec §6.8.3). Each ticked channel has its own background job.
  Neither waits for the other, and a farmer or agent on both channels may get both copies.
  - **SMS** starts at once:
    - **Farmers:** every farmer of the area, from the beneficiary API, page by page.
    - **Agents:** every agent of the area, minus the sender, with phones from the DA
      Registry, whether or not they are on Telegram.
    - Each SMS goes through `oan_messaging_connector.queue_sms`, with purpose by source
      (SMS-06): `Emergency` and `Weather` as `alert`, `Knowledge` and `Informational` as
      `broadcast`. `source_reference` is `da_broadcast:<DA Broadcast>:<row>`, where `row` is
      the recipient's row number in the broadcast (no personal data; the same after a
      restart or a retry).
    - Each page's SMS rows and the saved cursor commit together, so a restart never sends a
      page twice.
  - **Telegram:** `POST /broadcasts` to Bot Services, with the `client_op_id` as
    `Idempotency-Key`, then its progress until Bot Services says it has finished.
    - With SMS also ticked, a Telegram failure never delays or blocks the SMS. It is recorded
      on the dispatch (`telegram_error`, `telegram_retry_count`) and retried on Telegram's own
      backoff (`telegram_next_run_at`). A definite refusal (4xx), or a broadcast that expired
      before Bot Services took it, marks Telegram **failed** (`telegram_failed`).
    - Telegram alone keeps the dispatch's own handling: retried after a timeout or 5xx,
      Failed on a 4xx.
  - The broadcast is **Done** when every ticked channel is finished: the SMS pass and its
    retry list are over, and Telegram has finished or failed.
- **SMS not queued:** when `queue_sms` queues nothing, the recipient goes on the dispatch's
  retry list (Farmer ID or DA ID, language, row number; never the phone). After the pass the
  list is tried again with a fresh phone lookup, waiting longer each time
  (`retry_backoff_minutes`, doubling, at most an hour), up to `sms_retry_attempts` (3). Those
  still not queued are counted as **not sent**. The broadcast is Done only when the list is
  empty or spent.
- **Restarts and retries:** a one-minute scheduler job re-enters unfinished broadcasts.
  - A timeout or 5xx from an upstream is recorded and retried, with the same backoff
    (`next_run_at` on the dispatch; Telegram next to SMS uses `telegram_next_run_at`).
  - A 4xx refusal from the beneficiary API, or from Bot Services when Telegram is the only
    channel, marks the broadcast Failed. Next to SMS, a Telegram 4xx marks only Telegram
    failed.
  - `expires_at` (optional, at most 7 days) stops the SMS pass.
- **`GET /api/v1/da-services/broadcasts/<client_op_id>`** returns per-channel counts:
  - in-app recipients;
  - Telegram copies by OAN Messaging status: accepted, sending, **sent**, failed, expired,
    unknown. Telegram ends at **sent**: there is no "delivered" or "read" for Telegram. Also
    `finished` and `error` (the last Telegram error code, or null);
  - SMS read, queued, no phone, retrying, not sent, and the OAN status of each queued SMS.
    `skipped_on_telegram` stays in the answer for compatibility and is always 0 now.
- **SMS length:** up to **3 parts** (OAN Messaging contract 3.1.0, SMS-09). One part is 160
  characters, or 70 with any Amharic letter; beyond that 153 / 67 per part. The screen warns
  when the SMS turns multi-part and blocks Send beyond 3 parts. The API refuses it too.

## Install (DA team)

1. **Connector:** `oan_messaging_connector` `develop` at `96fc5d1` or later. That version
   accepts the `broadcast` purpose and lets a caller ask for up to 3 parts (`max_parts`);
   other modules keep one part.
2. **Backend:** copy `app/da_broadcast_channels` into `apps/`, then:
   - `pip install -e apps/da_broadcast_channels`
   - `bench --site <site> install-app da_broadcast_channels`
   - `bench --site <site> migrate`
3. **Settings** (`DA Broadcast Channel Settings`, or site_config
   `da_broadcast_channels.{beneficiary_api|bot_services}.{base_url, token_url, client_id, client_secret, ca_bundle}`):
   - both base URLs;
   - Keycloak client-credentials for each;
   - `page_size` (500).

   `sms_fallback_wait_minutes` is retired: SMS no longer waits for Telegram. It is removed
   from the Settings form; on a site that had it, the old value stays unused in the
   database and nothing reads it.

   **Upgrading a site that already runs the add-on:** `bench --site <site> migrate` adds
   `telegram_retry_count` and `telegram_next_run_at` to DA Broadcast Dispatch. A broadcast
   that was waiting for Telegram when you upgrade sends its SMS to everyone at the next
   scheduler minute, as a new one would.

   Until the real upstreams exist, run `python3 stubs/stub_server.py --fixture <file>` and
   point both base URLs at it.
4. **UI:** `git am ui/0001-*.patch` on `oan_da_app_ui` `develop`.
5. **OAN operator:**
   - give caller `da` the purposes `alert` and `broadcast`;
   - run OAN Messaging 3.1.0 or later.
   `broadcast` SMS need `OAN_MSG_BROADCAST_ENABLED` on (OAN Messaging with SMS-03
   broadcast ageing, migration 0004). While it is off they are refused.
6. **Folding it into `da_services` later:** the app is written in `da_services`' style (same
   router, `handle_api_errors`, services layer and tests). Moving it in is a file move:
   - `rules.py` and `dispatch.py` to `services/`;
   - `api.py` to `api/v1/`;
   - the two DocTypes and the clients alongside.

## Evidence (24 November 2026: SMS and Telegram independent)

| Check | Result |
|---|---|
| Add-on tests (`bench run-tests --module da_broadcast_channels.tests.test_broadcast_channels`) on a Frappe 16 bench with `oan_auth_service` `develop` `fb31c01`, `da_services` `656985b`, `oan_messaging_connector` `96fc5d1` | **31 passed**, 0 failed (the unchanged kit: 26 passed on the same bench) |
| New tests | One job per ticked channel; SMS queued at once with both ticked (no wait, no farmer dropped); the SMS job never calls Bot Services; Bot Services down (503): SMS goes, Telegram error recorded and retried on its own backoff, then Done; Telegram progress error backs off alone; Telegram expired before Bot Services took it is marked failed; Telegram only (no SMS; counts say sent, never delivered or read); SMS only (Bot Services never called); duplicate submit sends no second SMS or Telegram broadcast; every agent gets the SMS with Telegram ticked |
| End to end (`e2e/e2e_broadcast.py <site> --no-oan`) against `stubs/stub_server.py` on the same bench | **16 of 16 passed**: SMS for all 5 farmers with a phone queued before Bot Services was called; Telegram 1 sent, 1 copy failed; Bot Services down: SMS still queued at once, Telegram error `unavailable` recorded, then sent once it came back; replay sent nothing. With `--no-oan` the SMS stop at the connector outbox: delivery through OAN Messaging and the fake 8028 was **not** rerun (the trial stack's TLS certificate has expired) |
| UI patch on `oan_da_app_ui` `develop` `b99685e` | `git apply --check` clean. `npm run typecheck` clean, `eslint .` clean. `vitest run`: 427 passed, 11 failed, all 11 in `grievances/evidence.test.ts` (`localStorage` missing under Node 26 on this machine); the same 11 fail on `b99685e` without the patch. Broadcast and notification tests: 57 passed |
| Lint (ruff, house settings) | Clean |
| Not run | The full DA suite; the screen in a browser; delivery through OAN Messaging (see above) |

## Evidence (23 November 2026, rebased on `656985b`)

| Check | Result |
|---|---|
| Add-on tests (`bench run-tests --module da_broadcast_channels.tests.test_broadcast_channels`) on a Frappe 16 bench with `oan_auth_service` `develop`, `da_services` `656985b`, `oan_messaging_connector` `develop` `96fc5d1` | **26 passed**, 0 failed. Upstreams are in-process fakes of the beneficiary API, Bot Services and the DA Registry |
| New since 20 November | Retry list (kept, retried with a fresh lookup, gives up, no phone stored), scheduler backoff, row number in `source_reference`, the Telegram fixes (the wait is checked first; the clock starts with the dispatch; a refusal never stops the SMS, for farmers and agents), and a Region-scoped Communications Officer under the DA backend's rules |
| UI patch on `oan_da_app_ui` `develop` `b99685e` | `git apply --check` clean. `npm run typecheck` clean, `eslint .` clean, `vitest run` **438 passed** (52 files) |
| Lint (ruff, house settings) | Clean |
| Not rerun | End to end (`e2e/e2e_broadcast.py`, which needs OAN Messaging over HTTPS and the fake 8028) and the full DA suite; the screen in a browser |

Earlier evidence (20 November 2026, on `2ae3d13`): full DA suite with the add-on 856
integration + 139 unit tests, 0 failures; connector 44 passed; end to end 10 of 10.

End to end (20 November 2026, before SMS and Telegram became independent), a
Communications Officer sent a 2-part Weather warning to all farmers of a woreda on SMS and
Telegram. The woreda had 6 farmers: 2 on Telegram (1 of them failing there) and 1 without
a phone.
- Telegram went first: 1 sent, 1 failed.
- The SMS copy read 6 farmers, skipped the 1 reached on Telegram, counted 1 without a phone
  and queued 4 SMS. (Since 24 November it skips no one: 5 SMS.)
- The fake 8028 delivered all 4 (purpose `alert`).
- An Informational SMS went to OAN as `broadcast` and was delivered (broadcasts enabled, SMS-03 ageing).
- A replay of the `client_op_id` returned the original and sent nothing.

## QA cases

| ID | Case | Expected |
|---|---|---|
| BC-01 | DA or Supervisor opens Broadcast | "All farmers" is not offered |
| BC-02 | Communications Officer picks a region only, All farmers, SMS | Sent to the whole region; `scope: region` |
| BC-03 | Message of 161 English characters with SMS ticked | Warning: "2 parts"; Send allowed |
| BC-04 | Amharic message of 202 characters with SMS ticked | Send disabled: "Too long for SMS: 4 parts" |
| BC-05 | All farmers on SMS and Telegram | SMS starts at once to every farmer with a phone; Telegram goes on its own; a farmer on both gets both |
| BC-06 | SMS and Telegram, Bot Services down or answering 503 | SMS goes at once, unchanged; Telegram error shown on the dispatch and retried; broadcast Done once Telegram is sent |
| BC-07 | Same `client_op_id` sent twice | "Already sent", one dispatch, no extra SMS |
| BC-08 | Same `client_op_id`, different text | 409 conflict |
| BC-09 | Agents of a kebele (DA sender) on SMS | Every other agent of the kebele gets it; the sender does not |
| BC-10 | In-app only to agents | Exactly as before (`/notifications/broadcast`) |
| BC-11 | Bot Services refuses (4xx), Telegram only | Broadcast Failed, error code shown, nothing sent |
| BC-11a | Bot Services refuses (4xx), SMS and Telegram | Telegram marked failed; the SMS goes as usual; broadcast Done |
| BC-17 | Telegram counts on the status | Shows sent (and accepted, sending, failed, expired, unknown); never "delivered" or "read" |
| BC-12 | Upstream timeout | Retried by the one-minute job after the backoff; error code recorded |
| BC-15 | Send clicked twice, or again after an error | One broadcast: the form keeps one `client_op_id` until it is sent or cleared |
| BC-16 | An SMS that cannot be queued | Kept on the retry list (no phone stored), tried again with backoff, then counted as not sent |
| BC-13 | Farmer without a phone | Counted as no phone; nothing sent |
| BC-14 | Weather / Emergency vs Knowledge / Informational | OAN purpose `alert` vs `broadcast` |

## Decisions the DA team owns

- V-05: whether a Supervisor or Executive may broadcast. Since `656985b` only roles with DA
  Broadcast `create` may (Communications Officer, Development Agent, OAN Administrator,
  System Manager): a Supervisor or Executive needs one of those roles or a DocPerm.
- V-06: storing evidence files so Telegram can send them.
- V-07: wiring Knowledge Base "Send to farmers" to the same route.
- V-08: sending gathering SMS through the connector.
- When to fold the add-on into `da_services`.
