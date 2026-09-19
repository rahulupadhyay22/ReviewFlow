# ReviewFlow — Final Architecture Review

## Status: READY FOR PHASE 0 IMPLEMENTATION

The final documentation pass resolves the previously identified architecture inconsistencies without changing the core product direction.

### Locked decisions

1. **Integration scope and ownership**
   - `Integration` is merchant-level.
   - `IntegrationLocationMapping` maps an integration to one or more internal locations.
   - `IntegrationEvent.merchant_id` and `integration_id` are required at creation.
   - Idempotency is `UNIQUE(integration_id, external_event_id)`.

2. **Campaign concurrency**
   - One transaction may receive at most one active/sent review request across all campaigns.
   - Eligibility + execution creation uses `SELECT FOR UPDATE` on the transaction.
   - `(campaign_id, transaction_id)` remains a database backstop.

3. **Frequency cap**
   - V1 frequency capping is merchant-wide across campaigns and locations.
   - The requesting campaign supplies the effective cap window.

4. **Quota**
   - Quota is atomically reserved when `CampaignExecution` enters `SENDING`.
   - `SCHEDULED` and `QUOTA_EXCEEDED` consume zero quota.
   - A provider failure after reservation still consumes one usage unit.
   - Retries never consume another unit.
   - `QUOTA_EXCEEDED` is retained for 7 days, then becomes `EXPIRED`.
   - Resume re-checks eligibility and returns to normal scheduling; no burst sending.

5. **Refunds**
   - Refund while `SCHEDULED` or `QUOTA_EXCEEDED` → `CANCELLED`, zero usage.
   - Refund after quota reservation → no usage reversal.
   - Dispatch re-checks transaction status immediately before reservation.

6. **Subscription/payment policy**
   - Razorpay is the V1 payment gateway.
   - `PAST_DUE` grace period is 7 calendar days.
   - Sending is disabled immediately during `PAST_DUE`.
   - Dunning attempts are scheduled for days 0, 3, and 6.
   - Recovery returns the subscription to `ACTIVE`.
   - Unrecovered payment by the end of day 7 → `EXPIRED`.
   - `PaymentAttempt` makes payment/dunning operations idempotent.

7. **Google identifiers**
   - `google_location_id` = Google Business Profile location resource identifier.
   - `google_place_id` = Place ID used for the public review URL.
   - The two identifiers are stored separately and are never substituted for one another.

8. **RLS**
   - `SET LOCAL app.current_merchant_id` is executed inside each tenant transaction.
   - Direct tenant tables compare their `merchant_id` to the transaction-local value.
   - Transitive tenant tables use documented parent/`EXISTS` RLS checks.
   - No ordinary application connection bypasses RLS.

9. **Retention**
   - Raw integration payloads: 90 days.
   - WhatsApp message content/log detail: 90 days.
   - Customer/feedback/Google review PII: 24 months after relevant inactivity/update, subject to legal holds.
   - Audit logs: 365 days minimum, subject to longer legal/security holds.
   - A daily purge/redaction job enforces these operational defaults.

10. **WhatsApp send recovery**
    - Quota reservation and `SENDING` transition are durable before the provider call.
    - A 1:1 `WhatsAppMessage` is created in `QUEUED` state using the execution as the internal idempotency key.
    - Reconciliation handles worker crashes and provider timeouts without creating another execution or consuming another quota unit.
    - Exactly-once external delivery is not claimed where the provider cannot guarantee it; provider status lookup is preferred before uncertain resend.

## Remaining work that is implementation-specific, not architecture-open

- Provider-specific Shopify/WooCommerce/Petpooja/GoFrugal/Zapier/Make payload specifications.
- Production deployment/monitoring/runbooks.
- Detailed analytics query specification.
- Legal/tax/invoice review of the locked operational retention and payment defaults.
- Verification of current Meta/Google provider requirements during adapter implementation.

These do not block the core architecture sign-off. The core ReviewFlow documentation is internally consistent and ready for Phase 0 implementation.
