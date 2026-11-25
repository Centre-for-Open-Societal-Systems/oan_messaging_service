# Pinned versions

Broadcasts plus sign-up release (TG-36): RapidPro and its parts (courier, mailroom, Valkey,
Elasticsearch, DynamoDB) and Garage are **not used**, so they are not pinned. The earlier
RapidPro 26.3 pins are in this file's history.

| Part | Pin | Source |
|---|---|---|
| PostgreSQL | 16, exact image tag and digest recorded with the stack | OAN Messaging and OAN Bot Services each get their own database |
| OAN Messaging | this repository, the same commit | built from `Dockerfile` |
| OAN Bot Services, stubs, fake Telegram | this repository, the same commit | built in the stack |

Rules:

- No `latest` tags. Every image is pinned by tag **and** digest in `telegram/stack/`.
- Images run as a non-root user.
- Bringing RapidPro back in a later release means re-running the §5.2 checks and the
  release gate (§7.6).
