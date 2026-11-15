# 20 Testing and acceptance

## 1. Test levels

| Level | What it proves | Where |
|---|---|---|
| **Unit (fake OAN)** | Your mapping, keys, length, expiry and outcome handling | Your repository's test suite, with an in-process fake OAN (httpx MockTransport). See the connector, A2C and Consent Manager tests. |
| **Local integration** | The real OAN service, the real protocol, your real code paths, end to end | The trial stack (below), on your laptop |
| **Dev environment** | Your deployed service wired to the dev OAN, with mocked or sandboxed providers | Dev cluster, once URLs exist |
| **Controlled live** | Real 8028 and Telegram, registered test handsets, an approved window | Staging, once approved |

## 2. Local integration: the trial stack

`trial/oan_integration_trials/stack/README.md` brings up, on one Docker network:
* OAN (API over HTTPS, worker, receipts, cleanup, least-privilege database roles);
* a **fake 8028 gateway**: mTLS, an 8028-shaped response, then a **delivery receipt**
  back to OAN.

You see what the "handset" got at `http://127.0.0.1:18028/inbox`. Nothing leaves the
machine.

In the package it is in `trial/`: the stack (`trial/oan_integration_trials/stack/`)
builds the OAN service from the sibling folder `trial/oan_messaging_services-msg08/`.
Keep that layout. For the Credit and Consent Manager stacks, prepare
`trial/oan_integration_trials/repos/` as its README says. `trial/README.md` has the
full recipe.

```
cd stack && ./init-env.sh && docker compose up -d --build
docker compose run --rm -T --entrypoint cat fake-8028 /certs/ca.crt > trial-ca.crt
python tools/trial.py smoke      # accepted -> gateway -> delivered
```

* **Your token:** register a caller and issue a token inside the API container, the same
  way `onboard()` in `tools/trial.py` does:
  `python -m oan_messaging.runtime caller …` then `issue-credential …` (the token is
  printed once).
* **From a container** on the external `oan-trial` network: `https://oan-api:8000`.
* **From your machine** (a bench outside Docker): `https://localhost:18000`; the trial
  certificate covers `localhost` and `127.0.0.1`.
* In both cases trust `trial-ca.crt` (for the connector: `oan_messaging_ca_bundle`).

## 3. Acceptance checklist per module

Every case is **NOT EXECUTED** until it has been run with evidence. The QA catalogue
(`code/06-qa-cases.csv`) holds the full COMMON, ES, AUTH, GR, DA and TG cases. Credit and
Consent Manager cases are new here.

### Credit (CR)

| ID | Case | Expected |
|---|---|---|
| CR-01 | Bank moves an application to the next stage | One `stage_changed` SMS; Desk Submitted → delivered |
| CR-02 | Bank moves it to "Approved" | `application_approved` |
| CR-03 | Bank rejects it | `application_rejected`, with no reason text in the SMS |
| CR-04 | Bank completes it (disbursed) | `loan_disbursed` |
| CR-05 | Same stage set again | No SMS |
| CR-06 | Visit scheduled, then cancelled | Two SMS; both expire at the visit |
| CR-07 | OAN unreachable during dispatch | Row stays Queued; later sent once with the same key |
| CR-08 | Connector disabled | No rows, no errors; business actions unaffected |

### DPI Consent Manager (CM)

| ID | Case | Expected |
|---|---|---|
| CM-01 | Request a consent OTP | SMS arrives; the code is not in the API response or the logs |
| CM-02 | Wrong code | 401; the attempt counted |
| CM-03 | Right code, then approve | Consent approved |
| CM-04 | Re-issue | New SMS, new key; the old code rejected |
| CM-05 | Subject without a phone | 422 `otp_destination_unknown`; no SMS |
| CM-06 | OAN unavailable | 503; nothing stored; succeeds on retry |
| CM-07 | OTP not sent before its 300 s expiry (worker stopped) | OAN ends it `expired`; never sent late |
| CM-08 | Data-release OTP (aggregation with registries) | SMS with the "release your registry data" text |

### Common to every module (COMMON cases)

* Retries reuse the key.
* `unknown` is never resubmitted.
* The 70-character Amharic limit is enforced.
* No secret or full phone number in logs.
* The status UI never shows "Sent" on `202`.

## 4. What the trial already proved (15 November 2026)

| Module | Result |
|---|---|
| OAN | Smoke test: HTTPS API → worker → 8028 mTLS → receipt → `delivered` |
| Credit | CR-01, 02, 04 and 06 passed end to end (9 messages, each delivered once); CR-03 is covered by a unit test. Connector 11/11; A2C suite shows no regressions. |
| Consent Manager | CM-01 to CM-05 passed end to end; unit tests 5/5 |
| Not yet | CR-07, CR-08, CM-06 to CM-08 end to end. |
