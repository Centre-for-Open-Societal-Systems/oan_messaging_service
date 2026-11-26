# 7 Deployment, operations and testing

## 7.1 Environments

| Item | SMS (OAN Messaging) | Telegram |
|---|---|---|
| Namespaces | Own namespace and Helm release (`oan-messaging`) | `oan-bot-services` release; Telegram sending runs in OAN Messaging's `worker-telegram` |
| Databases | Own PostgreSQL database; owner role for migrations, runtime role for processes | OAN Bot Services' own PostgreSQL (no RapidPro, Redis, DynamoDB or Elasticsearch) |
| Secrets | Caller tokens are hashed in OAN; content and fingerprint keys, database URLs, 8028 client certificate and key, 8028 client secret, 8028's callback CA (and optional pinned callback certificates), callback secret: all in Kubernetes Secrets | Bot token per environment, in OAN Messaging's Secret only; webhook secret (gateway and Bot Services); `contact_ref`, fingerprint and data keys; Bot Services' OAN Messaging caller token; Keycloak client secret; hashes of the DA backend's `/broadcasts` tokens |
| Public exposure | 8028 receipt listener only, at the registered callback host (mutual TLS with 8028's callback CA, plus the callback secret) | One webhook path (§5.3) |
| URLs | Dev and staging URLs to be published by DevOps; all examples use `example.invalid` | Same |
| Certificates | HTTPS everywhere. Python 3.13 and later (including Frappe v16 on 3.14) verify strictly: private CA certificates need Subject and Authority Key Identifiers and CA key usage. Ask 8028 to confirm its certificates carry both identifiers. | Public certificate on the webhook host |

## 7.2 Network

* **OAN Messaging:** default deny. API ingress from caller namespaces only. Worker egress
  to 8028 (mutual TLS) and the database. Receipt ingress from 8028 only.
* **OAN Bot Services:** ingress from the gateway (`POST /telegram/webhook` only) and from
  the DA backend (`/broadcasts`, bearer token), each on its own APISIX route. Egress to its
  PostgreSQL, the OAN Messaging API (replies and broadcast copies), Master Data, the DA
  backend, the beneficiary API, the Auth service and Keycloak. No egress to Telegram.
* **OAN Messaging `worker-telegram`:** egress to `api.telegram.org:443` and the database
  only. The SMS worker keeps no Telegram egress and needs no Telegram settings.
* On the Istio mesh, allow `api.telegram.org` by hostname (ServiceEntry, registry-only
  outbound) rather than by IP range.

## 7.3 Operations

| Task | SMS | Telegram |
|---|---|---|
| Health | `/livez`, `/readyz`, worker heartbeat probe | OAN Bot Services `/healthz`; `getWebhookInfo`; `worker-telegram` heartbeat |
| Alerts | `status-check` CronJob (`status --fail-on-alert`) | Rejected webhook calls, webhook errors, adapter failures, 429 pauses (Rancher monitoring) |
| Pause | `suspend --channel sms`, then `resume` | `suspend --channel telegram` pauses all Telegram sending without touching SMS; turning the Telegram broadcast switch off stops new broadcasts only; stopping the webhook queues updates at Telegram for up to 24 hours |
| Credentials | `issue-credential`, `list-credentials`, `revoke-credentials` | Rotate the bot token (OAN Messaging only) and the webhook secret (gateway, Bot Services and `setWebhook` together; runbook) |
| Audit | Message metadata and receipts in OAN | Every broadcast accepted (sender, scope, recipient count) |
| Restore | PostgreSQL backups; the cleanup sweeper resumes on recovery | OAN Bot Services PostgreSQL backups |

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
* Bot data under its retention (§5.4): updates deleted once handled, stopped or
  unreachable subscriptions deleted after 90 days; sensitive fields sealed.
* The webhook secret is checked at the gateway and in OAN Bot Services; a wrong or missing
  header stores nothing.

## 7.5 Testing and Definition of Done

A module is done when its acceptance criteria pass in Development, in both languages,
and these checks are green.

| Test | SMS | Telegram |
|---|---|---|
| Unit and integration | OAN suite against local PostgreSQL with mocked providers | Every conversation path in both languages, including unexpected replies, Main menu, `/stop` and timeouts; repeated updates; re-check and cancel |
| Contract | Each caller against `openapi.yaml`; connector and client tests | Each OAN Bot Services adapter against its contract file; OAN Messaging 3.4.0 (`reply_markup`, cancel route) |
| Provider | Mocked 8028 (mutual TLS, receipts) | A fake Telegram on a local stack: reply buttons, 429 with `retry_after`, Telegram down, blocked user |
| Security | Secret scan, image scan, log check | Webhook: requests without the secret or from other addresses rejected; only the one route reachable; `getWebhookInfo` clean; no container reaches the internet in the local stack |
| Linking | Auth OTP sent to the registered phone through OAN | Same and alternate number; someone else's contact refused; unknown ID; wrong, expired and repeated codes; lockout; unlink; registered phone changed; alternate never reaches the registry, SMS senders or logs |
| Load | Rate limit held across workers; batches of 100 identical texts; a broadcast is never starved under saturating transactional traffic (ageing) | 500 simulated farmers for 10 minutes: no lost messages, replies ahead of reminders. **Region-wide Emergency:** one Emergency broadcast per woreda of the largest region (300+), started within minutes, up to 1 million linked farmers in total, with normal Q&A running: replies stay under 5 seconds, every woreda makes progress, the pace never exceeds the setting or Telegram's 30 a second, the SMS copy is not delayed by Telegram; total time recorded |
| Language | Every template fits one SMS part with the longest real values | A native Amharic speaker reviews every string and button length |
| UAT | Controlled live acceptance with real 8028 and registered test handsets | ATI and selected Development Agents run the farmer journeys |

### Current state (24 November 2026)

| Area | Result |
|---|---|
| OAN Messaging and OAN Bot Services | 1,333 tests passed on `main` (OAN Messaging contract 3.4.0, Bot Services, stubs). |
| DA broadcast kit | 31 add-on tests passed on Frappe 16 with the DA backend at `656985b`; end to end against stubs 16 of 16. |
| Helm chart | Lint, render and regression checks passed; the Telegram broadcast switch leaves the SMS worker's manifest unchanged. |
| Telegram end to end | Not yet run on a local stack with a fake Telegram. |
| Live SMS | First live SMS through OAN Messaging on 16 November 2026: a DPI Consent test message reached a test handset from sender 8028. Delivery receipts and the remaining 8028 acceptance cases are still to run. |
| Live Telegram | Not yet: the throwaway test bots were rate-limited by Telegram from their first request. |

## 7.6 Telegram release gate (RV-01)

Reworded for this release (TG-36): there is no RapidPro to verify. Run before go-live.
Owners: delivery team, ATI DevOps and security. Telegram does not go live until every
check passes.

* The Amharic bot texts are in `text-keys.yaml` and reviewed by a native speaker
* The webhook is registered with the secret and `allowed_updates: [message]`; a call
  without the secret is refused at the gateway and in OAN Bot Services
* Bot replies are sent ahead of a running broadcast at the configured pace, and Telegram's
  `retry_after` is honoured
* Updates off and Stop: no further copy is handed over, and waiting copies are cancelled
* A blocked or deleted chat is marked unreachable and skipped by later broadcasts
* Stopped and unreachable subscriptions are deleted after 90 days
* The SMS copy of a broadcast is sent on time while Telegram is down
* The region-wide Emergency load test (§7.5)
