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
