# Appendix A: v1.0 custom router (fallback)

**Not needed in this release** (TG-36, 24 November 2026). RapidPro was dropped, and OAN
Bot Services now receives the webhook and runs the button conversation itself (§5.1),
sending through OAN Messaging. This appendix is kept only as the earlier v1.0 design for
reference; it is not built.

| Area | Custom router (v1.0) |
|---|---|
| Stack | Python 3.12, async, python-telegram-bot v21 or later; Redis for state and queues; PostgreSQL for questions; one image and Helm chart `oan-telegram-bot` |
| Receiving | Long-poll `getUpdates`; exactly one receiver per bot token; no public endpoint. Accept `message`, `callback_query` and `contact` updates; deduplicate `update_id` for 24 hours. |
| Buttons | Inline buttons (`callback_query`) |
| State | Redis keys: `tg:c:{contact_ref}` (chat id, language, kebele, active; 90 days), `tg:s:{contact_ref}` (step; 30 minutes), `tg:u:{update_id}` (24 hours), `tg:r:{kebele}:{specialism}` (round-robin pointer), `tg:in` and `tg:q:*` (queues) |
| Outbound | Two lanes (replies first); about 1 a second per chat and 25 a second overall; pause on 429; a sent marker prevents double sends |
| Run modes | `combined` (1 pod) in Development; `split` (1 receiver, 2 or more handlers) before the load test. Receiver and combined Deployments use `Recreate` with 1 replica; handlers roll with `maxUnavailable: 0` and a PodDisruptionBudget. |
| Forwarding | Telegram forwards in both directions: the Development Agent sees the farmer's Telegram name |
| Reply matching | `reply_to_message_id` |
| Egress | `api.telegram.org:443` (by hostname on Istio), Master Data, Marketplace, DA backend, beneficiary API, Auth, Keycloak, Audit Manager, Redis, PostgreSQL |

The farmer linking (MOD-08), kebele routing (§6.4, including the private-chat inbox), the
separate SMS channel and Telegram-only Development Agent alerts are the same in both
options. With the custom router, inline buttons may replace the reply keyboards.
