# 5 Telegram channel: platform, security and shared rules

## 5.1 Platform: RapidPro plus OAN Bot Services

RapidPro (UNICEF's open-source messaging platform: courier, mailroom and web) receives
Telegram updates, keeps contacts and runs the conversation flows. It lets non-developers
edit menus and texts. It cannot do the OAN-specific parts, so a small service, **OAN
Bot Services**, does them and RapidPro flows call it.

| RapidPro does | OAN Bot Services does |
|---|---|
| Receive updates through the webhook (Design 1) | Adapters to Master Data, Marketplace, the DA backend, the beneficiary API and Auth, behind one Keycloak service account |
| Contacts, language, the current step, flow timeouts | Development Agent routing (specialism round robin, kebele inbox claim, leave cover) |
| Flows: language, privacy notice, location menus, Main menu, prices, question intake, farmer linking, Stop | Question store, relay between farmer and Development Agent, reminders |
| Send replies within Telegram's limits; stop a contact that blocked the bot | Development Agent linking; farmer linking |

**Flow ↔ service contract:** plain JSON over HTTPS with a shared service credential.
Flows never call Master Data, Marketplace, the beneficiary API, Auth or OAN Messaging
directly, so the service is the single, auditable adapter layer. Each call has a
contract test.

**What changes for users because of RapidPro** (open decisions, §8.3):

| Topic | Effect | Decision |
|---|---|---|
| Data held | RapidPro keeps a contact per farmer (Telegram id and name), message history and downloaded media | RP-D1: accept with encryption, operator-only access, retention (for example 90 days) and a Stop flow that deletes the contact |
| Buttons | Menus are **reply-keyboard** buttons that send their label as a message. Inline buttons (`callback_query`) are not received. | RP-D2: accept, or patch courier |
| Media to Development Agents | Sent as attachments, not forwards, so the Development Agent no longer sees the farmer's Telegram name | RP-D4 |
| Operations | RapidPro is a larger platform (web, mailroom, courier, PostgreSQL, Redis, storage, search) | RP-D6: who runs it |

If these are not accepted, the custom router in Appendix A is the fallback; every
module rule in chapter 6 applies to both.

## 5.2 Verified RapidPro behaviour

Checked in courier `v26.3.82` and goflow source:

* Telegram is received only through an HTTP POST `receive` route: a webhook is required;
  long polling is not supported.
* Courier does **not** check Telegram's `X-Telegram-Bot-Api-Secret-Token` header; the
  gateway must (§5.3).
* Quick replies become reply keyboards; inline keyboards are URL links only.
* "Bot was blocked by the user" or "user is deactivated" stops the contact.
* Incoming photo, video, audio, voice, document, location and contact messages are
  handled; files are downloaded into RapidPro storage.
* Incoming messages are deduplicated by Telegram message id (changelog; not seen in
  code).
* A flow webhook node evaluates header values as templates; a ticket's assignee is a
  fixed user, so dynamic routing cannot be done in flows.

**Not verified** (deployed version unknown): whether the web app registers the webhook
with a secret token; `/start` deep-link handling; courier's 429 back-off and reply
priority; message and media retention settings; RapidPro API rate limits.

## 5.3 Design 1: securing the webhook

![Webhook security](diagrams/t2-webhook-security.png)

| Control | Setting |
|---|---|
| One route only | `POST /c/tg/<channel-uuid>/receive` on a dedicated host; everything else returns 404 |
| Secret header | Register with `setWebhook … secret_token=<64 random characters>`. APISIX compares `X-Telegram-Bot-Api-Secret-Token` in constant time and drops a mismatch. |
| Source addresses | Accept only Telegram's webhook ranges (`149.154.160.0/20`, `91.108.4.0/22`); defence in depth, because the ranges can change |
| TLS | Public certificate, TLS 1.2 or later, port 443 |
| Limits | Body at most 1 MB; rate limit per source; short timeout; webhook `max_connections` about 20; `allowed_updates: [message]` |
| Isolation | Courier ingress only from the gateway. Courier egress only to `api.telegram.org:443` and RapidPro internals. RapidPro web and API only for staff (VPN and SSO). Mailroom has no external route. |
| Secrets | Bot token per environment in courier's channel configuration (database encrypted, access restricted); webhook secret in a Kubernetes Secret; rotation runbook |
| Monitoring | Gateway counts of rejected calls (alert on spikes); scheduled `getWebhookInfo` (`last_error_message`, `pending_update_count`) |
| Registration | If RapidPro registers the webhook without the secret, DevOps re-registers it with the secret after the channel is added, and checks it after every channel change |

## 5.4 Conversation rules (every module)

* **State machine.** Each step is a flow step; buttons are reply-keyboard buttons. An
  unexpected reply shows the same menu again with "Please choose one of the options
  below."
* **Global commands:** `/start`, `/stop`, Main menu, Change language and Back work from
  every step.
* **Session:** a step expires after 30 minutes of silence; the farmer returns to Main
  menu.
* **What the bot keeps** (shown in the privacy notice after the first language choice):
  * in RapidPro: the farmer's contact (Telegram id and name), language, kebele, and the
    conversation, including downloaded photos and voice notes, for the retention period
    decided in RP-D1;
  * in OAN Bot Services: question records (with `contact_ref` only) for 90 days after
    closing, and, only after *Link my Farmer ID*, the Farmer ID and linked phone.

  The bot asks for the Farmer ID and phone number only if the farmer chooses *Link my
  Farmer ID*. **The notice text is final only after RP-D1:** it must state the real
  retention period and must not promise more than the platform does.
* **Stop:** Main menu has "Stop and delete my data"; `/stop` does the same. After
  confirmation:
  1. OAN Bot Services deletes the farmer link and drops queued messages;
  2. open questions close with the reason "farmer stopped" (their records keep only
     `contact_ref` until normal retention);
  3. the flow deletes the RapidPro contact through the RapidPro API, which removes its
     message history; downloaded media must be removed from RapidPro's storage with it.

  Each deletion must be verified on the deployed RapidPro version before the Stop flow is
  published (part of RP-D1). A later `/start` begins as a new contact.
* **`contact_ref`** (HMAC-SHA256 of `chat_id` with a secret key) is the only farmer
  identifier allowed in logs, metrics, audit events, the question store and calls to
  OAN services, except the Farmer ID and phone sent to the beneficiary API and Auth
  during linking.
* **Sending limits** (Telegram Bot FAQ): about 1 message a second to one chat and 30 a
  second overall; replies to a farmer's action before reminders; on HTTP 429 pause for
  `retry_after`; a blocked or deactivated user stops the contact.

## 5.5 Texts and languages

* Every farmer-facing string lives in one catalogue keyed by id (for example
  `price.card`, `ask.ack`), with English and Amharic values. With RapidPro the
  catalogue becomes flow translations; no strings in code.
* The source is the OAN Telegram Bot copy and translation sheet. New strings are
  translated with the existing Google Sheet workflow and reviewed by a native Amharic
  speaker before release.
* Buttons hold at most about 30 characters; keep labels short in both languages.
* Languages are configuration: each contact's language is an ISO 639 code (`am`, `en`,
  later `om` for Afaan Oromoo).

## 5.6 Location and Master Data

* Region › zone › woreda › kebele, from Master Data (Development:
  `master-data.development.oanstaging.com`), cached 24 hours. Up to 8 buttons a page
  with Next page and Back. The chosen kebele is saved and offered again ("Use Kebele
  X?").
* Access through the bot's Keycloak service account with a read-only role on
  administrative areas.
* On failure, serve the last cached copy and log a warning.
* The **kebele code** from Master Data is the key used everywhere: QR codes, the DA
  backend roster and question records. Routing on it, and what the DA backend must hold,
  is in §6.4.

| Call | Request | Response fields (placeholders to confirm with the Master Data team) |
|---|---|---|
| List child areas | parent area code (none for regions), level | `area_code`, `name_am`, `name_en`, `level`, `parent_code` |
| Get area | area code | `area_code`, names, `level`, parent chain up to region |

## 5.7 Service adapters

* One adapter per service in OAN Bot Services, behind the egress allowlist, using the
  bot's own Keycloak service account (client-credentials, one client per environment,
  roles limited to the routes the bot calls). Never a person's or a farmer's
  credentials.
* Timeout 5 seconds; one retry on connection failure; circuit breaker after 5 failures
  in a minute.
* On failure the farmer sees "This information is not available right now. Please try
  again later." and Main menu.
* Agree each adapter's JSON contract with the owning team before coding against a stub,
  and keep the stub as a contract test.
