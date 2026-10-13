# WhatsApp — Meta Cloud API facts

Verified facts and open items for the `MetaCloudProvider`
(`whatsapp/providers/meta_cloud.py`), as `WhatsApp-Architecture.md` and
`FINAL-ARCHITECTURE-REVIEW.md` ("Remaining work") require before
implementation. Spec: `.claude/specs/08-whatsapp-foundation.md`, gates
M-1 to M-10.

Verified on **2026-10-07** against Meta's developer documentation, read
through a page-summarizing fetch tool and web search. Meta moved its
WhatsApp docs to `/documentation/business-messaging/whatsapp/`; the
older `/docs/whatsapp/cloud-api/` URLs redirect or are stale. A summary
is not the page itself, so every fact below says how firmly it was
established. Nothing is asserted that was not seen.

**Status key:** VERIFIED = the doc text was quoted back. PARTIAL = part
of the fact was confirmed. UNVERIFIED = not confirmed; the code that
depends on it is marked and must be checked against a development WABA
before production.

## Facts

| # | Fact | Status | Source |
|---|---|---|---|
| M-1 | Inbound webhook signature: header `X-Hub-Signature-256`; "HMAC-SHA256 hash, calculated using the post body payload and your app secret as the secret key"; it covers the whole JSON POST body. The `sha256=<hex>` prefix form is Meta's documented header value. | VERIFIED (prefix: PARTIAL) | /documentation/business-messaging/whatsapp/webhooks/getting-started |
| M-2 | Verification handshake: a `GET` with `hub.mode=subscribe`, `hub.challenge`, `hub.verify_token`; the endpoint must "respond with HTTP status `200` and the `hub.challenge` value". | VERIFIED | same |
| M-3 | **Resolved 2026-10-07 (user sign-off): one endpoint, `/webhooks/whatsapp`.** Incoming messages and message statuses are both delivered through the single `messages` webhook field. Meta sends it to the app's one callback URL, unless a different callback is set per WABA or per phone number (an "override"). | PARTIAL: the single `messages` field is confirmed by search results from the WhatsApp webhooks overview and override pages; the getting-started page itself does not say it | /documentation/business-messaging/whatsapp/webhooks/overview/ and /webhooks/override/ |
| M-4 | Inbound text payload: `entry[].changes[].value.metadata.phone_number_id`, `...value.messages[].from`, `...value.messages[].text.body`, `type: "text"`. A reply's `context.id` was not shown in the doc's example. (Not needed: the approved OD-1 (b) fan-out does not use reply context.) | VERIFIED for the fields; `context.id` UNVERIFIED | /documentation/business-messaging/whatsapp/webhooks/reference/messages |
| M-4b | Status payload: `...value.metadata.phone_number_id`, `...value.statuses[]` with `id` (the `wamid`), `status` (`sent`, `delivered`, `read`, `failed`, `played`), `timestamp` (Unix), `recipient_id`, and `errors[]` only when `failed`. Each of sent, delivered and read arrives as its own webhook. | VERIFIED | /documentation/business-messaging/whatsapp/webhooks/status-messages |
| M-5 | Create a template: `POST /{version}/{WABA_ID}/message_templates` with `name`, `category`, `language`, `parameter_format`, `components`. Response `{id, status, category}`. Names: "lowercase alphanumeric and underscores"; "names are not unique" (the same name in several languages). Named parameters (`{{first_name}}`, lowercase with underscores) are sent as `parameter_name` plus `text` in the body component's `parameters`. | VERIFIED | /documentation/business-messaging/whatsapp/templates/template-fundamentals/ and the Cloud API message reference |
| M-5b | Body length: 1024 characters when other components are included, 32768 when the body is the only component. The code uses the conservative 1024 (`WHATSAPP_TEMPLATE_BODY_MAX`). | PARTIAL (search result only) | Cloud API message reference |
| M-5c | Template status values: `APPROVED`, `ARCHIVED`, `DELETED`, `DISABLED`, `IN_APPEAL`, `LIMIT_EXCEEDED`, `PAUSED`, `PENDING`, `PENDING_DELETION`, `REJECTED` (plus `IN_REVIEW` on the status page). Status of one template: `GET /{version}/{TEMPLATE_ID}?fields=status`. ReviewFlow maps `APPROVED`→`APPROVED`; `REJECTED`, `DISABLED`, `DELETED`, `ARCHIVED`, `PENDING_DELETION`→`REJECTED`; everything else→`PENDING`. | VERIFIED | WhatsApp Business Message Templates API reference |
| M-5d | The shape of the named-parameter **example** in a create request (`example.body_text_named_params[{param_name, example}]`) was not confirmed. `MetaCloudProvider.submit_template` uses it. | **UNVERIFIED**, check on a development WABA before production | — |
| M-5e | Category: Meta classes "collect feedback on previous orders, transactions, or engagements" as **UTILITY**, but "specificity of the order or interaction ... is necessary. A general/generic survey or request for feedback will not be approved as utility." A utility template with marketing content is re-categorized to marketing. ReviewFlow's four standard variables name no order or transaction detail, so Meta may re-categorize or reject a template. This is a product risk, not a code conflict. | VERIFIED | /documentation/business-messaging/whatsapp/templates/template-categorization |
| M-5f | Template list: `GET /{version}/{WABA_ID}/message_templates` takes `fields`, `limit`, `after`, `before`; it returns `data[]` (`id`, `name`, `status`, `language`, ...) and cursor `paging`. It has **no `name` filter**: `name` exists only on the delete endpoint. No error code is documented for creating a duplicate name+language. `MetaCloudProvider.find_template_id` therefore pages through the list (`limit=100`, at most 50 pages) and matches the derived `rf_<uuid>` name and language; it runs only after a permanent refusal. The paging key names it reads (`paging.cursors.after`, `paging.next`) are the standard Graph API shape and were not quoted on the page. | VERIFIED (params, response fields); PARTIAL (paging keys) | WhatsApp Business Account Message Template API reference, verified 2026-10-07 |
| M-6 | Quality rating: `GET /{version}/{WABA_ID}/phone_numbers` returns each number's `quality_rating`; seen values `GREEN`, `YELLOW`, `RED`, `UNKNOWN`, `NA`. The full list is not enumerated on the page. | PARTIAL | /documentation/business-messaging/whatsapp/business-phone-numbers |
| M-7 | Latest Graph API version `v26.0` (released 2026-07-29), core APIs guaranteed for 2 years. Meta's WhatsApp doc examples pin older versions (`v23.0`). `META_GRAPH_API_VERSION` defaults to `v26.0`. | PARTIAL (search result) | /docs/graph-api/changelog/versions/ |
| M-8 | Official sample payloads: the inbound text and status JSON in M-4/M-4b are Meta's own examples, for the `/test-feature` step to anonymize into `whatsapp/tests/fixtures/`. No full official sample exists for the send, template-status or quality-rating responses; those fixtures would be written from the documented field names, and flagged as such. | PARTIAL | the pages above |
| M-9 | Platform token: a **system user access token**, with the permissions `business_management`, `whatsapp_business_management`, `whatsapp_business_messaging`. Expiry is chosen when the token is generated (an optional 60-day expiring token exists). The docs do not name a "platform-owned number" token type. `META_SHARED_POOL_ACCESS_TOKEN` is therefore a system-user token the operator generates and rotates. | PARTIAL | /documentation/business-messaging/whatsapp/access-tokens/ |
| M-10 | Meta's Business Messaging Policy: businesses must obtain opt-in, and "must respect all requests ... by a person to block, discontinue, or otherwise opt out of communications". It defines **no keyword list**; ReviewFlow's `STOP`/`UNSUBSCRIBE` set is its own (spec OD-4). | VERIFIED (policy text); no keyword rule found | business.whatsapp.com/policy |
| M-11 | **Webhook redelivery (verified 2026-10-12).** A non-`200` response to a `POST` is a failed delivery: "If any POST request sent to your server fails, delivery is retried immediately", then "with decreasing frequency" and "Unacknowledged responses will be dropped after 7 days." "These retries can result in duplicate webhook notifications", so the server must deduplicate. Batches can hold up to 1000 updates and are not guaranteed. **Not documented:** the retry intervals, the response timeout, and any limit on repeated failures. | VERIFIED (quoted); intervals, timeout and failure limits NOT FOUND | /documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint and /webhooks/overview |

## Resolved: one callback, not two (M-3)

`Webhook-Specification.md` used to list `POST /webhooks/whatsapp/status`
and `POST /webhooks/whatsapp/inbound` as separate endpoints. Meta
delivers both kinds of event through one `messages` field to one
app-level callback URL, and verifies that URL with a `GET` handshake at
the same address (M-2).

**Finding and source.** Meta sends webhooks to the callback URL set on
the app, and the `messages` webhook describes both messages sent from a
WhatsApp user to a business and the status of messages sent by a
business to a WhatsApp user, so incoming messages and statuses share the
`messages` field. Sources:
https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview/
and
https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/override/
(the override page: an alternate callback may be set per WABA or per
phone number, otherwise delivery falls back to the app's callback).
Found by web search on 2026-10-07; the fetch tool could not return the
pages' own text, so this stays PARTIAL until read on the page itself.

**Decision (user, 2026-10-07).** ReviewFlow exposes one endpoint,
`/webhooks/whatsapp` (`GET` handshake and `POST` delivery), and the
`/status` and `/inbound` paths are removed from the docs. The `POST`
receiver verifies the signature before any database access and tells
inbound messages (`value.messages[]`) from status notifications
(`value.statuses[]`) by payload shape. Phase 08 processes only opt-outs;
status notifications are acknowledged and discarded, never persisted
(`WhatsAppMessage` is Phase 11). Per-phone-number and per-WABA callback
overrides are not implemented.

## Embedded Signup / `OWN_NUMBER` facts (E-series)

Gates for `.claude/specs/08-whatsapp-embedded-signup.md`. Verified on
**2026-10-09**. Each page was read through a page-summarizing fetch tool,
using the `.md` form of the URL Meta lists in its index
(`/documentation/business-messaging/whatsapp/llms.txt`). The status key is
the one at the top of this file. A fact is VERIFIED only where the
page's own sentence came back quoted. A statement paraphrased by the tool
is PARTIAL. Anything the pages do not say is UNVERIFIED, and the
development-WABA check it needs is named.

Base URL for the sources below:
`https://developers.facebook.com/documentation/business-messaging/whatsapp/`

| # | Fact | Status | Source (relative to base) | Development-WABA check still required |
|---|---|---|---|---|
| E-1a | Embedded Signup "leverages the Facebook Login for Business product and our JavaScript SDK". Launched by `FB.login(callback, { config_id, response_type: 'code', override_default_response_type: true, extras: { setup: {} } })`. `config_id` is a Facebook Login for Business configuration id. | VERIFIED (example code on the implementation page) | `embedded-signup/overview`, `embedded-signup/implementation` | The exact v4 `extras` (the page's example has no version field) |
| E-1b | Success returns "the customer's WABA ID, business phone number ID, and an exchangeable token code". The code arrives in the `FB.login` callback (`response.authResponse.code`). The ids arrive as a `window` `message` event from an origin ending in `facebook.com`, with `type: 'WA_EMBEDDED_SIGNUP'`, `event` (`FINISH`, `FINISH_ONLY_WABA`, `FINISH_OBO_MIGRATION`; `CANCEL` with `data.current_step`; an error variant with `error_message`, `error_code`, `session_id`), and `data: { phone_number_id, waba_id, business_id, … }`. | VERIFIED (payload code block). The error variant's `event` value is inconsistent on the page (`CANCEL` vs `ERROR`) | `embedded-signup/implementation` | Capture a real v4 FINISH and CANCEL event |
| E-1c | No `state` or nonce parameter is shown or documented. | UNVERIFIED (absent from the page) | `embedded-signup/implementation` | — (ReviewFlow's signup `state` is server-side only: a hash in `EmbeddedSignupState`, spec ES-18) |
| E-1d | The page's domain must be in "Allowed domains" and "Valid OAuth redirect URIs", and the Client OAuth / Web OAuth / Enforce HTTPS / Embedded Browser OAuth / strict redirect / Login with the JavaScript SDK settings must be on. The page says to subscribe to the `account_update` webhook. | PARTIAL (tool paraphrase) | `embedded-signup/implementation` | App Dashboard configuration |
| E-1e | Versions: `v4` is "the latest Embedded Signup Version" (released 2025-10-08). "Embedded signup v2 will be deprecated on October 15, 2026"; v2/v3 are available until "October, 2026". Choosing products in the Login for Business configuration sets the flow to v4. | VERIFIED | `embedded-signup/versions`, `embedded-signup/version-4` | — |
| E-2a | "The exchangeable token code has a time-to-live of 30 seconds." | VERIFIED | `embedded-signup/implementation` | — |
| E-2b | Exchange: `GET` Graph `oauth/access_token` with the app id, app secret and the code. The result is the customer's "business token", a **business integration system user access token**. All requests are made server-to-server. | PARTIAL (steps paraphrased; the response field name was not quoted) | `embedded-signup/onboarding-customers-as-a-tech-provider` | The exact response body (the `access_token` field name) and the error when the code has expired or been reused |
| E-2c | Business tokens are "scoped to individual onboarded customers" and useful "without … requiring future re-authentication". **No expiry, refresh or revocation is documented.** The implementation page offers a configuration template named "WhatsApp Embedded Signup Configuration With 60 Expiration Token". | VERIFIED (scope); UNVERIFIED (expiry, refresh, single-use code) | `access-tokens`, `embedded-signup/implementation` | Inspect a real business token's expiry for the configuration ReviewFlow chooses; exchange the same code twice |
| E-3 | "If you are a **Tech Provider**, use a [Business Integration System User access token]" for onboarded customer data. "Most endpoints check if the user identified by the token has access to the queried resource." Whether the platform's own system-user token can act on a customer WABA is not stated. | VERIFIED (the Tech Provider rule); UNVERIFIED (platform token on a customer WABA) | `access-tokens` | — (the documented path is the per-customer business token) |
| E-4 | Before the onboarding calls you need "the customer's WABA ID and business phone number ID (from session logging or an API request)". `GET /{WABA_ID}/phone_numbers` lists a WABA's numbers (M-6). No dedicated "verify ownership" call is documented. | PARTIAL | `embedded-signup/onboarding-customers-as-a-tech-provider`, `business-phone-numbers` | Customer X's business token on customer Y's WABA must fail, and X's `/{waba}/phone_numbers` must contain the selected id |
| E-5a | Subscribe: `POST /<WABA_ID>/subscribed_apps` with the business token → `{"success": true}`. "You must individually subscribe to every WABA for which you want to receive webhooks." `GET` lists and `DELETE` unsubscribes (`{"success": true}`). The `messages` field carries inbound messages and outbound statuses. | VERIFIED (endpoint, response, per-WABA rule); PARTIAL (`GET`/`DELETE`) | `embedded-signup/onboarding-customers-as-a-tech-provider`, `solution-providers/manage-webhooks` | — |
| E-5b | With no override set, webhooks go to "the callback URL set on your app" ("set in the App Dashboard"). Overrides are optional. | VERIFIED | `webhooks/override` | — |
| E-5c | POST payloads are signed in `X-Hub-Signature-256` with an HMAC-SHA256 "using the post body payload and your app secret". They are sent "anytime a webhook event is triggered for any webhook fields you are subscribed to". The page does not say customer-WABA deliveries are covered by name. | PARTIAL | `webhooks/create-webhook-endpoint` | A signed delivery from a customer WABA must verify with `META_APP_SECRET` |
| E-6a | Register: `POST /<PHONE_NUMBER_ID>/register`, body `messaging_product: "whatsapp"`, a 6-digit `pin`, optional `data_localization_region` → `{"success": true}`. "Embedded Signup performs steps 1-3 automatically", so only registration remains. | VERIFIED | `business-phone-numbers/registration`, `solution-providers/registering-phone-numbers` | — |
| E-6b | PIN: if two-step verification is off, the PIN "becomes the business phone number's two-step verification PIN". If it is already on, you must send "the number's 6-digit two-step verification PIN". A forgotten PIN is changed in WhatsApp Manager; "there is no endpoint to disable two-step verification". | VERIFIED | `business-phone-numbers/registration`, `business-phone-numbers/two-step-verification` | Whether numbers fresh from Embedded Signup ever already have two-step verification |
| E-6c | Limit: 10 register/deregister requests per number in a 72-hour moving window; error `133016` then blocks registration for 72 hours. | VERIFIED | `business-phone-numbers/registration` | — |
| E-6d | "A phone number must be registered up to 14 days after going through the Embedded Signup flow." Otherwise the flow must be run again. | VERIFIED | `solution-providers/manage-phone-numbers` | — |
| E-6e | Re-registering an already-registered number (idempotency, error code) is not documented. | UNVERIFIED | — | Register the same number twice |
| E-6f | The Tech Provider guide lists "subscribe" (step 2) before "register" (step 3). Whether that order is required is not stated. The amended spec registers inline before the Celery subscribe. | UNVERIFIED | `embedded-signup/onboarding-customers-as-a-tech-provider` | Register before subscribing on a development WABA, and confirm `messages` deliveries then arrive |
| E-7a | Templates are created and listed per WABA (`/WABA_ID/message_templates`). Names are unique per WABA per language; a duplicate returns error `100`, subcode `2388024`. A named-parameter template carries `example.body_text_named_params[{param_name, example}]` (seen in a GET response). | VERIFIED (scope, duplicate error); PARTIAL (example shape seen in a response, not a create request) | `templates/template-management` | — |
| E-7b | Creating a template on a **customer** WABA with the business token is not described. `whatsapp_business_management` without Advanced access returns error `200` on WABAs "not owned by your business". | UNVERIFIED (business-token creation); VERIFIED (Advanced access requirement) | `templates/template-management`, `solution-providers/get-started-for-tech-providers` | Create and poll a template on a customer WABA with its business token |
| E-8a | Deregister: `POST /<PHONE_NUMBER_ID>/deregister` → `{"success": true}`. It makes the number "unusable with Cloud API"; "Deregistration does not delete a number or its message history". It shares the 10-per-72-hours limit. | VERIFIED | `business-phone-numbers/registration` | — |
| E-8b | Meta notifies through `account_update`: `PARTNER_APP_UNINSTALLED` ("A business customer deauthenticated or uninstalled the app"), carrying `waba_info { waba_id, owner_business_id, partner_app_id }`. Also `PARTNER_REMOVED`, `ACCOUNT_DELETED`, `DISABLED_UPDATE`, `ACCOUNT_RESTRICTION`, `ACCOUNT_OFFBOARDED` and `ACCOUNT_RECONNECTED`. | VERIFIED | `webhooks/reference/account_update` | — |
| E-8c | Revoking a business token from ReviewFlow's side is not documented. | UNVERIFIED | — | Behaviour of the token after `DELETE subscribed_apps` and after the customer uninstalls the app |
| E-9a | Business verification is required before App Review. App Review grants Advanced access to `whatsapp_business_messaging` and `whatsapp_business_management`, which are "required to become a Tech Provider". Before App Review, test users need a role on the app. | VERIFIED (verification, Advanced access); PARTIAL (the pre-review limit) | `solution-providers/get-started-for-tech-providers`, `solution-providers/app-review` | — |
| E-9b | Billing: "Your onboarded clients must add a credit card to your WhatsApp Business Platform Account". The customer adds a payment method in WhatsApp Manager, after which "they're fully onboarded". Credit-line sharing is a Solution Partner mechanism. | VERIFIED (the client adds a payment method); PARTIAL (who pays which charge) | `solution-providers/get-started-for-tech-providers`, `embedded-signup/onboarding-customers-as-a-tech-provider`, `solution-providers/share-and-revoke-credit-lines` | Whether a template send fails before a payment method is added |
| E-10 | Official samples: the `WA_EMBEDDED_SIGNUP` message event (E-1b), `{"success": true}` for subscribe/register/deregister, the `subscribed_apps` GET shape, and the `account_update` payloads (E-8b). **No sample body for `oauth/access_token`.** | PARTIAL | the pages above | The token-exchange response fixture must be captured on a development app, or flagged as written from field names |
| E-11 | Number fields: `code_verification_status`, `name_status` (`APPROVED`, `DECLINED`, `EXPIRED`, `PENDING_REVIEW`, `NONE`), `account_mode`, `platform_type`, `throughput`, `quality_rating`. **No documented "ready to send" criterion.** The onboarding guide's readiness check is an optional test message, plus a payment method for "fully onboarded". | PARTIAL (fields); UNVERIFIED (readiness criterion) | `solution-providers/manage-phone-numbers`, `embedded-signup/onboarding-customers-as-a-tech-provider` | Whether a template send fails with `name_status` other than `APPROVED`, or with no payment method |

### Remaining development-WABA gates (Embedded Signup)

Until each item below is run on a development app and WABA, and recorded
here with a date, it stays as marked. The code that depends on it stays
gated, per the spec's "Implementation blockers".

| Gate | Evidence needed |
|---|---|
| E-1a | A real v4 `FB.login` launch (the exact `extras`) plus a captured `WA_EMBEDDED_SIGNUP` FINISH and CANCEL event |
| E-2b / E-10 | A redacted `oauth/access_token` response body, and the error returned for an expired (more than 30 s) and a reused code |
| E-2c | The expiry of a real business token for the chosen Login for Business configuration |
| E-4 | Customer X's business token is refused on customer Y's WABA, and `GET /{waba}/phone_numbers` with X's token lists the selected id |
| E-5c | A signed delivery from a customer WABA verifies with `META_APP_SECRET` |
| E-6b / E-6e / E-6f | Whether a fresh Embedded Signup number already has a two-step PIN; the result of registering twice; register-before-subscribe works |
| E-7b | Create and poll a template on a customer WABA with its business token |
| E-8c | Token behaviour after the customer removes the app (`PARTNER_APP_UNINSTALLED`) |
| E-9b / E-11 | Whether a template send fails with no payment method, or with `name_status` other than `APPROVED` |

**Correction to M-5f.** The template-management page documents a duplicate
name+language error (`100` / `2388024`); M-5f recorded none. The foundation
code is unaffected: `find_template_id` still recovers correctly. It is
noted here so the docs do not contradict each other.
