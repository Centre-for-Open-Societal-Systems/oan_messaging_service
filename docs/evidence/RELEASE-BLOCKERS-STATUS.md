# Release blockers: status against msg08 (15 November 2026)

The 16 release blockers in the project's open-items list were compiled before the msg07b
remediation and MSG-08. This table re-checks each one against the code in
`oan_messaging_services-msg08` (which contains msg07b) and the connector. **"Fixed"
means the code and a test exist there; nothing is accepted until the independent review
passes.** Full suite on Python 3.12: 896 passed, 1 xfailed.

| ID | Blocker | Status | Evidence | Still needed |
|---|---|---|---|---|
| OI-07 | Dispatch timing under database latency (F-03) | **Fixed (msg08)** | Reservation inside `begin_attempt`; start-up latency guard; `tests/worker/test_dispatch_latency.py` | Review. The latency test stalled once on a loaded host; run it on a quiet machine. |
| OI-08 | Credential/TLS fault fails the whole backlog (F-04) | **Fixed (msg07b)** | `dispatch_controls` suspend and cooldown (migration 0003); `suspend`/`resume`; `suspended:<channel>` alert | Review |
| OI-09 | Poison row stalls the queue (F-01) | **Fixed (msg07b)** | Undecryptable rows deferred with content kept and an error logged; `deferred dispatch` alert; `tests/integration/test_remediation.py` | Review |
| OI-10 | Worker exits on a transient storage error | **Fixed (msg07b)** | Worker recovery loop; tests | Review |
| OI-11 | Lost late-success evidence (F-05, F-10) | **Fixed (msg07b)** | Success fenced to the exact attempt; `tests/api/test_repository.py` | Review |
| OI-12 | OTP waits behind a claimed batch (F-06) | **Fixed (msg07b)** | `batch_size = 1` default (`worker/dispatcher.py`) | Review |
| OI-13 | Missing time index (F-02) | **Fixed (msg07b)** | Migration `0002_attempt_start_index` | Review |
| OI-14 | Redaction misses later loggers | **Fixed, with a known limit** | Handler-level redaction (msg07b); uvicorn access-log fix with regression test (msg08) | Port the uvicorn fix to msg07b. Direct stream writes and third-party exporters are not covered. |
| OI-15 | Worker leases a channel it cannot send | **Fixed (msg07b)** | `claim_due` filters by the worker's channels | Review |
| OI-19 | Telegram bot key without a worker token | **Fixed (msg08)** | Worker refuses to start when an enabled Telegram caller's bot has no token (`runtime/__main__.py`) | Review. No Telegram caller in Phase 1. |
| OI-30 | DA visit expiry beyond 7 days (F-07) | **Fixed in the example; test gap** | `examples/da_visits.py` caps the deadline below the horizon; a 20-day test exists for the Credit example only | Add a >7-day DA visit test |
| OI-31 | Endless status polling (F-08) | **Fixed** | `examples/outbox_dispatch.py` and the connector: `poll_complete`, `needs_attention`, SMS polling stops after `receipt_wait_days` | Review |
| OI-62 | Missing process entry points | **Fixed (msg08)** | One image runs `api`, `worker`, `receipts`, `cleanup`; smoke-tested; kind install | Review |
| OI-63 | No database grant procedure (F-09) | **Fixed (msg08)** | `deploy/sql/grants.sql`; refused before grants and recovered after, on kind | Review |
| OI-64 | No observability (F-11) | **Mostly fixed (msg08)** | Heartbeat probe, `/livez` `/readyz`, `status --fail-on-alert` CronJob with 7 alerts, runbook | No metrics endpoint, by decision. Confirm that is acceptable for go-live. |
| OI-72 | Coordinated stop for rollout | **Open** | The Helm guide covers the migration hook; there is no phased compatibility matrix or activation sequence | Write it before the first upgrade that changes behaviour across versions |

**Summary:** 12 fixed (pending review), 3 partly fixed (OI-14 limit, OI-30 test gap, OI-64
metrics decision), 1 open (OI-72).

## New since the open-items list

| Item | Severity | Notes |
|---|---|---|
| Test certificates failed strict checks on Python 3.13+ (missing Authority Key Identifier) | Fixed, 15 November 2026 | The test helpers now add key identifiers and CA key usage; the full suite (896) passes on 3.13. The image uses 3.11. Ask 8028 to confirm their certificates carry both key identifiers before any move to 3.13. |
| Telegram: no caller in Phase 1 | Information | The OAN Telegram bot runs on RapidPro and sends to Telegram itself (OAN Channels Specification v2.0). |
