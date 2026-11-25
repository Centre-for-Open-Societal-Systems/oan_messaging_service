# 8 Build order and decisions

## 8.1 Build order

| Step | Channel | What is built | Can start when | Depends on |
|---|---|---|---|---|
| 1 | SMS | Accept OAN Messaging (msg07b and msg08) after independent review; deploy to Development | Review passed | Dev URL, private CA, 8028 test access |
| 2 | SMS | Module integrations: Credit and Consent Manager (patches built), then Grievance, DA, Auth, eSignet | Step 1 in Development | Caller tokens; beneficiary phone lookup; shortened templates |
| 2a | SMS | Broadcast ageing (SMS-03 fix), batching of identical texts (up to 100), both receipt formats, the DA broadcast caller (`alert`, `broadcast`); then the welcome SMS campaign | Step 1 in Development | 8028 answers (ID-12); DA web app wiring (ID-09) |
| 3 | Both | Hardening and UAT: load, security review, audit events, Amharic review, controlled live acceptance | Steps 1–2 and the Telegram waves below | Security review; ATI UAT |
| — | Telegram | MOD-06/07 Verified status (deferred) | Interface only in Phase 1 | New backend routes |

**Telegram: broadcasts plus sign-up in this release** (TG-36, 24 November 2026). The bot answers
no questions; it replies only to sign the farmer up and manage their updates. Ask a Development
Agent, market prices, pest reports, media relay, routing, cover, escalation, longer
conversation flows and the weekly report are **deferred, not supported**; their designs
stay in chapter 6 for a later release.

| Step | What is built | State | Depends on |
|---|---|---|---|
| T1 | OAN Messaging 3.4.0: Telegram-only broadcast switch, reply buttons, cancel route, bot replies claimed before broadcasts | Merged (24 Nov 2026) | — |
| T2 | OAN Bot Services without RapidPro: webhook receiver, button conversation, farmer and staff linking, Updates on/off, Stop, broadcasts with re-check and cancel, 90-day retention | Merged (24 Nov 2026), tested against stubs | Real routes: ID-01, ID-02, ID-04, ID-03 (R1-R3) |
| T3 | DA broadcast kit: SMS and Telegram sent independently | Merged (24 Nov 2026) | DA team integration (ID-09) |
| T4 | Local stack with a fake Telegram (no RapidPro) and end-to-end tests | To do | — |
| T5 | Amharic bot texts in `text-keys.yaml`, reviewed by a native speaker | To do (release gate) | The translator |
| T6 | Dev deployment: gateway route, webhook registration with the secret, caller `bot-services` (`transactional,alert,broadcast`), Telegram release gate (§7.6) | To do | OPS-02; Development bot token from ATI |

## 8.2 Decisions taken

| ID | Topic | Decision |
|---|---|---|
| TG-36 | Telegram broadcasts plus sign-up (24 Nov 2026) | RapidPro is not used: its current line is BSL 1.1 (not open source), it needs DynamoDB, and the open-source lines run on Django without security support. This release does area broadcasts plus sign-up (Start, language, privacy notice, farmer and staff linking, Updates on/off, Stop) and answers no questions; the other modules are deferred (§5.1, §5.2). Supersedes OQ-11 and RP-D1, RP-D2, RP-D4, RP-D6 for this release |
| TG-37 | Bot replies (24 Nov 2026) | Replies, with buttons, go through OAN Messaging (purpose `transactional`, `reply_markup`): one rate budget, the bot token in one place (§5.8) |
| TG-38 | Inbound (24 Nov 2026) | Webhook behind the gateway; the secret header is checked at the gateway and in OAN Bot Services (§5.3) |
| TG-39 | Bot texts (24 Nov 2026) | `telegram/contracts/text-keys.yaml`, English and Amharic; the Amharic translation is a release gate (§5.5) |
| TG-40 | Stop and retention (24 Nov 2026) | Farmer Stop turns updates off; the link and data are deleted 90 days later unless updates are turned on again. No inactivity expiry while the chat is reachable; stopped, blocked or deleted chats are deleted after 90 days (§5.4). Supersedes the 12-month rule of TG-22 |
| TG-41 | Queue boundary (24 Nov 2026) | Each copy is re-checked before it is handed to OAN Messaging; copies waiting there are cancelled on Updates off or Stop; no recall after a worker took the copy (§6.8.4) |
| TG-42 | Region scope (24 Nov 2026) | Region allowed for the Communications Officer and the OAN Administrator, in the DA kit and `/broadcasts` (§6.8) |
| TG-43 | RapidPro code in OAN Bot Services (24 Nov 2026) | Kept but switched off (`OAN_BOT_RAPIDPRO_ENABLED=false`, default): not built, not required, no jobs; its columns nullable |
| TG-44 | Staff Stop (24 Nov 2026) | Work messages stay on; the bot says the supervisor or the DA app can unlink. No DA backend contract change (§6.5) |
| TG-45 | SMS and Telegram independent (24 Nov 2026) | The DA backend sends the SMS and Telegram copies independently; neither waits. Replaces "Telegram first" (§6.8.3). Supersedes TG-20 |
| TG-46 | Text and status (24 Nov 2026) | Campaign text is sent as written; no automatic translation. A Telegram copy ends at `sent`; there is no "delivered" or "read" |
| OQ-01 | Where Development Agents answer | Telegram (Option A) in Phase 1; DA app inbox (Option B) later, behind the same delivery port |
| OQ-02 | Pest escalation tracking | "Sent to Crop Specialist"; one reminder if advice is still pending at the region's reminder time |
| OQ-03 | Reminders | One working day per wait, configurable per region (KR-16); Telegram only |
| OQ-04 | Broadcast posting | Telegram channels: the DA Communications Officer for each region, zone and woreda posts. Broadcasts through the bot: as the DA web app sends them: farmers of a region, zone or woreda (Communications Officer and Administrator only, 20 Nov 2026); agents of the sender's area (a Supervisor's own woreda; a DA's own kebele); no approval step (§6.8) |
| OQ-07 | Retention in the bot | 90 days by default for contact details and closed questions; administrator can change both |
| OQ-09 | Price API | The Marketplace team publishes it; the adapter follows |
| OQ-11 | Telegram platform — superseded by TG-36 | RapidPro with OAN Bot Services, webhook secured by Design 1 (RP-D3, the webhook-security decision, §5.3); the custom router (Appendix A) is the fallback |
| OQ-12 | SMS and Telegram | Separate channels. SMS is outbound only, sent by each module to the registered phone through OAN Messaging. The bot sends no SMS; Development Agent alerts are Telegram only. |
| OQ-13 | Farmer linking | OTP from the Auth service to the registered phone; a different Telegram number (shared with the contact button) approved by that OTP is used for Telegram only (RP-D5, the alternate-number decision, §6.6) |
| SMS-01 | SMS sender | One sender (OAN Messaging) for all modules; no module calls 8028 directly |
| SMS-02 | Telegram through OAN Messaging (revised 23 and 24 Nov 2026) | OAN Bot Services is an OAN Messaging Telegram caller for the broadcast copies and, since 24 Nov 2026, the bot's replies (TG-37), so Telegram follows the same rules, statuses and statistics as SMS. Earlier: no OAN Telegram caller in Phase 1 |
| SMS-03 | Broadcast purpose (revised 19 Nov 2026; built 20 Nov 2026) | **Built and the starvation test passes** (20 Nov 2026); kept as decided after the campaign trade-off was raised: an aged broadcast goes ahead of newer non-OTP SMS, so a large campaign delays them until it drains. Enabled by `OAN_MSG_BROADCAST_ENABLED` once **broadcast ageing** is built and passes the starvation test: a `broadcast` waiting longer than a configurable time (proposed 180 s) is claimed ahead of newer non-OTP traffic; no reserved share (§3.4) |
| SMS-05 | Batching (19 Nov 2026; built 23 Nov 2026) | Identical broadcast texts may go to 8028 in one request of up to 100 numbers (`OAN_MSG_SMS_BATCH_MAX_RECIPIENTS`; default 1, which is off); each recipient stays its own message; OTPs and personal notices one per request (§3.4) |
| SMS-06 | Purposes for DA web app broadcasts (19 Nov 2026) | `Emergency` and `Weather` → `alert`; `Knowledge` and `Informational` → `broadcast` |
| SMS-07 | 8028 receipt format (19 Nov 2026; settled 20 Nov 2026) | 8028 confirmed `status` with `Pending`, `Delivered`, `Rejected`; the listener reads `status` only (§3.5) |
| SMS-08 | Volume declared to 8028 (19 Nov 2026) | About 50,000 SMS a day; welcome campaign about 1 million over 2 weeks; peak about 40 SMS a second for 2 hours. **20 Nov 2026:** the operator sends 2–5 SMS a second, so the peak is not available: a region-wide Emergency of 300,000 SMS takes about 21 hours at 4 a second. Everyday volume and the welcome campaign (about 70,000 a day) fit |
| SMS-10 | Pacing to 8028 (20 Nov 2026; built 23 Nov 2026) | 4 SMS parts a second (a 3-part SMS uses 3; a batch uses numbers × parts), one slot kept for OTP, configurable; so messages wait in OAN in priority order rather than in 8028's queue (§3.4) |
| SMS-11 | HTTP 500 from 8028 (20 Nov 2026) | 8028 confirmed a 500 queues nothing: retried with the normal backoff. Other 5xx stay `unknown`, never resent |
| SMS-04 | Alerting | Status CronJob and pod signals |
| SMS-12 | Statistics (20 Nov 2026) | User: a metrics or statistics API is needed, modelled on the other services' dashboard APIs. `GET /api/v1/charts` with the OAN dashboard contract; read-only statistics reader token (user's choice); charts: KPIs, trend, by module, by purpose, failure reasons, OTP speed, broadcast campaigns, live queue and channel health (all chosen). Reports Central needs a static-token option to read it  **23 Nov 2026:** charts count all channels, and every row shows its channel (user's choice); broadcast campaigns get a channel column, so the Telegram and SMS copies of one broadcast show as two labelled rows |
| SMS-09 | Multi-part SMS (20 Nov 2026) | User: "allow multi-part but alert on screen when the message appears to be turning multi-part". OAN accepts up to 3 parts (160/70 in one part, 153/67 per part beyond); an OTP stays one part. The DA app warns when the SMS turns multi-part and blocks Send beyond 3. Contract 3.1.0 |
| KR-01 | Routing key | A Master Data kebele code: the chosen kebele, or for a linked farmer the registry kebele (KR-13) |
| KR-02 | Kebele not covered | The option shows; "coming soon" message; offers prices and channels |
| KR-03 | Roster owner (revised 19 Nov 2026) | The DA backend owns it (D1–D7), taking agent details from the DA Registry; the bot reads R1–R4 from the DA backend only and never calls the registry (§6.4.5) |
| KR-04 | Kebele inbox | Private-chat inbox: offered to every eligible agent in their own chat; first "I will answer" claims (KR-12) |
| KR-06 | Same Development Agent | A farmer's new question goes to the agent who last answered them, if eligible |
| KR-07 | Silent Development Agent | Reminder after 1 working day; reassigned after another |
| KR-08 | Unclaimed inbox question | To the Woreda Officer after 2 working days |
| KR-09 | Agents passed over | To the woreda's Supervisors (each in their own chat, first claims, TG-25) once 2 agents are passed over, or none is left (KR-14) |
| KR-10 | Silent officer | One reminder; the question stays with the officer |
| KR-11 | Weekly summary (revised 19 Nov 2026) | An emailed executive report in Reports Central (category DA), not a Telegram message: Supervisor (woreda scope) by kebele; Executive with a zone scope by woreda; Executive with only a region scope by zone, then woreda (§6.4.4) |
| KR-12 | Inbox mechanism (16 Nov 2026) | Private chats with reply-keyboard buttons; no Telegram groups, no bot admin rights (replaces KR-05, group setup) |
| KR-13 | Linked farmers (16 Nov 2026) | Farmer linking stays in Phase 1. A linked farmer routes on the registry kebele code; if it differs from the chosen kebele the bot asks the farmer once; with no registry code, the chosen kebele |
| KR-14 | Long chains (16 Nov 2026) | After 2 agents are passed over, the question goes to the Woreda Officer |
| KR-15 | Claimed but silent (16 Nov 2026) | Reminder 1 working day after the claim; after another the claim is released to the Woreda Officer |
| KR-16 | Wait clock (16 Nov 2026) | Working time: one wait = 1 working day (Mon–Fri 08:30–17:30 EAT, public holidays excluded), configurable per region |
| KR-17 | Summary time (16 Nov 2026) | Friday 16:00 EAT, configurable; the figures are frozen at that time (TG-17) |
| KR-18 | No individual agent–farmer link (19 Nov 2026, closes ID-10) | A farmer belongs to a kebele, and every Development Agent of that kebele serves every farmer in it; `DA Farmer Link` is not used for routing |
| RP-D1 | RapidPro data held (19 Nov 2026) — superseded by TG-36 | Accepted: encryption at rest, operator-only access, retention configurable (default 90 days) for messages and media, Garage storage with a GET-only public media route, Stop deletes the contact, staff-side copies and media; verified on the deployed version before release (§5.1, §5.4, §5.9) |
| RP-D2 | Buttons (19 Nov 2026) — superseded by TG-36 | Reply keyboards everywhere; courier not patched; stale taps refused politely |
| RP-D4 | Farmer name for Development Agents (19 Nov 2026) — superseded by TG-36 | Linked farmer: registered name and Farmer ID, read at delivery and not stored; unlinked: Telegram first name marked "not verified" |
| RP-D6 | Who runs RapidPro (19 Nov 2026) — superseded by TG-36 | ATI DevOps, self-hosted; the delivery team supplies Helm values, pinned version, backup and restore runbook and upgrade checklist. **24 Nov 2026:** the 26.3 line also needs DynamoDB or a compatible store (§5.2); **ATI DevOps to confirm what they can run** |
| TG-01 | Staff conversations (19 Nov 2026) | One bot. Staff messages pass through one thin staff flow to Bot Services, which holds all routing logic (§5.8) |
| TG-02 | Texts (19 Nov 2026) | Bot Services returns text keys and parameters; every farmer and staff text is a RapidPro translation (§5.5) |
| TG-03 | Roster changes (19 Nov 2026) | No roster copy; R1 and R4 read when needed, plus a sweep every 8 hours of kebeles with open questions |
| TG-04 | Telegram broadcasts (19 Nov 2026; revised 23 Nov 2026) | The DA backend calls Bot Services `POST /broadcasts` (contract unchanged); Bot Services chooses the recipients and OAN Messaging sends each Telegram message (SMS-02 revised); the SMS copy stays with the DA backend through OAN Messaging (§6.8) |
| TG-05 | Broadcast audience (19 Nov 2026, revised the same day) | Farmers: **linked farmers only**, on their registry kebele, from the subscription table in Bot Services; Development Agents from R1 |
| TG-06 | Broadcast languages (19 Nov 2026, revised the same day) | One text, sent as written, as the DA web app sends it; a text per language is a later enhancement needing DA app changes |
| TG-07 | Updates opt-out (19 Nov 2026, revised the same day) | No question at onboarding; updates on once the farmer links; the privacy notice says so; "Updates on/off" in Main menu |
| TG-08 | Broadcast pace (19 Nov 2026; revised 23 Nov 2026) | Configurable, default 20 messages a second, never above 30; set as OAN Messaging's Telegram limit, leaving the rest of the bot's 30 a second for RapidPro's replies |
| TG-09 | Broadcast failures (19 Nov 2026; revised 23 Nov 2026) | Blocked or deleted: counted as failed, skipped later, never retried; temporary errors retried 3 times. Bot Services learns a block from OAN Messaging's failure reason and marks the subscription |
| TG-33 | Chat id for broadcasts (23 Nov 2026) | Kept in the linked farmer's subscription row in Bot Services (not read from RapidPro at send time); passed to OAN Messaging per message; never logged (§2.4, §6.8.1) |
| TG-34 | Telegram broadcast format (23 Nov 2026) | Text only in Phase 1: title on its own first line, then the message; no bold, no evidence files; OAN Messaging's Telegram contract unchanged |
| TG-35 | Woreda fairness (23 Nov 2026) | Bot Services submits recipients to OAN Messaging interleaved across woredas, so every woreda of a running broadcast makes progress |
| TG-18 | Broadcasts follow the DA web app (19 Nov 2026; senders revised 20 Nov 2026) | Bot Services delivers what the app sends: senders (farmers: Communications Officer and Administrator only), region/zone/woreda/kebele, recipients, source, title, message, evidence; no approval field. `Emergency` goes ahead of other broadcasts. The app's behaviour changes only by decision of the DA team and ATI |
| TG-19 | Bringing farmers in (19 Nov 2026) | A welcome SMS to every registered farmer with a phone, one SMS part in English and Amharic, the same bot link for all, sent from the DA web app woreda by woreda; linking (OTP) offered at onboarding with Later |
| TG-20 | SMS copy (19 Nov 2026) — superseded by TG-45 | Telegram first; the SMS copy skips farmers whose Telegram copy is `sent`; failed, expired, `unknown` or still pending after 15 minutes get the SMS |
| TG-21 | `/broadcasts` conventions (19 Nov 2026) | As OAN Messaging: `Idempotency-Key`, problem+json codes, `expires_at` up to 7 days set by the caller, the same status names, `unknown` never resent |
| TG-22 | Data and scale (19 Nov 2026) | Subscription row lives with the link, removed after 12 months without activity (configurable; superseded by TG-40: no inactivity expiry, 90 days after Stop); public media links work 7 days (configurable); release gate includes the region-wide Emergency load test (§7.5) |
| TG-23 | Webhook payload content (19 Nov 2026) — superseded by TG-36 | Allowed (question text, staff replies, Farmer ID in `notify`); RapidPro HTTP log retention short (3 days default) and a small flow-editor group (§5.4) |
| TG-24 | Closing questions (19 Nov 2026) | "Thank you, that answers it" closes; otherwise auto-close 2 working days after the last reply (configurable); follow-ups go to the current holder (§6.3) |
| TG-25 | Several Supervisors in a woreda (19 Nov 2026) | Escalations offered to every linked, available Supervisor; first claims (§6.4.3) |
| TG-26 | Nobody at woreda level (19 Nov 2026) | Question held as `waiting`, farmer told once, retried by the sweep, reported weekly; no escalation to Executives |
| TG-27 | Woreda Officer source (19 Nov 2026) | DA app roles (Supervisor with woreda scope), not the registry's `position` or `reports_to` |
| TG-28 | Roster source and Q&A timing (19 Nov 2026, revised the same day) | R1 is a DA backend route over its `Effective` kebele postings, and active grants (as for its broadcast recipients), replacing the earlier DA Registry search; questions to Development Agents stay in wave 2 |
| TG-29 | Reports Central not ready (19 Nov 2026) | The emailed report waits; snapshots accumulate; no interim email |
| TG-10 | Messages to a contact in a flow (19 Nov 2026) — superseded by TG-36 | Queued until the contact is idle; waits start at delivery |
| TG-11 | Phasing (19 Nov 2026) | One delivery phase in two waves; farmer linking in the first wave; the second wave is gated by real DA roster data (§8.1) |
| TG-12 | Development Agent availability (19 Nov 2026) | Self-service in the bot with dates; no automatic "presumed away". Leave management is out of scope (§6.4.7) |
| TG-13 | Officer cover (19 Nov 2026) | The officer names covering Development Agents; each escalation is offered to all, first tap claims |
| TG-14 | Topics (19 Nov 2026) | Six topics mapped to the DA registry specialisation codes, mapping in configuration (§6.4.2) |
| TG-15 | Working calendar (19 Nov 2026) | YAML in the Helm values, maintained by ATI DevOps from ATI's yearly holiday list (closes ID-07) |
| TG-16 | Test environments (19 Nov 2026) — superseded by TG-36 | Local docker-compose stack in CI plus a shared Dev RapidPro behind Design 1 |
| TG-17 | Weekly figures (19 Nov 2026) | Frozen snapshot at Friday 16:00 EAT, served to Reports Central by week |
| TG-30 | Verification for status checks (19 Nov 2026, closes OQ-08) | Only farmers who linked their Farmer ID (§6.6) can see a grievance or credit status; MOD-06/07 stay deferred (§6.7) |
| TG-31 | Faster Telegram broadcasts (19 Nov 2026, closes OQ-16) | Not taken up: Telegram paid sending is the customer's decision; broadcasts use the free rate (TG-08) |
| TG-32 | Grievance routing (19 Nov 2026, closes GR-01) | Stays with the Grievance service and its own officers and desks; the bot's roster and officer chain are not reused |

## 8.3 Open decisions

* **Pilot setting for the Telegram broadcast switch:** whether the pilot turns on
  `workerTelegram.broadcastEnabled` (Telegram broadcasts only; SMS broadcasts stay as
  set by `OAN_MSG_BROADCAST_ENABLED`).

Release gates that are not decisions: the Amharic bot texts (§5.5) and the Telegram
release checks (§7.6). Work other teams must deliver is in §8.4.

## 8.4 Dependencies on other teams

Work other teams deliver. Each row names the features that cannot work until it is done,
so a team can see what its item unblocks. The bot and the SMS service are built and
tested against stubs with the same fields meanwhile.

| ID | Owner | What they deliver | What cannot work without it |
|---|---|---|---|
| ID-01 | Identity owner (DPI) | Beneficiary API routes for service clients (contract and reasons in the DA broadcast kit's `docs/BENEFICIARY-API-REQUIREMENTS.md`): for a Farmer ID, the registered phone and the Master Data kebele code; and the farmers of a region, zone, woreda or kebele by Master Data code with their phones, cursor-paged; the access rule (`farmer:contact:read`, no per-farmer consent proposed); what happens when there is no phone. Checked 20 Nov 2026: neither exists (`iam-bene-portal-api` is login only; Beneficiary-360 and DCI search need per-farmer consent; the Staff API filters by names) | **Farmer ID linking in the Telegram bot** (§6.6), **the SMS copy of farmer broadcasts** (§6.8.3), and so: Telegram broadcasts to farmers (only linked farmers receive them). Also the Consent Manager OTP to the registered phone |
| ID-02 | Auth team | Service-client routes that send and check the linking OTP, and which Auth site hosts them (it sends with that site's caller, which must allow `otp`) | **Farmer ID linking in the Telegram bot**, and with it everything listed for ID-01 |
| ID-03 | DA team | DA backend routes and data (§6.4.5): roster per kebele (R1), staff linking (R2), dead chat report (R3), officers per area (R4), the Telegram link (D4) and the routing mode per kebele (D5). Checked 19 Nov 2026 (`develop`, `2ae3d13`): postings and roles exist; D4, D5 and R1–R4 do not | **Any Telegram message to staff:** Development Agents, Supervisors and Executives cannot link in the bot without R2 and D4. **Telegram broadcasts to Development Agents** (R1). **Ask a Development Agent:** the kebele coverage check, routing questions to agents and agents taking turns (R1, D5). **Escalation to the Woreda Supervisor**, officer cover and the weekly report recipients (R4) |
| ID-04 | Farmer registry and identity owner | The Master Data kebele code on each farmer record (the registry holds kebele names today, which are not unique), returned by the beneficiary API | **Telegram broadcasts to linked farmers** by region, zone, woreda or kebele |
| ID-08 | Reports Central team | The DA report category and upstream; scheduled email; one report per recipient's area, with recipients from `DA RBAC Assignment` (all out of scope in its current specification) | The **weekly emailed report** on unanswered questions (§6.4.4). The weekly figures are still collected and served for charts |
| ID-09 | DA team | **Kit delivered 20 Nov 2026** (`integrations/kits/oan_da_app_services/broadcast-channels/`: add-on Frappe app, one UI patch, stubs, validation report); the DA team validates and integrates it. Wire the Telegram and SMS boxes of the DA app Broadcast screen: when Telegram is ticked, call `/broadcasts` with `client_op_id` as `Idempotency-Key`; send the SMS copy through OAN Messaging independently, to every farmer and agent of the area, with SMS-06 purposes (TG-45; the kit does this since 24 Nov 2026); show counts from `GET /broadcasts/{id}`. The backend now accepts a Development Agent's own kebele (`48d6034`) | **Every farmer broadcast from the DA app** on Telegram or SMS, including the **welcome SMS** that invites farmers to the bot (TG-19) |
| ID-12 | You or the delivery team, with 8028 | **Answered 20 Nov 2026:** callback certificates (the callback sample's `ssl` folder), callback secret (a temporary value; ask for a strong one), callback source 213.55.73.209, a 500 queues nothing, no API rate limit with 2–5 SMS a second at the operator, receipt field `status`. **Still open:** which guide applies for `pushdatetime`; whether more short codes or lines can raise the 2–5 a second; up to 100 numbers per request; a strong callback secret; the callback CA's Key Usage. **We owe 8028:** the receipt URL of each environment (DevOps) | **Large SMS sends:** the welcome SMS campaign and the SMS copy of broadcasts at full speed. Delivery receipts may be read wrongly until the callback field is confirmed |
| OPS-01 | DevOps | Rollout order and compatibility across service versions (OI-72) | Safe release of the SMS service changes and the Telegram services to Staging and Production |
| OPS-02 | DevOps, 8028 | Dev and Staging addresses, the private CA, 8028 test access | End-to-end testing of SMS and Telegram on Dev and Staging |
