# Telegram Phase A stubs and fake clock

One small server on port 8200 that stands in for the services OAN Bot Services calls in
the local stack: the DA backend, Auth, the beneficiary API, Master Data and Marketplace.
It also serves the fake clock. Standard library only. **Test data only.**

The contracts are frozen in `telegram/contracts/` (see its README). This server follows
them; where they are silent, the choices are listed at the end of this file.

## Run

```bash
OAN_STUBS_CLIENT_ID=bot-services OAN_STUBS_CLIENT_SECRET=<generated> python3 telegram/stubs/stubs_server.py
```

| Setting | Default | Meaning |
|---|---|---|
| `OAN_STUBS_CLIENT_ID`, `OAN_STUBS_CLIENT_SECRET` | none (required) | The one test client allowed to get a token. The stack's init script generates the secret; it is never committed. |
| `OAN_STUBS_HOST`, `OAN_STUBS_PORT` | `127.0.0.1`, `8200` | The Docker image uses `0.0.0.0`. |
| `OAN_STUBS_CLOCK` | `running` | `running` or `frozen` (see Fake clock). |
| `OAN_STUBS_SEED` | `seed.json` here | Another seed file, same shape. |

Docker: `docker build -t oan-telegram-stubs telegram/stubs` (non-root, health check on
`GET /healthz`, same pinned base image as the repository `Dockerfile`).

Tests:

```bash
uv run --extra dev pytest telegram/stubs/tests -q
```

## Routes

| Prefix | Contract | Routes |
|---|---|---|
| `/da` | `contracts/stubs/da-backend.openapi.yaml` | `POST /roster` (R1), `POST /staff-links` (R2), `POST /staff-links/{ref}/unreachable` (R3), `GET /officers` (R4) |
| `/auth` | `contracts/stubs/auth.openapi.yaml` | `POST /token`, `POST /link-otp`, `POST /link-otp/verify` |
| `/beneficiary` | `integrations/kits/oan_da_app_services/broadcast-channels/docs/beneficiary-api.openapi.yaml` | `GET /api/v1/farmers`, `GET /api/v1/farmers/{farmer_id}` |
| `/master-data` | `contracts/stubs/master-data.openapi.yaml` | `GET /areas`, `GET /areas/{area_code}` |
| `/marketplace` | `contracts/stubs/marketplace.openapi.yaml` | `GET /commodities`, `GET /markets`, `GET /prices/latest` |
| `/clock` | `contracts/stubs/clock.md` | `GET /clock`, `POST /clock/advance`, `POST /clock/set` |

So the beneficiary API's "get one farmer" is
`GET http://stubs:8200/beneficiary/api/v1/farmers/FR-TEST-0001`.

**Token.** `POST /auth/token`, form-encoded, `grant_type=client_credentials` with the
client id and secret above. Every route under the five service prefixes needs
`Authorization: Bearer <token>`; a missing, unknown or expired token gives **401**. Tokens
last 3600 seconds of real time. The clock and the `/_test` routes need no token.

**Errors** are RFC 9457 problem JSON: `{type, title, status, code, errors[]}`, where each
error is `{field, code}` and never echoes the value sent.

| Case | Status |
|---|---|
| Unknown body field, unknown query parameter, wrong type or pattern | **422** (`validation_failed`) |
| The same for the beneficiary API | **400**, as its contract says |
| Not valid JSON | 422 (`invalid_json`) |
| Unknown route | 404 |
| Service switched down | 503 (`service_unavailable`) |

## Test routes (no token)

| Route | What it does |
|---|---|
| `POST /<service>/_test/down` `{"on": true}` | That service answers every call with 503 until switched off with `{"on": false}`. An empty body means on. `<service>` is `da`, `auth`, `beneficiary`, `master-data` or `marketplace`. |
| `POST /<service>/_test/slow` `{"on": true}` | That service waits 6 seconds before each answer, then answers normally. |
| `POST /_test/reset` | Clears all state: OTP codes and rate limits, used link codes, staff links (back to the seed), fault switches, and the clock (back to 2026-12-01T08:30:00+03:00). Returns `{ok, now}`. |
| `GET /auth/_test/codes?link_request_id=` | The OTP the stub "sent": `{link_request_id, farmer_id, phone, code, expires_at, used}`. 404 if none was sent (unknown, or rate-limited). |
| `POST /da/_test/link-codes` `{"ref": "DA-TEST-001"}` | Issues a fresh staff link code for that person, valid 15 minutes from the fake now. Returns `{code, ref, expires_at}`. |
| `GET /healthz` | `{ok: true}` |

Auth's down switch stops the OTP routes only. `POST /auth/token` stands in for Keycloak,
a separate service, so it keeps working.

## Behaviour

**Auth OTP**
- `POST /auth/link-otp` sends (records) a 6-digit code, valid 600 seconds on the fake
  clock (`expires_in: 600`; the contract allows up to 1800).
- At most 3 sends per Farmer ID in 15 minutes (fake clock); the 4th gives `rate_limited`
  and records no code.
- Repeating a `link_request_id` returns the first answer, sends nothing and does not count
  against the limit.
- Verify: `valid` once; after that `invalid`. A wrong code is `invalid` and does not use up
  the code. Once the code has expired, the answer is `expired`. An unknown request is `invalid`.
- The stub does not check that the phone matches the registry; Bot Services must send the
  registered phone (tests read it back from `/auth/_test/codes`).

**DA backend**
- R1 lists, per requested kebele (in the order asked), the agents with an effective
  posting **and** an active grant, sorted by ref. An unknown kebele gives
  `{routing_mode: "specialism", agents: []}`. `telegram_id` appears only when linked.
- R2: a link code works once, and is `expired` 15 minutes after it was issued (seed codes are
  issued at the reset time). A wrong or used code is `invalid`. Linking sets the
  person's `telegram_id`; a new link replaces the old one, and a chat that was linked to
  someone else is taken from them (one chat, one person).
- R3 clears the person's link (204), or 404 for an unknown ref.
- R4: `level=woreda` gives Supervisors scoped to that woreda; `zone` and `region` give
  Executives scoped exactly there. Unknown code gives an empty list.

**Master Data**: children of `parent_code` at `level`, ordered by English name.
`parent_code` is required except for regions, and refused for regions. A parent with no
children at that level gives an empty list. `GET /areas/{code}` returns `parents` from
the region down to the direct parent.

**Marketplace**
- Commodities are returned in seed order (most requested first).
- Markets: by distance when `lat` and `lon` are both given. Otherwise, with `woreda_code`,
  the woreda's markets come first and then the other markets in the same zone. With no
  parameters, all markets are returned.
- Prices: `price_date` = fake today (Addis Ababa time) minus the market's
  `price_age_days`. `price_etb` = the commodity's base price × the market's factor.
- At most 5 `market_id`s. Unknown markets, unknown commodities and seed
  `missing_prices` give no row.

## Fake clock

As `contracts/stubs/clock.md`. It starts at 2026-12-01T08:30:00+03:00 and never goes
backwards (`/clock/set` to an earlier time gives 422). Times are RFC 3339 in +03:00.

`clock.md` does not say whether time stands still between calls. In **`running`** mode
(the default, used in the stack) the fake time moves on with real time from the last
reset, set or advance, so the bot's own short waits still pass. In **`frozen`** mode it
moves only when a test moves it. The stubs' own tests use frozen.

`GET /clock` also reports the mode, `{"now": "...", "mode": "running"}` or
`"frozen"`, so end-to-end tests can check it. `advance` and `set` return `{now}` only.

## Seed data (`seed.json`)

Everything is made up. Names start with `Test`, `Testfarmer` or `Sample`, phones are
`+251900000xxx`, and there are no hosts. The tests check this.

| Part | What is there | Cases for tests |
|---|---|---|
| `areas` | 2 regions (`ET91`, `ET92`, not real codes) × 2 zones × 2 woredas × 3 kebeles = 38 areas, English and Amharic names | Kebele names repeat (`Kebele 01` in every woreda), as real kebele names do: route on codes. |
| `kebele_routing_modes` | `ET91010102` is `kebele_inbox`; all others `specialism` | |
| `farmers` | 30, `FR-TEST-0001` … `FR-TEST-0030`, over 12 kebeles of zones `ET9101` and `ET9201` | No phone: 0005, 0017, 0026. Two phones: 0008, 0021. INACTIVE: 0029. Unknown kebele `ET92010199`: 0030. |
| `people` (agents) | `DA-TEST-001` … `010`, telegram ids `9000001nn` | Kebele `ET91010101`: 001 and 002 both `CROP_PRODUCTION`, 003 not linked. `ET91010103`: an agent, but none linked. `DA-TEST-008`: no active grant. `DA-TEST-010`: posting not effective (both left out of R1). Kebeles with no agents: most of the others. |
| `people` (officers) | Supervisors: 2 in woreda `ET910101` (one not linked), 1 in `ET910102`, none in `ET910201`. Executives: zone `ET9101`, region `ET92`. | |
| `link_codes` | `TESTDA03`, `TESTDA06` (agents), `TESTSUP2` (supervisor), `TESTEXE1` (executive) | Valid until 08:45 fake time after a reset. |
| `commodities` | 10, with Amharic names, unit and base price | |
| `markets` | 2 per woreda (16), with made-up coordinates | `MKT-ET910102-2` has a price 40 days old (the stale one). |
| `missing_prices` | coffee at `MKT-ET910101-2` | No row for it. |

The data in `seed.json` is the source. It is written one record per line so it is easy
to read and change.

## Choices where the contracts are silent

1. **Fake clock runs by default**, with a `frozen` mode, and `GET /clock` reports the
   mode (above). Agreed with the control session; it will add this to `clock.md`.
2. **Beneficiary paths** keep their contract paths under the prefix:
   `/beneficiary/api/v1/farmers...`.
3. **Beneficiary validation errors are 400**, because its contract lists 400 and not 422.
   All other stubs give 422.
4. **Unknown query parameters** also give 422 (400 for the beneficiary API). The token
   form accepts extra fields such as `scope`, because its contract does not close the
   form.
5. **Token**: 3600 seconds of real time, not fake time, so moving the clock forward never
   breaks a running Bot Services. Tokens survive `/_test/reset`. A wrong client gives 401
   `{"error": "invalid_client"}`; a wrong grant type or a body that is not a form gives 400.
6. **Auth down** does not stop `/auth/token`.
7. **Fault switches come before the token check**, so a down service gives 503 even with no
   token, like an unreachable service. Slow then answers normally after 6 seconds.
8. **OTP**: 6 digits; 600 seconds; verify checks used, then expiry, then the code.
9. **R4 levels**: woreda = Supervisors, zone or region = Executives scoped exactly at that
   level (an Executive of a zone is not returned for its region).
10. **Master Data** `parents` order is region first. `parent_code` sent for regions gives 422.
11. **Markets** "neighbours" means the other woredas of the same zone.
12. **Link codes** issued by the test route use the letters A–Z without I and O, and the
    digits 2–9.
