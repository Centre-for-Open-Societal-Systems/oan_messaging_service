# Grievance kit: SMS rows through OAN Messaging

| | |
|---|---|
| **Repository** | `oan_grievance_service` |
| **Base** | `develop` at `5bb42c4` |
| **Patch** | `0001-feat-notifications-send-SMS-rows-through-the-OAN-Mes.patch` (5 files) |
| **Handoff guide** | 12 |

## What it changes

| File | Change |
|---|---|
| `services/notifications.py` | `_send_sms_row` hands the row to `queue_sms` when OAN is configured on the site; otherwise Frappe's own sender and its fail-closed checks, unchanged |
| Grievance Notification Log | New status **Queued with OAN** and a read-only link **OAN Message** |
| `services/ticket_form.py` | SMS handed to OAN counts as a way the complainant was reached |
| `hooks.py` | `required_apps` adds `oan_messaging_connector` |
| `test_grievance_notification_log.py` | 4 tests for the OAN path; the existing gateway tests now state that they test Frappe's sender |

Grievance keeps all of its own logic: Notification records per event and channel,
eligibility, recipients, language, rendering, deduplication and the log. Only the SMS send
step changes. With OAN, an SMS row reads **Queued with OAN** on hand-over (never "Sent"),
and its OAN Message shows whether it was sent and delivered. A message OAN cannot queue
(longer than one SMS part, an invalid number) fails the row with the reason in the Error
Log. `source_reference = grievance:<grievance>:<event>`, purpose `transactional`.

## Configure

Caller `grievance` (sms, `transactional`, plus `otp` for the Auth kit), then the
connector settings on each Grievance site. Before go-live, shorten the templates that do
not fit one SMS part (guide 12 §3 step 5): those rows fail visibly instead of sending.

## Evidence (17 November 2026)

| Check | Result |
|---|---|
| Grievance pre-commit hooks on the changed files | Pass |
| Semgrep: Frappe rules, Python correctness, shared OAN rules | 0 findings |
| Notification tests (with the 4 new ones) | 28 passed |
| Full Grievance suite with the kit / without | 1,221 run, 15 fail / 1,217 run, 18 fail. Every failure with the kit also fails without it: PDF export tests (no PDF library on the test bench) and temporary-password tests |

Not run end to end: the OAN path ends in the same connector `queue_sms` that the Credit
kit ran end to end.
