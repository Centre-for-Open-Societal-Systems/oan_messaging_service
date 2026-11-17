# Integration kits: validation report (17 November 2026)

Five kits in `integrations/kits/`, one per module repository. Each was applied to the
module's current branch head, checked with the module's own tooling, and tested with the
module's full suite with and without the kit. Nothing was pushed to a module repository.

## Summary

| Kit | Base | Lint and Semgrep | Kit tests | Full suite: with kit / without | End to end |
|---|---|---|---|---|---|
| Credit `oan_a2c_service` | `develop` `f6bb735` | Clean | 12 passed | 293 run, 7 fail / 281 run, 7 fail (the same 7) | **Pass**: 5 messages delivered once each |
| Consent Manager `consent-management` | `develop` `32bce76` | Clean | 25 passed | 220 passed / 195 passed | **Pass** (same delivery module, `main` build) |
| Auth `oan_auth_service` | `develop` `34c7306` | Clean | 7 passed | 391 passed / 384 passed | **Pass**: one `otp` SMS, no code in logs |
| Grievance `oan_grievance_service` | `develop` `5bb42c4` | Clean | 28 notification tests passed (4 new) | 1,221 run, 15 fail / 1,217 run, 18 fail; every failure with the kit also fails without it | Not run (same connector path as Credit) |
| DA Services `oan_da_app_services` | `develop` `ce156bf` | Clean | 8 passed | 930 passed / 922 passed | Not run (same connector path as Credit) |

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
  delivery callbacks, test numbers only.

## Pre-existing failures (not from the kits)

* **Credit:** `test_farmer_b2c` and loan-product checks (7).
* **Grievance:** PDF export tests (the bench has no PDF library) and temporary-password
  tests that fail intermittently.

## Supporting changes

* **Connector** (`develop` `628c504`): `send_now.send_sms_now` for OTP senders (the Auth
  kit), with 6 tests; 40 tests in all; Semgrep 0 findings.
* **Services repository:** the trial helper scripts now pass `ruff check .`; three
  Semgrep warnings about logging a credential *id* (not the token) are marked with the
  reason. Ruff, strict mypy, Semgrep correctness and security, and pip-audit are clean;
  the suite passes (901 passed, 1 expected failure for disabled broadcasts).

## Not covered

* Grievance and DA end to end through OAN; the shared dev environment; the real 8028.
* eSignet: the repository was not available.
* The Consent Manager on `develop` end to end: no image of that branch was available; its
  delivery module is the one tested end to end on `main`, and its 6 integration tests run
  the real API and database.
