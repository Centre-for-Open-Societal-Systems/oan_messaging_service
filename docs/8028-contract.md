# 8028 SMS Gateway — integration contract summary

Source: the supplied "8028 SMS Gateway API — Integration Guide for Protean" (12 pages).
This is a working summary for MSG-03 and MSG-05, not a copy of the guide. Where the guide
contradicts itself or this service's delivery rules, the **Decision** column says what this service does.
Items marked **OPEN** need an answer from 8028 / eCom before go-live.

Contract version: 3.0.0. The review of 1.0.0 approved the decisions on ambiguous 5xx,
persist-before-ack, single-part limits, undocumented `Failed` and certificate-pin
discrepancies; 2.0.0 and 3.0.0 apply the corrections from the 1.0.0 and 2.0.0 reviews.

## Transport and auth

| Item | Guide | Decision |
|---|---|---|
| Base URL | `https://8028.et:3400/sms-api/v1` | Configurable; default only in config examples |
| Send | `POST /sms/send-sms` | — |
| TLS | 1.2 or 1.3, client certificate required, verify server cert | Never disable verification; optional `ca.crt` path in config |
| Auth | client cert **and** `clientSecret` in body; either alone is rejected | Secret from env/vault only; never logged |
| Charset | `Content-Type: application/json; charset=utf-8` | — |
| Source IP | Only IPs declared at onboarding can connect | Ops item (deployment out of scope) |
| Time zone | EAT (UTC+3), `YYYY-MM-DD HH:mm:ss` | Adapter converts from aware UTC; receipts converted back |

## Send request

```json
{"clientSecret": "<secret>", "smstext": "…", "phonenums": ["09XXXXXXXX"], "pushdatetime": "2026-09-01 08:12:13"}
```

| Field | Guide | Decision |
|---|---|---|
| `phonenums` | 10 digits, `09…` or `07…`; up to 1,000 per request | **One recipient per request** (one message = one `sms_id`). Adapter converts E.164 `+2519…`/`+2517…` → `09…`/`07…` |
| `pushdatetime` | now/past = immediate; future = scheduled | Always "now" in EAT. Scheduling is out of scope |
| `smstext` | UTF-8; Amharic supported | Single part only (see length) |

## Message length

| Text type | 1 part | per part when multi-part | Guide "max 3 parts" |
|---|---|---|---|
| Latin only | 160 | 153 | 466 |
| Any Amharic/emoji/other Unicode | 70 | 67 | 204 |

**Inconsistency:** 3 × 153 = 459 and 3 × 67 = 201, not 466 / 204 (the guide's figures equal
160 + 153 + 153 and 70 + 67 + 67, which is not how concatenated SMS works). The guide also
says "Latin only" without defining whether that means GSM-7 (which includes `€ { } [ ]`
counted as two septets). **Decision:** pilot accepts exactly one part — 160 GSM-7 septets
with extension characters counted as 2, otherwise 70 UTF-16 units. **OPEN:** real
multi-part limit, whether 8028 bills/encodes GSM-7 extension characters as 2 septets.

## Send response → `ProviderResult`

Success (HTTP 200): `{"success": true, "message": "…", "data": [{"sms_id": "17006100", "phone_number": "0900000901"}]}`.
HTTP 200 means queued at 8028, **not delivered**.

| Observation | Outcome | `failure_code` | Retry? |
|---|---|---|---|
| 200, `success: true`, `data` has exactly one entry whose `phone_number` equals ours, non-empty string `sms_id` | `sent` | — | — |
| 200 but `success` missing/false, `data` empty/multiple/mismatched, or body not JSON | `unknown` | `malformed_response` | **no** — 8028 may have queued it |
| 400 with `success: false` | `rejected` | `invalid_recipient` if message names the number, else `text_rejected` | no |
| 401 (wrong secret) | `not_sent`, `retryable=false` | `provider_auth` | no; ops alert |
| 403 (credentials suspended) | `not_sent`, `retryable=false` | `provider_auth` | no; ops alert |
| 413 | `rejected` | `text_rejected` | no (single recipient, so cannot split) |
| 500 / 503 | `unknown` | `provider_server_error` | **no automatic resend** |
| Read timeout after the request was written | `unknown` | `timeout` | no |
| TCP connect refused / DNS failure (nothing sent) | `not_sent`, `retryable=true` | `provider_unavailable` | yes, with backoff |
| TLS handshake failure, cert expired/revoked, hostname mismatch | `not_sent`, `retryable=false` | `provider_config` | no; ops alert |

`provider_status` records `http_<code>` (or `timeout`, `tls_error`, `conn_refused`). Error
`message` text can contain the phone number (e.g. `Invalid phone number: 091261825`) and is
therefore **never** stored or logged verbatim.

**Conflict with the guide:** it says to retry 500/503 after 30 s, 2 min, 10 min. This service
forbids blind resends after ambiguous outcomes, because a 5xx may follow a successful queue
and the guide offers no idempotency key or lookup API. **Decision (approved):** 500/503 → `unknown`,
no automatic resend absent proof of non-submission. **OPEN:** does 8028 guarantee nothing was queued on 5xx? Is there a
client reference/dedup field or a status lookup by our id?

## Status callback → `DeliveryReceipt`

8028 → our HTTPS endpoint, one JSON object per status change:
`{"sms_id": "17006100", "phone_number": "0900000901", "sms_status": "Delivered", "event_time": "2026-09-01 08:12:19"}`.

| Item | Guide | Decision |
|---|---|---|
| Auth | mTLS; 8028 presents **the same** client cert it issued us; pin SHA-256 fingerprint | Pin at the TLS terminator that actually performs the handshake; never trust a forwarded cert header from an untrusted hop |
| Ack | 200 within 10 s; "reply 200 immediately, then process in background" | Authenticate, then **durably persist the receipt evidence, then 200** (approved). Matching/status changes may follow asynchronously |
| Retries by 8028 | after 1 min, 5 min, 15 min, 1 h, up to 6 h on non-2xx | Receipts must be idempotent (duplicates expected) |
| Matching | "find the SMS by sms_id" | Match on `sms_id` **and** the keyed HMAC fingerprint of the normalized `phone_number` (works after content purge); mismatch = security event, no status change; after `recipient_match_deadline` recorded as `unmatched_late`. The deadline is anchor + `receipt_window` (default 7 days); the anchor is `sent_at`, or the last persisted attempt time for `unknown` or rejected attempts. A late receipt never causes a resend |
| Stored evidence | not specified | `StoredReceipt`: `sms_id`, recipient fingerprint, masked phone, raw `sms_status`, times, disposition. **The raw phone number is never persisted.** Deleted after `receipt_evidence_retention` (default 90 days) |
| Ordering | not specified | Out-of-order handled by `ALLOWED_TRANSITIONS`; final states never move |
| Early callback | not specified | Callback may arrive before our send response is stored → keep as orphan, re-match on `finish_attempt` |

Status values (guide 5.3):

| `sms_status` | Final | Our status |
|---|---|---|
| `Sent` | no | `sent` |
| `Delivered` | yes | `delivered` |
| `Expired` | yes | `failed` / `provider_delivery_expired` — the gateway reports the delivery validity period ran out; the cause (handset off, coverage, operator) is not proven. Raw `Expired` kept as `provider_status` |
| `Rejected` | yes | `failed` / `provider_rejected` |
| anything else | no | record + log; no change |

**Inconsistency:** the guide's sample receiver treats `Failed` as final, but `Failed` is not in
the status table. **Decision:** `Failed` is handled as undocumented (non-final, recorded, no
change). **OPEN:** confirm whether `Failed` exists and what it means.

## Other inconsistencies and open items

1. **Callback fingerprint algorithm.** Section 5.5 pins SHA-256; the nginx sample uses
   `$ssl_client_fingerprint`, which is SHA-1. **Decision:** SHA-256 pinning, never weakened because of the sample; if nginx is
   used, compare the full certificate (`$ssl_client_escaped_cert`) or use a SHA-256 capable
   terminator. **OPEN:** confirm 8028 is fine with that.
2. **Garbled sentence** in 5.5: "CA file, extra key or Protean-issued certificate is
   needed" — almost certainly "**no** CA file…". Assumed so.
3. **Acceptance scenarios** say "ten scenarios" but list seven (S1–S7).
4. **S3 expectation** "no callback before 06:00 EAT" ties to the sample time, not a rule.
   Scheduling is out of scope.
5. **Certificate renewal**: the sample pins `client.crt` and `client.old.crt`. **Decision:**
   pin set is configurable (current + previous). **OPEN:** renewal notice period.
6. **Callback server certificate** must be publicly trusted while also requesting a client
   cert — needs a terminator configured for `optional_no_ca`-style requests. Deployment is
   out of scope; recorded for the runbook (MSG-08).
7. **Rate limit**: 20 requests/second per client. Enforced across workers in MSG-05.
   **OPEN:** is the limit per IP, per certificate or per account; behavior when exceeded
   (status code not documented).
8. **Partial success** for multi-recipient requests is not described. Avoided by sending one
   recipient per request; any `data` shape other than exactly one matching entry → `unknown`.
9. **`sms_id` uniqueness** scope and lifetime not stated. Stored as an opaque string ≤ 64.
10. **No client reference / idempotency on 8028.** The send API has no caller-supplied
    reference, so our `Idempotency-Key` protects only our own API. It cannot dedupe at the
    gateway, which is another reason ambiguous submissions stay `unknown`.
