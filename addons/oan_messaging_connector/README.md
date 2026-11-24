# OAN Messaging Connector (Frappe app)

A reusable Frappe app that sends SMS from any Frappe service (A2C Credit, Grievance, DA,
Auth, ...) through the **OAN Messaging Service** (contract 3.1.0). It owns the parts every
caller would otherwise rebuild:

- the **OAN Message** outbox: one row per intended message, with a random UUID4
  `delivery_id` used as the Idempotency-Key and never changed;
- dispatch **after commit**, plus a one-minute safety sweep (`FOR UPDATE SKIP LOCKED`,
  one commit per row, so it is safe with several workers);
- status polling into a separate field. 202/200 shows as _Submitted_, never "Sent", and
  `unknown` is flagged and never resubmitted;
- the length check: **one SMS part** by default (160 GSM-7 characters, or 70 if any
  Amharic character). A caller that sends longer texts on purpose passes `max_parts` up
  to 3 (153 / 67 per part, so at most 459 / 201); OTPs with `send_sms_now` are one part;
- a daily **purge**: finished rows keep only delivery metadata after a retention period
  (default 7 days); the text is cleared and the recipient masked;
- **OAN Messaging Settings**: base URL, caller token (Password), CA bundle, retention.

Status: **trial build, not released**. Licensed under the MIT License, the same as other
OAN services (see `LICENSE`).

## Use it from a caller app

1. Add it to the caller app's `hooks.py`:
   `required_apps = [..., "oan_messaging_connector"]`.
2. At each business event, inside the transaction, call:

   ```python
   from oan_messaging_connector.api import queue_sms

   queue_sms(recipient=doc.phone_number, text=text, purpose="transactional",
             source_reference=f"<app>:{doc.name}:<event>",
             reference_doctype=doc.doctype, reference_name=doc.name, event="<event>")
   ```

   - `purpose` is `transactional`, `alert` or `broadcast` (OAN accepts `broadcast` once its
     `broadcast_enabled` setting is on). `expires_at`, when given, must be
     timezone-aware. `sms.sms_parts(text)` gives the part count OAN will charge for.
   - **OTPs are refused.** The outbox stores the text, and an OTP must never be stored.
     OTP senders send at once with `oan_messaging_connector.send_now.send_sms_now`
     instead, from a job that holds the code only in its arguments (see the Auth kit).
   - `queue_sms` never sends inline and never raises: if the connector is disabled, the
     message is invalid, or settings, the database or the queue fail, it returns `None`
     (or the row name, if only the enqueue failed; the sweep then sends it) and writes
     an Error Log entry.

3. Keep the texts and event mapping in the caller app. See A2C's `oan_a2c/oan_messaging/`
   for the pattern: `events.py` holds the templates and mapping, `notify.py` the call
   sites.

## Existing Frappe SMS senders: the `send_sms` hook (supported, not preferred)

**Use explicit `queue_sms` calls** (above) for every new SMS. For modules that already
send through Frappe's built-in sender (`frappe.core.doctype.sms_settings.sms_settings._send_sms`,
used today by Grievance and Auth), the connector also provides a Frappe `send_sms` hook,
so that sender goes to OAN before the module rewrites its send path.

It is **off unless a module opts in** with one line in its own `hooks.py`:

```python
send_sms = "oan_messaging_connector.sms_hook.send_sms"
```

Why it is only an interim step:

- **No delivery status.** Success means "accepted by OAN"; the caller never learns
  `delivered` or `failed`.
- **Possible duplicates.** Each call is a new message with its own key. The client
  retries an unclear outcome with that key; if it is still unclear the hook raises, and
  a resend by the caller is a second message.
- **No outbox.** If OAN is unavailable the send fails at once instead of waiting.
- **Site-wide.** It replaces Frappe's sender for every app on the site (Auth is on every
  module site), with one purpose and one expiry for all of them: **Hook Message Purpose**
  (`transactional` or `otp`, default `transactional`) and **Hook Message Expiry
  (minutes)** (default 30; at most 30 for `otp`), or `oan_messaging_hook_purpose` and
  `oan_messaging_hook_expiry_minutes` in `site_config.json`.

What it guarantees:

- It **raises `OanSmsNotSent` on every failure** (connector disabled or not configured,
  text longer than one SMS part, OAN refusal, no answer) and never falls back to
  Frappe's gateway. Grievance and Auth skip their own "is a gateway configured?" checks
  once a hook exists, so this keeps them failing closed.
- It stores nothing (safe for OTP text) and writes an **SMS Log** row with masked numbers
  and no text when the doctype exists.

Full guidance, per-module steps and QA cases: handoff guides 03 §7, 12 §6 and 14 §6 in
the `oan_messaging_services` repository.

## Configure a site

The OAN operator registers the caller and issues its token:

```
python -m oan_messaging.runtime caller --caller a2c --channels sms --purposes transactional
python -m oan_messaging.runtime issue-credential --caller a2c
```

Then fill in **OAN Messaging Settings** (Enabled, Base URL, Token), or provide these
through `site_config.json`, which overrides the doctype (useful for secrets injected by
Kubernetes):

```
bench --site <site> set-config oan_messaging_enabled 1
bench --site <site> set-config oan_messaging_base_url https://oan-messaging.<env>
bench --site <site> set-config oan_messaging_token <token from the OAN operator>
bench --site <site> set-config oan_messaging_ca_bundle /path/to/ca.pem   # only for a private CA
```

The scheduler and a worker for the `short` queue must be running:
`bench --site <site> enable-scheduler` and `bench worker --queue short,default`.

Retention: **Clear Message Text After (days)** in the settings (default 7), or
`oan_messaging_content_retention_days` in `site_config.json`.

## Operate

- Desk, **OAN Message** list: filter **Needs Attention** for messages rejected by OAN,
  reconciliation cases and `unknown` outcomes.
- `oan_messaging_connector.api.messages_for(doctype, name)` returns the delivery state of
  one business record, for support screens.
- Rows cannot be deleted; they are the delivery audit trail. After the retention period
  they keep only metadata (status, ids, masked recipient, times).

## Tests

```
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app oan_messaging_connector
```

The tests use an in-process fake OAN API and need no network. `test_sms_hook.py` also
runs the hook through Frappe's own `_send_sms`, Grievance's SMS delivery logic (with and
without the hook) and, when `oan_auth_service` is installed, Auth's gateway check.

## Code standard

The app follows the same checks as the other OAN Frappe apps:

- **pre-commit** (`.pre-commit-config.yaml`): file hygiene, ruff import sorting, ruff
  lint and ruff format (line length 110, tab indentation, settings in `pyproject.toml`),
  prettier on Markdown, and gitleaks.
- **CI** (`.github/workflows/linters.yml`) calls the shared Linters workflow of
  `oan_auth_service`: conventional commit titles (`feat:`, `fix:`, `refactor:`, ...),
  pre-commit, Semgrep (Frappe's rules, Python correctness and the shared OAN rules) and
  pip-audit.
- Database access uses the Frappe query builder; there is no hand-written SQL. Workers
  claim one due row at a time with `FOR UPDATE SKIP LOCKED` and commit after each row;
  those commits are marked and justified in the code.
- `oan_messaging_connector/vendor/` is copied unchanged from `oan_messaging_services`
  (`examples/`) except for relative imports, and is excluded from ruff here: it is
  linted and tested in that repository.

Run the checks locally:

```
pre-commit install            # once
pre-commit run --all-files
```
