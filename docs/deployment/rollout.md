# Rollout and compatibility (OI-72)

How to move an environment from one OAN Messaging version to the next without a moment in
which two running versions disagree. The Helm chart runs migrations in a pre-install /
pre-upgrade hook ([Helm guide](helm.md)); this page says what may run together and in which
order features are switched on.

## Rule

Every migration is **expand-only**: it adds tables, nullable or defaulted columns, or
indexes, and never changes or removes what an older release reads. So the new schema is
always safe for the old release, and an upgrade is:

1. the migration hook runs (`alembic upgrade head`, owner role), then `deploy/sql/grants.sql`
   if the release adds tables;
2. the API, worker, receipts and cleanup Deployments roll to the new image;
3. **only after every old pod has gone**, switch on any feature flag the release adds.

A behaviour that the old release would contradict is always behind a flag that is off by
default, so step 3 is the only moment it starts.

## Versions

| Contract | Migration | Adds | Old pods during the roll | Flags to switch on after the roll |
|---|---|---|---|---|
| 3.0.0 | 0001–0003 | Schema, attempt index, `dispatch_controls` | Old images ignore `dispatch_controls` (and so suspensions): suspend by scaling workers to zero instead | none |
| 3.1.0 | 0004 | `ix_messages_claimable_by_age` (index only). Up to 3 SMS parts; broadcast ageing; 4 SMS/s pacing; 500 retried; receipts read `status` only | Old workers claim in strict priority and pace at 20/s until replaced: harmless for a few minutes. Old API refuses texts over one part until replaced: callers asking for more parts (DA broadcasts) may see `422` during the roll and retry | `OAN_MSG_BROADCAST_ENABLED=true` (Helm `config.broadcastEnabled`) on API, worker and the `caller` command, only after every worker runs 3.1.0 (an old worker refuses to start with it, and would starve broadcasts). Then grant caller `da` the `broadcast` purpose |
| 3.2.0 | 0005 | `stats_*` tables, `callers.can_read_stats`, `ix_messages_created_at`; `GET /api/v1/charts` | Old API pods answer `404` for `/api/v1/charts` until replaced. Statistics are rebuilt by the `stats` CronJob every 15 minutes (Helm `statsRefresh`, on by default) and by cleanup in `deployment` mode; cleanup in `cronjob` mode never rebuilds them | none. Create the statistics reader (`caller --stats-reader`, `issue-credential`) and give its token to Reports Central |

| 3.4.0 | 0008 | Telegram only, additive: optional `reply_markup` on `POST /api/v1/messages/telegram` (kept inside the encrypted content, purged with the text, part of the idempotency fingerprint); `POST /api/v1/messages/{id}/cancel` and failure code `cancelled`; `OAN_MSG_TELEGRAM_BROADCAST_ENABLED` (Helm `workerTelegram.broadcastEnabled`, env on the api and worker-telegram pods only). Migration `0008` replaces the status-trigger function (no table lock) and swaps the `failure_code` check: added `NOT VALID` (short lock, no scan), then validated outside that transaction (does not block reads or writes); no column, no rewrite. Telegram claim order: a waiting broadcast is never aged ahead of a bot reply; SMS order unchanged | Old API pods answer `422` (unknown field) to `reply_markup`, `404` to the cancel route and `403` to a Telegram broadcast while only the new switch is on, until replaced: callers retry. An old Telegram worker would send a message **without** its buttons: roll worker-telegram before Bot Services sends `reply_markup`. SMS pods see no change | `workerTelegram.broadcastEnabled: true` after every api and worker-telegram pod runs 3.4.0 (the SMS worker is not restarted: the value is not in the ConfigMap). Then register `bot-services` with `--channels telegram --purposes transactional,broadcast` |
| 3.3.0 | 0007 | `stats_campaigns.channel` (`text`, server default `sms`, joins the primary key; touches only `stats_campaigns`, never `messages`); chart rows labelled by channel; Telegram pace, API address and link previews from configuration; optional `workerTelegram` Deployment | Old pods run on the new schema: an old statistics refresh inserts campaign rows without `channel`, which become `sms` (the next new refresh rebuilds the recent days correctly); old API pods answer charts without the new `channel` and `by_channel` fields until replaced | none. `workerTelegram.enabled` only when the Telegram caller (`bot-services`) is registered; keep `worker.channels: [sms]` |

`grants.sql` must be re-run for 3.2.0 (four new tables); the runtime role cannot read them
otherwise and `GET /api/v1/charts` returns `503`.

## Order across components

| Step | Who | Why |
|---|---|---|
| 1 | Migration hook | New schema first; old pods keep working on it |
| 2 | `grants.sql` (3.2.0) | Runtime role reads the new tables |
| 3 | Worker | Claim order, pacing and retries change here |
| 4 | API | New limits and routes |
| 5 | Receipts, cleanup | Receipt parsing; statistics refresh |
| 6 | Flags | After `kubectl rollout status` reports every Deployment complete |
| 7 | Caller grants | `broadcast` for `da`; the statistics reader |

## Rollback

* **Image:** roll the Deployments back to the previous image. Every migration above is
  additive, so the old image runs on the new schema. Switch the flags of the newer release
  **off first** (an old worker refuses to start with `OAN_MSG_BROADCAST_ENABLED=true`).
* **Schema:** never downgrade migrations on a database that holds production data.
  `0008`, `0007`, `0005` and `0004` can be downgraded on an empty test database only
  (`0007`'s downgrade drops the Telegram campaign rows and the column; `0008`'s refuses
  while any message is `cancelled`, because the old check would reject later updates of
  those rows).
* **3.4.0 image rollback:** switch `workerTelegram.broadcastEnabled` off first. A 3.3.0
  worker ignores stored buttons (it sends the text alone) and a 3.3.0 API answers `404`
  to cancel; cancelled rows stay valid for it (`failed`, final).
* **Messages already accepted** under a newer release stay valid for the older one, except
  texts longer than one SMS part: a 3.0.0 worker's 8028 adapter refuses them as
  `text_too_long` (failed, never sent). Roll back only when no such message is waiting
  (`msgQueueNow`, or `status`).

## Checks after every roll

* `python -m oan_messaging.runtime status --fail-on-alert` exits 0.
* `GET /readyz` is 200 on every API pod.
* `GET /api/v1/charts/msgQueueNow` (reader token) shows nothing waiting longer than a few
  minutes; `msgProviderHealth` shows no suspended channel.
