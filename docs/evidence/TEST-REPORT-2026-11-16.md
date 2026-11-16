# Test report: oan_messaging_services and oan_messaging_connector (16 November 2026)

Everything below was run against **fresh clones from GitHub**:

| Repository | Commit tested |
|---|---|
| `oan_messaging_services` | `main` at `77c8082` |
| `oan_messaging_connector` | `develop` at `a86c10d` |

All providers were mocks or the fake 8028 gateway; no live message was sent. Only
`0900000xxx` test numbers were used.

## Result

**Both repositories pass every functional test.** Three documentation and tooling
problems were found in the local-trial helpers (F1 to F3); none affects the service or
the connector.

| Area | Check | Result |
|---|---|---|
| Service | Full test suite, Python **3.11.15** | **896 passed, 1 xfailed** (132 s) |
| Service | Full test suite, Python **3.12.13** | **896 passed, 1 xfailed** (123 s) |
| Service | Full test suite, Python **3.13.13** | **896 passed, 1 xfailed** (124 s) |
| Service | Type check, `mypy -p oan_messaging` | Pass (27 files) |
| Service | Lint, `ruff check .` as the README says | **Fail: 17 issues, all in `trial/` helper scripts** (F1). Service code is clean. |
| Service | `uv lock --check` | Pass |
| Service | `deploy/docker/requirements.txt` against the lock | Pass: identical to a fresh `uv export` |
| Service | Helm `ci/run-regressions.sh` (lint, render, secret scan, kubeconform) | Pass: **2,824 render checks**, 0 failed |
| Service | `docker build .` | Pass: image 87 MB, runs as uid 10001 with a read-only root file system |
| Service | Runtime commands in the image (`worker`, `receipts`, `cleanup`, `status`) | Pass. The API starts with `uvicorn` (the image's default command) and refuses to start without its required settings, as designed. |
| Trial | `trial/README.md`: `init-env.sh`, `docker compose up -d --build`, CA export | Pass: all six services healthy |
| Trial | Smoke test (`tools/trial.py smoke`) | **Pass** with Python 3.12 and `httpx`: accepted, gateway, receipt, `delivered`, no operator alerts. **The documented command fails as written** (F2). |
| Connector | App tests on Frappe **16.56.0**, Python **3.14.10** (`bench run-tests --app oan_messaging_connector`) | **11 passed** |
| Connector + Credit | Credit end to end (`e2e_a2c.py` with patched `oan_a2c` at `25ceee0`), against the repository's OAN | **Pass: 5 new messages, each `delivered` after 1 poll, each sent exactly once** |
| Consent Manager | OTP end to end (`e2e_consent.py`, patched image), against the repository's OAN | **Pass**: OTP SMS at the gateway; approval with the code; wrong code `401`; re-issue sends a second SMS and the stale code gets `401`; unknown phone `422` with no SMS; no OTP code in the logs |
| Logs | OAN API, worker and receipts logs after the trial runs | No tracebacks, no logging errors, no full phone numbers |

The one xfail is the strict broadcast-starvation test; broadcasts are disabled in the pilot.

## Findings

| ID | Severity | Where | Problem | Suggested fix |
|---|---|---|---|---|
| F1 | Low | `trial/stack/**/*.py`; `README.md` "Develop and test" | `ruff check .` (the README's command) fails with 17 issues in the trial helper scripts: unused imports, and mixed tabs and spaces in the Frappe-side scripts. The service code passes. | Exclude `trial/` in the Ruff configuration (the bench scripts follow Frappe's tab style), or fix the scripts |
| F2 | Medium | `trial/README.md`, `trial/stack/README.md` | `python tools/trial.py smoke` fails as written on a standard Mac: there is no `python` command, and the system `python3` is 3.9, while the script needs Python 3.11 or later (`datetime.UTC`) and `httpx`. Neither requirement is stated. | Document "Python 3.11+ with `httpx`", for example `uv run --no-project --python 3.12 --with httpx python tools/trial.py smoke` (the command used here). The same applies to `e2e_consent.py`. |
| F3 | Low | `trial/stack/compose.yaml`, `consent/compose.yaml`, `a2c/compose.yaml` | The compose project names and the network name `oan-trial` are fixed, and the ports are fixed (18000, 18028, 18100, 18200), so a second trial cannot run next to an existing one. | Document it ("stop any earlier trial first"), or make the names and ports configurable |
| F4 | Info | `pyproject.toml` | `[project.scripts] oan-msg` is declared, but the image installs only the dependencies, so `oan-msg` is not on the image's PATH. No document uses it; every document uses `python -m oan_messaging.runtime`. | None needed; optionally remove the entry or install the project in the image |

## How the trial was run

The machine already had an earlier trial stack using the same names and ports (F3). To
test the repository copy cleanly:

* The earlier stack's containers were stopped (not removed). They were restarted
  afterwards.
* The repository stack ran under its own project name (`COMPOSE_PROJECT_NAME`).
* A local `compose.override.yaml` (not in the repository) renamed only the network.
* Everything else followed `trial/README.md` exactly.

Afterwards, the repository stacks and their volumes were removed. The Credit bench got
its original OAN settings and network back; its connector stays at `a86c10d`, which
differs from the previous `71ef221` only in licence metadata.

## Environment

macOS 27.0.1 (arm64); Docker 29.5.3, Compose 5.1.4; PostgreSQL 16.15 (local, for the
suite); uv 0.11.18; Helm v4.3.1, kubeconform v0.8.0; Frappe 16.56.0 with Python 3.14.10
in the Credit bench container.

## Not covered

* NetworkPolicy enforcement and LoadBalancer behaviour (no enforcing cluster); a
  kind install was done earlier, on 15 November, not repeated here.
* A real 8028 gateway, real Telegram, real handsets.
* Grievance, DA, Auth and eSignet integrations (designed, not built).
* The GitHub Actions workflow (not run on GitHub).
