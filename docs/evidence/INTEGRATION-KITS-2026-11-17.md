# Integration kits: validation report (17 November 2026)

Five kits in `integrations/kits/`, one per module repository. Each was applied to the
module's current branch head, checked with the module's own tooling, and tested with the
module's full suite with and without the kit. Nothing was pushed to a module repository.

## Summary

| Kit | Base | Lint and Semgrep | Kit tests | Full suite: with kit / without | End to end |
|---|---|---|---|---|---|
| Credit `oan_a2c_service` | `develop` `f6bb735` | Clean | 15 passed | 296 run, 7 fail / 281 run, 7 fail (the same 7) | **Pass**: 5 messages delivered once each |
| Consent Manager `consent-management` | `develop` `32bce76` | Clean | 21 passed | 216 passed / 195 passed | **Pass** (same delivery module, `main` build) |
| Auth `oan_auth_service` | `develop` `34c7306` | Clean | 7 passed | 391 passed / 384 passed | **Pass**: one `otp` SMS, no code in logs |
| Grievance `oan_grievance_service` | `develop` `5bb42c4` | Clean | 29 notification tests passed (5 new) | 1,222 run / 1,217 run; every failure with the kit also fails without it | **Pass**: row Queued with OAN, SMS delivered |
| DA Services `oan_da_app_services` | `develop` `ce156bf` | Clean | 8 passed | 930 passed / 922 passed | **Pass**: planned and rescheduled SMS delivered |

Each base is the branch head on 17 November 2026, and each patch applies cleanly to it
(`git am` on a fresh clone).

## Checks used

* **Frappe modules (Credit, Auth, Grievance, DA):** the module's pre-commit hooks (ruff,
  ruff-format, prettier, gitleaks, file hygiene) on every changed file; Semgrep with
  Frappe's `semgrep-rules`, `r/python.lang.correctness` and the shared OAN rules
  (`oan_auth_service/.semgrep/rules.yml`), the set the modules' Linters workflow runs.
* **Consent Manager** (no lint configuration of its own): ruff with E, F, W, I, UP, B,
  RUF, S and BLE (line length 100, Python 3.10) and ruff format on the new files;
  Semgrep `r/python.lang.correctness` and `r/python.lang.security` on every changed file.
* **Tests:** Frappe 16.56.0 with Python 3.14.10 and MariaDB on a local bench (sites
  `a2c.trial`, `grievance.trial`, `da.trial`); the Consent Manager suite on Python 3.12
  with PostgreSQL 16.
* **End to end:** the OAN Messaging Service over HTTPS with the fake 8028 gateway and its
  delivery callbacks, test numbers only, through the bench's real worker; scripts in
  `trial/stack/a2c/e2e/`.

## Pre-existing failures (not from the kits)

* **Credit:** `test_farmer_b2c` and loan-product checks (7).
* **Grievance:** PDF export tests (the bench has no PDF library) and temporary-password
  tests that fail intermittently.

## Supporting changes

* **Connector** (`develop` `4ad8507`): `send_now.send_sms_now` for OTP senders (the Auth
  kit), with 6 tests, and a local check that the recipient is an Ethiopian mobile; 41 tests
  in all; Semgrep 0 findings.
* **Services repository:** the trial helper scripts now pass `ruff check .`; three
  Semgrep warnings about logging a credential *id* (not the token) are marked with the
  reason. Ruff, strict mypy, Semgrep correctness and security, and pip-audit are clean;
  the suite passes (901 passed, 1 expected failure for disabled broadcasts).

## Independent review

A separate review of the kit code (17 November 2026) raised 10 points. All were checked
against the code, fixed where real, and re-tested (the numbers above are after the fixes):

| Finding | Fix |
|---|---|
| Credit: a visit time loaded as a `timedelta` (or typed as "9:00") broke the visit hook, and with it the cancel | Parsed with Frappe's `getdate`/`get_time`; both visit hooks catch and log any SMS problem; 2 tests |
| Credit, Grievance, DA: CI would not install the connector they now require | Their `ci.yml` fetches it (repository variable `OAN_MESSAGING_CONNECTOR_REPO` until it is published under the organisation) |
| Consent: an uncertain send did not count against the resend cooldown or the send budgets | Counted, and a resend keeps the new code; 1 integration test |
| Grievance: an invalid number read "Queued with OAN" | The connector now refuses a number that is not an Ethiopian mobile (after removing spaces and dashes); the row fails; 2 tests |
| Credit: a stage move on a closed application re-sent its notice | Only a late "paid out" is sent; 1 test |
| Auth: an unexpected error in the job could reach Frappe's job-failure log | The job records every failure by type only; covered by the existing test |
| Consent: a misleading docstring, an unused data-release path, a changed `otp_channel` | Corrected, removed, and left unchanged |
| Tests that did not test what they claimed (ticket form, submitted event) | The ticket-form test now calls the form; the other renamed; guide 10 corrected |

Also found while re-testing: a test helper's change to a fixture user leaked into later
tests (fixed), and the end-to-end Grievance script now deactivates its routing assignment
when it finishes.

## Not covered

* The shared dev environment; the real 8028.
* eSignet: the repository was not available.
* The Consent Manager on `develop` end to end: no image of that branch was available; its
  delivery module is the one tested end to end on `main`, and its 7 integration tests run
  the real API and database.
