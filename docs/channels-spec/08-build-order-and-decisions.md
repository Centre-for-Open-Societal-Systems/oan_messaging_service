# 8 Build order and decisions

## 8.1 Build order

| Step | Channel | What is built | Can start when | Depends on |
|---|---|---|---|---|
| 1 | SMS | Accept OAN Messaging (msg07b and msg08) after independent review; deploy to Development | Review passed | Dev URL, private CA, 8028 test access |
| 2 | SMS | Module integrations: Credit and Consent Manager (patches built), then Grievance, DA, Auth, eSignet | Step 1 in Development | Caller tokens; beneficiary phone lookup; shortened templates |
| 3 | Telegram | RapidPro Telegram channel and Design 1 route; OAN Bot Services skeleton (Keycloak client, PostgreSQL, flow contract, adapter stubs); onboarding and Main menu flows; CI | Development bot token from ATI; RapidPro available; public route approved | RP-D1, RP-D2, RP-D6 |
| 4 | Telegram | MOD-02 Broadcast channels | Now (setup, no code) | DA Communications Officers per area |
| 5 | Telegram | Location menus | Step 3 | Master Data (running) |
| 6 | Telegram | MOD-01 Market prices | Step 5, against a stub | Marketplace price API |
| 7 | Telegram | MOD-03 handover and kebele routing: coverage check, topics, media intake, delivery, relay, routing modes, no-answer chain, working calendar, weekly summary | Step 5, against a stub | DA backend D1–D7 and R1–R4; DA app Link Telegram code for agents, officers and heads |
| 8 | Telegram | MOD-08 Farmer linking: beneficiary adapter, Auth linking calls, link store, registry-kebele choice, flows | Step 3 | Auth linking routes; beneficiary phone and kebele-code lookup; test farmer records |
| 9 | Telegram | MOD-04 Pest escalation | Step 7 | — |
| 10 | Both | Hardening and UAT: load, security review, audit events, Amharic review, controlled live acceptance | Steps 1–9 | Security review; ATI UAT |
| — | Telegram | MOD-06/07 Verified status (deferred) | Interface only in Phase 1 | New backend routes |

## 8.2 Decisions taken

| ID | Topic | Decision |
|---|---|---|
| OQ-01 | Where Development Agents answer | Telegram (Option A) in Phase 1; DA app inbox (Option B) later, behind the same delivery port |
| OQ-02 | Pest escalation tracking | "Sent to Crop Specialist"; one reminder if advice is still pending at the region's reminder time |
| OQ-03 | Reminders | One working day per wait, configurable per region (KR-16); Telegram only |
| OQ-04 | Broadcast posting | The DA Communications Officer for each region, zone and woreda approves and posts |
| OQ-07 | Retention in the bot | 90 days by default for contact details and closed questions; administrator can change both |
| OQ-09 | Price API | The Marketplace team publishes it; the adapter follows |
| OQ-11 | Telegram platform | RapidPro with OAN Bot Services, webhook secured by Design 1 (RP-D3); the custom router (Appendix A) is the fallback |
| OQ-12 | SMS and Telegram | Separate channels. SMS is outbound only, sent by each module to the registered phone through OAN Messaging. The bot sends no SMS; Development Agent alerts are Telegram only. |
| OQ-13 | Farmer linking | OTP from the Auth service to the registered phone; a different Telegram number (shared with the contact button) approved by that OTP is used for Telegram only (RP-D5) |
| SMS-01 | SMS sender | One sender (OAN Messaging) for all modules; no module calls 8028 directly |
| SMS-02 | Telegram through OAN Messaging | No OAN Telegram caller in Phase 1; the bot sends through RapidPro |
| SMS-03 | Broadcast purpose | Disabled until priority fairness is designed and tested |
| SMS-04 | Alerting | Status CronJob and pod signals; no metrics endpoint |
| KR-01 | Routing key | A Master Data kebele code: the chosen kebele, or for a linked farmer the registry kebele (KR-13) |
| KR-02 | Kebele not covered | The option shows; "coming soon" message; offers prices and channels |
| KR-03 | Roster owner | The DA backend owns it; the bot needs D1–D7 and R1–R4 (§6.4.5) |
| KR-04 | Kebele inbox | Private-chat inbox: offered to every eligible agent in their own chat; first "I will answer" claims (KR-12) |
| KR-06 | Same Development Agent | A farmer's new question goes to the agent who last answered them, if eligible |
| KR-07 | Silent Development Agent | Reminder after 1 working day; reassigned after another |
| KR-08 | Unclaimed inbox question | To the Woreda Officer after 2 working days |
| KR-09 | Agents passed over | To the Woreda Officer's own chat once 2 agents are passed over, or none is left (KR-14) |
| KR-10 | Silent officer | One reminder; the question stays with the officer |
| KR-11 | Weekly summary | Woreda Officer by kebele; zone and regional extension heads by woreda and zone |
| KR-12 | Inbox mechanism (16 Nov 2026) | Private chats with reply-keyboard buttons; no Telegram groups, no bot admin rights (replaces KR-05, group setup) |
| KR-13 | Linked farmers (16 Nov 2026) | Farmer linking stays in Phase 1. A linked farmer routes on the registry kebele code; if it differs from the chosen kebele the bot asks the farmer once; with no registry code, the chosen kebele |
| KR-14 | Long chains (16 Nov 2026) | After 2 agents are passed over, the question goes to the Woreda Officer |
| KR-15 | Claimed but silent (16 Nov 2026) | Reminder 1 working day after the claim; after another the claim is released to the Woreda Officer |
| KR-16 | Wait clock (16 Nov 2026) | Working time: one wait = 1 working day (Mon–Fri 08:30–17:30 EAT, public holidays excluded), configurable per region |
| KR-17 | Summary time (16 Nov 2026) | Friday 16:00 EAT, configurable |

## 8.3 Open decisions

| ID | Decision | Owner | Options |
|---|---|---|---|
| RP-D1 | RapidPro stores contacts, message history and media | ATI, security | Accept with encryption, operator-only access, a retention period (for example 90 days) and a Stop flow verified to delete the contact, its messages and its media (§5.4); or the custom router. The privacy notice text depends on this. |
| RP-D2 | Inline buttons | ATI | Accept reply keyboards; or patch courier |
| RP-D4 | Media to Development Agents as attachments | ATI | Accept (agents no longer see farmer names) |
| RP-D6 | Who runs RapidPro (version, upgrades, backups) | ATI DevOps | ATI DevOps or the RapidPro platform owner |
| OQ-08 | Verification for status checks | ATI | Linked Farmer ID (§6.7) proposed; MOD-06/07 deferred until decided |
| ID-01 | Registered phone lookup for OTPs (Consent Manager, Telegram linking) | Identity owner, DPI | Beneficiary API route, its access rules and what to do when there is no phone |
| ID-02 | Auth linking routes and which Auth site hosts them | Auth | New service-client routes; caller naming (`auth-<site>` proposed) |
| ID-03 | DA backend data and routes D1–D7, R1–R4 | DA backend | Build them; until then the bot uses a stub with the same fields |
| ID-04 | Registry kebele code | Identity owner, registry | The beneficiary API returns the Master Data kebele code (the registry holds names today, which are not unique) |
| ID-05 | Who sets leave | DA team | The agent in the DA app, or their supervisor |
| ID-06 | Link codes for zone and regional extension heads | DA team | Confirm they can open "Link Telegram" in the DA app |
| ID-07 | Working calendar | ATI | Working hours and public holidays per region |
| GR-01 | Grievance routing | ATI, Grievance | Parked; the same roster and officer chain are candidates |
| OPS-01 | Rollout sequence across versions | DevOps | Phased compatibility matrix (OI-72) |
| OPS-02 | Dev and staging URLs, private CA, 8028 test access | DevOps, 8028 | — |
