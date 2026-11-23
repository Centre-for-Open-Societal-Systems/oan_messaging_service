# Reports Central: OAN Messaging as an upstream

Reports Central (`oan-reports-central`, branch `developer`) reads every OAN service through
the same dashboard contract: `GET {UPSTREAM_<SERVICE>_BASE_URL}/api/v1/charts/{chartKey}`
(`backend/app/services/upstream.py`). OAN Messaging serves that contract (contract 3.3.0),
so adding it is configuration plus a catalogue entry on the Reports Central side.

## What Reports Central needs

| Item | Value |
|---|---|
| Upstream name | `messaging` (`UPSTREAM_MESSAGING_BASE_URL`, the OAN Messaging API base URL) |
| Auth | `Authorization: Bearer <token>` of a statistics reader (OAN runbook §3: `caller --stats-reader`, `issue-credential`). Held as a Secret; never logged |
| Envelope | `{"success": true, "data": [rows], "meta": {"as_of", ...}}`; `unwrap_chart_payload` already handles it |
| Batch | `GET /api/v1/charts?charts=a,b` returns `data: {chartId: {"chartName", "data"}}`, the "gateway style" that `unwrap_chart_payload` handles |
| Filters | `from`, `to`, `granularity`, `caller`, `purpose`, `channel`; region filters are ignored (OAN has no geography) |

## Telegram in the totals (contract 3.3.0, from 24 November 2026)

OAN Messaging now also sends the Telegram copy of DA broadcasts. **Totals include
Telegram.** Every chart counts every channel, and every row says which one:

| Chart | What changed | How to show SMS and Telegram apart |
|---|---|---|
| `msgKpis` | Still one row, the overall total, now with `channel: "all"` and `by_channel: [{channel: "sms", ...}, {channel: "telegram", ...}]` (same fields as the row) | Read `by_channel`, or call with `channel=sms` / `channel=telegram` |
| `msgTrend` | Still one row per period, with `channel: "all"` and `by_channel` per period | As above |
| `msgByCaller`, `msgByPurpose`, `msgFailureReasons`, `msgOtpLatency`, `msgCampaigns` | One row per key **and channel**; each row has `channel` | Group or pivot on `channel`; a report that wants one row per key sums the rows of that key |
| `msgQueueNow`, `msgProviderHealth` | Unchanged (already per channel) | |

`by_channel` always lists both `sms` and `telegram`, in that order, with zeros when a
channel had no traffic, so its shape never changes. With the `channel` filter, the overall
row (`channel: "all"`) counts only the filtered channels, and `by_channel` lists only them.

Nothing was removed or renamed. A report that wants SMS only, as before, passes
`channel=sms`. A broadcast sent on both channels appears in `msgCampaigns` twice: its
Telegram copy as `bc:<broadcast>` (`channel: telegram`) and its SMS copy as
`da_broadcast:<broadcast>` (`channel: sms`).

## Proposed reports (category "Messaging")

| Report | Charts |
|---|---|
| Messaging summary | `msgKpis`, `msgTrend` |
| Messages by module | `msgByCaller`, `msgFilterCallers` (filter list) |
| Messages by purpose | `msgByPurpose` |
| Why messages fail | `msgFailureReasons` |
| OTP speed | `msgOtpLatency` |
| Broadcast campaigns | `msgCampaigns` |
| Queue and gateway now | `msgQueueNow`, `msgProviderHealth` |

Reports Central is a COSS repository: these changes are for its team (no push from here).

**Auth gap to close on the Reports Central side (checked 20 November 2026, `developer`
`1f9322c`):** `fetch_chart` sends no Authorization header today. The planned upstream auth
(`backend/tests/test_upstream_auth.py`) is a **Keycloak client-credentials** token for every
upstream. OAN Messaging does not accept Keycloak tokens: its statistics reader holds an OAN
token. Reports Central needs a per-upstream **static bearer token** option
(`UPSTREAM_MESSAGING_TOKEN`, from a Secret) used instead of the Keycloak token for this
upstream.
