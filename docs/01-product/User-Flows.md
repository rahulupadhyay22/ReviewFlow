# User Flows

## 1. Merchant Onboarding Flow

1. Sign up (email/password) -> create `Merchant` + first `Location`.
2. Connect at least one integration (native POS, generic webhook, or CSV as a fallback).
3. Configure WhatsApp sender: connect own WhatsApp Business number (Embedded Signup) or use the shared platform number instantly.
4. Submit/select a message template; wait for Meta approval.
5. Connect Google (optional but nudged): OAuth -> list accessible Google locations -> map each to an internal `Location`.
6. Activate the first `ReviewCampaign`.
7. Dashboard shows a "waiting for first sale" empty state until real data flows in.

## 2. WhatsApp Onboarding Flow

- **Own number**: merchant chooses "I have a WhatsApp Business number" -> Meta Embedded Signup flow -> number registered under the platform's Meta app -> `WhatsAppAccount(sender_type=OWN_NUMBER)` created -> `WhatsAppLocationMapping` created for the relevant location(s).
- **Shared number**: merchant chooses "Use shared number" -> location(s) mapped instantly to the platform's existing `WhatsAppAccount(sender_type=SHARED_POOL)`, zero extra steps.
- Either path: merchant submits/selects a template -> template approval polled -> campaign becomes activatable once approved.

## 3. Google OAuth Flow

1. Merchant clicks "Connect Google" -> redirected to Google consent screen.
2. Callback returns an authorization code -> exchanged for access + refresh tokens -> stored encrypted on `GoogleConnection`.
3. Platform calls Google's location-list API for that connection.
4. Merchant maps each returned Google location to an internal `Location` via the mapping UI.
5. `GoogleLocation` rows created; first sync enqueued immediately, not waiting for the next scheduled tick.
6. Subsequent syncs run on schedule (see `06-automation/Google-Reviews.md`).

## 4. Sale-to-Review Customer Journey

```
POS/e-commerce sale completed
   -> webhook received & verified
   -> normalized to internal event, Customer/Transaction created
   -> eligibility check passes
   -> CampaignExecution scheduled (delay_minutes after the sale)
   -> WhatsApp message sent: "Thanks for visiting {{business_name}}! We'd love to hear
      about your experience. [Review us on Google]"
   -> customer clicks -> Google's own review composer opens
   -> customer manually writes and submits the review on Google
   -> review synced back into the merchant dashboard on the next sync cycle
```

The platform never composes, submits, or edits the review itself — see `06-automation/Google-Reviews.md`.

## 5. Optional Feedback Journey (Mode B campaigns)

```
Sale -> WhatsApp -> our feedback page (rating + comment + categories)
     -> Google review CTA shown to the customer regardless of the rating they gave
     -> customer optionally continues to Google
```

The Google invitation is never withheld or hidden based on the feedback rating — this is a hard business rule (see `Business-Rules.md`).

## 6. QR Code Scan Flow

Customer scans a QR code (counter, table, receipt, packaging) -> short redirect URL (`reviewflow.app/q/<code>`) -> scan is logged -> customer is redirected to either the Google review link or the feedback page, depending on how the QR code was configured.
