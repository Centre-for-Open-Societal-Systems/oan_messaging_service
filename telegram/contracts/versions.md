# Pinned versions

RapidPro's three parts are released together within one line, so all three come from the
**26.3 line**. The spec checked courier `v26.3.82` (§5.2). Between `v26.3.82` and
`v26.3.84` the Telegram handler did not change; only the Infobip handler and the runtime
did (checked 23 Nov 2026 with a GitHub compare).

| Part | Pin | Source |
|---|---|---|
| RapidPro web | `v26.3.222` | github.com/nyaruka/rapidpro tag; Python `>=3.14.5,<3.15` (its `pyproject.toml`) |
| Courier | `v26.3.84` | github.com/nyaruka/courier release binaries (`linux_arm64` / `linux_amd64`), checked against the release's `checksums.txt` |
| Mailroom | `v26.3.156` | github.com/nyaruka/mailroom release binaries, checked the same way |
| PostgreSQL | the major version RapidPro `v26.3.222` CI uses | exact image tag and digest recorded by worker 1a |
| Valkey (Redis) | the major version RapidPro CI uses | exact tag and digest recorded by worker 1a |
| Elasticsearch or OpenSearch | the one mailroom `v26.3.156` CI uses | exact tag and digest recorded by worker 1a |
| Garage | latest stable 2.x | exact tag and digest recorded by worker 1a |
| OAN Messaging | this repository, the same commit | built from `Dockerfile` |
| Bot Services, stubs, fake Telegram | this repository, the same commit | built in the stack |

Rules:

- No `latest` tags. Every image is pinned by tag **and** digest in `telegram/stack/`.
- RapidPro does not publish ready images for this line, so the stack builds its own
  images from the tags above: binaries for courier and mailroom, source for the web app.
  The build checks release checksums and runs as a non-root user.
- Worker 1a fills the "recorded by worker 1a" rows in its PR, with the reason for each
  choice. That is the only edit to this folder a worker may make.
- Moving to a newer RapidPro line means re-running the §5.2 checks and the RV-01 release
  gate (§7.6).
