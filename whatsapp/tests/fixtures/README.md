# Fixture provenance (spec 08, gate M-8)

All values are fake: phones `+919999990001` / `15550000001`, ids `TEST-*`,
`wamid.TEST*`. Source for every payload: `docs/05-integrations/WhatsApp-Meta.md`.

- `inbound_text_stop.json`, `inbound_text_other.json`, `status_delivered.json`,
  `status_read.json`, `status_failed.json`, `status_sent.json`: anonymized
  adaptations of Meta's own documented webhook examples (M-4, M-4b,
  developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages
  and /webhooks/status-messages).
- `inbound_stop_with_status.json`: the two documented shapes combined in one
  `value` (messages[] and statuses[] together), built by hand.
- **Written from documented field names, not from an official sample (M-8):**
  `inbound_text_reply_with_context.json` (`context.id` is UNVERIFIED, M-4),
  `template_status_*.json`, `template_create_response.json`,
  `quality_rating.json`, `send_response.json`.
- **Written from documented field names, not from an official sample (M-8,
  M-5f):** `template_list_page1.json` / `template_list_page2.json`, a two-page
  `GET /{WABA}/message_templates` listing for `MetaCloudProvider.find_template_id`.
  The derived template name `rf_00000000000000000000000000000001` is the one
  for `uuid.UUID(int=1)`; page 1 holds an unrelated template and the same name
  in another language, page 2 (the last page, no `paging.next`) holds the
  match. The paging key names (`paging.cursors.after`, `paging.next`) are the
  standard Graph API shape and only partly confirmed.
