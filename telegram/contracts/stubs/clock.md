# Fake clock

The stubs server also serves the time that Bot Services and the stubs use, so a test can
move time forward. Examples are the 30-minute step timeout, OTP expiry, auto-close and
working-day waits.

| Call | What it does |
|---|---|
| `GET http://stubs:8200/clock` | `{"now": "<RFC 3339 with offset>", "mode": "running" \| "frozen"}` |
| `POST http://stubs:8200/clock/advance` `{"seconds": n}` | Moves time forward by `n` seconds (n ≥ 0), returns the new `now` |
| `POST http://stubs:8200/clock/set` `{"now": "..."}` | Sets the time; never earlier than the current fake time |
| `POST http://stubs:8200/_test/reset` | Clears every stub's state (codes, rate limits, fault switches) and resets the clock to 2026-12-01T08:30:00+03:00 |

Rules:

- **Bot Services** reads time through one function. With `OAN_BOT_CLOCK_URL` set, it calls
  that URL; otherwise it uses the system clock. The variable is set only in the local
  stack; production refuses to start with it.
- **RapidPro and OAN Messaging use real time.** Tests that need RapidPro's own timeout
  (30-minute step expiry) set the flow's timeout from a RapidPro global, which the stack
  sets to 1 minute.
- The fake clock never goes backwards.
- **Mode** (decided 23 Nov 2026, added after PR #20): by default the clock is `running`. It
  moves on with real time from the last reset, set or advance. With `OAN_STUBS_CLOCK=frozen`
  it stands still between calls (the stubs' own tests use this). `GET /clock` reports the
  mode; `advance` and `set` return `{"now"}` only. Clients may ignore `mode`.
