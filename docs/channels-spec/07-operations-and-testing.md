# 7 Deployment, operations and testing

## 7.1 Environments

| Item | SMS (OAN Messaging) | Telegram |
|---|---|---|
| Namespaces | Own namespace and Helm release (`oan-messaging`) | RapidPro deployment; `oan-bot-services` release |
| Databases | Own PostgreSQL database; owner role for migrations, runtime role for processes | RapidPro's PostgreSQL and Redis; OAN Bot Services' own PostgreSQL |
| Secrets | Caller tokens are hashed in OAN; content and fingerprint keys, database URLs, 8028 client certificate and key, 8028 CA, pinned receipt certificate fingerprint: all in Kubernetes Secrets | Bot token per environment (courier channel configuration); webhook secret; `contact_ref` HMAC key; Keycloak client secret; flow ↔ service credential |
| Public exposure | 8028 receipt listener only (pinned mutual TLS) | One webhook path (Design 1) |
| URLs | Dev and staging URLs to be published by DevOps; all examples use `example.invalid` | Same |
| Certificates | HTTPS everywhere. Python 3.13 and later (including Frappe v16 on 3.14) verify strictly: private CA certificates need Subject and Authority Key Identifiers and CA key usage. Ask 8028 to confirm its certificates carry both identifiers. | Public certificate on the webhook host |

## 7.2 Network

* **OAN Messaging:** default deny. API ingress from caller namespaces only. Worker egress
  to 8028 (mutual TLS) and the database. Receipt ingress from 8028 only.
* **Courier:** ingress only from the gateway; egress to `api.telegram.org:443` and
  RapidPro internals.
* **OAN Bot Services:** egress to Master Data, Marketplace, the DA backend, the beneficiary
  API, the Auth service, Keycloak, Audit Manager, its PostgreSQL and the RapidPro API (and
  MinIO when Option B arrives). No egress to OAN Messaging.
* On the Istio mesh, allow `api.telegram.org` by hostname (ServiceEntry, registry-only
  outbound) rather than by IP range.

## 7.3 Operations

| Task | SMS | Telegram |
|---|---|---|
| Health | `/livez`, `/readyz`, worker heartbeat probe | Courier and mailroom health; `getWebhookInfo`; OAN Bot Services health |
| Alerts | `status-check` CronJob (`status --fail-on-alert`) | Rejected webhook calls, webhook errors, adapter failures, questions waiting for a reply, 429 pauses (Rancher monitoring) |
| Pause | `suspend --channel sms`, then `resume` | Stop the webhook (unregister or block at the gateway); updates queue at Telegram for up to 24 hours |
| Credentials | `issue-credential`, `list-credentials`, `revoke-credentials` | Rotate the bot token and webhook secret (runbook) |
| Audit | Message metadata and receipts in OAN | Audit Manager events for every question created, delivered, replied, escalated and closed |
| Restore | PostgreSQL backups; the cleanup sweeper resumes on recovery | RapidPro and OAN Bot Services backups |

**Rollout across versions (open):** the OAN Messaging chart runs migrations before an
upgrade, but there is no phased compatibility matrix yet for upgrades that change
behaviour across versions (release blocker OI-72).

## 7.4 Security checklist (both channels, before UAT)

* Secrets only in Kubernetes Secrets or a secret manager; none in values, images or git;
  separate credentials per environment.
* Service accounts per environment with only the routes each needs; never test or admin
  accounts.
* Containers non-root, read-only file system, requests and limits set.
* Logs carry no message text, phone numbers, Farmer IDs, OTP codes, names, tokens or media
  links. OAN Messaging redacts at the logging-handler level; the bot logs only
  `contact_ref`.
* RapidPro data under the retention decided in RP-D1, database encrypted, operator-only
  access.
* Rate limits per contact in the bot (for example 20 actions a minute).
* Media: attachments through RapidPro storage under RP-D1 retention; for Option B, MinIO
  with encryption, retention and signed links, scanned before a Development Agent opens
  them.

## 7.5 Testing and Definition of Done

A module is done when its acceptance criteria pass in Development, in both languages,
and these checks are green.

| Test | SMS | Telegram |
|---|---|---|
| Unit and integration | OAN suite against local PostgreSQL with mocked providers | Every flow path in both languages, including unexpected replies, Back, Main menu and timeouts |
| Contract | Each caller against `openapi.yaml`; connector and client tests | Each OAN Bot Services adapter against its agreed JSON contract; flow ↔ service contract tests |
| Provider | Mocked 8028 (mutual TLS, receipts) | A test bot: keyboards, media, 429 pause, blocked user |
| Security | Secret scan, image scan, log check | Webhook: requests without the secret or from other addresses rejected; other RapidPro paths unreachable; `getWebhookInfo` clean |
| Linking | Auth OTP sent to the registered phone through OAN | Same and alternate number; someone else's contact refused; unknown ID; wrong, expired and repeated codes; lockout; unlink; registered phone changed; alternate never reaches the registry, SMS senders or logs |
| Load | Rate limit held across workers | 500 simulated farmers for 10 minutes: no lost messages, replies ahead of reminders |
| Language | Every template fits one SMS part with the longest real values | A native Amharic speaker reviews every string and button length |
| UAT | Controlled live acceptance with real 8028 and registered test handsets | ATI and selected Development Agents run the farmer journeys |

### Current state (15 November 2026)

| Area | Result |
|---|---|
| OAN Messaging full suite | 896 passed, 1 expected failure (broadcast fairness, disabled in the pilot). Passes on Python 3.13 after the test-certificate fix; passed on 3.12 before it. |
| Helm chart | Lint, render and schema checks passed; kind install, recovery and upgrade passed. NetworkPolicy and LoadBalancer behaviour not tested. |
| Integration trial | Credit (9 messages, each delivered once) and Consent Manager OTP passed end to end against a fake 8028. |
| Release blockers | 16 listed: 12 fixed pending review, 3 partly fixed (redaction limits, a DA visit test, metrics decision), 1 open (rollout sequence). |
| Telegram | Nothing built yet. |
| Live | No real 8028 or Telegram traffic yet. |
