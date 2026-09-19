# Coding Standards

These standards exist to keep the architecture's guarantees (tenant isolation, idempotency, testability) intact as the codebase grows past what one person can hold in their head.

## 1. Service-Layer Rule (non-negotiable)

Views and serializers never contain business logic. Every non-trivial operation (eligibility checks, sending, syncing, normalization) lives in a `services.py` function in the owning Django app, called from the view, a Celery task, or a management command alike.

```python
# Wrong
class CampaignViewSet(...):
    def activate(self, request, pk):
        campaign = self.get_object()
        if not campaign.location.google_location_exists...  # logic in the view
            return Response(..., status=422)
        campaign.is_active = True
        campaign.save()

# Right
class CampaignViewSet(...):
    def activate(self, request, pk):
        campaign = self.get_object()
        campaigns.services.activate_campaign(campaign)  # raises on invalid state
        return Response(...)
```

## 2. Tenant Scoping Rule (non-negotiable)

Every tenant-owned model's manager inherits `core.TenantScopedManager` (see `../02-architecture/Multi-Tenancy.md`). Never call `Model.objects.all()` on a tenant-owned model without going through the scoped manager. Celery tasks always receive `merchant_id` explicitly as an argument and set the tenant context at the top of the task body.

## 3. Adapter Pattern for External Systems

Any new POS/e-commerce/WhatsApp/Google provider integration implements the relevant interface (`BaseAdapter`, `WhatsAppProvider`, `GoogleSyncProvider` — see the relevant architecture doc) rather than being special-cased inline. This is what keeps V2 additions from becoming rewrites.

## 4. Idempotency by Default

Any code that processes an inbound event (webhook, retry, Celery task re-run) must be safe to run twice. Prefer database unique constraints over "check then insert" application logic where possible — the constraint is the real guarantee.

## 5. Naming Conventions

- Django apps: lowercase, plural where the app represents a collection of things (`locations`, `transactions`), singular where it represents a concept (`billing`, `analytics`).
- Model fields: `snake_case`, timestamps always suffixed `_at` (`sent_at`, `occurred_at`), booleans prefixed `is_`/`has_` where it reads naturally.
- Status/enum fields: `UPPER_SNAKE_CASE` values (`SCHEDULED`, `QUOTA_EXCEEDED`).

## 6. Migrations

- One logical change per migration where practical.
- Never write a migration that silently drops data — a destructive migration requires an explicit backup step noted in the PR description.

## 7. Testing Expectations

Every new service-layer function ships with a unit test; every new webhook/adapter ships with a fixture-based contract test using a real (anonymized) sample payload. See `Testing-Strategy.md`.

## 8. Pull Request Expectations

- PR description states which architecture doc(s) the change relates to, if any.
- A change that touches a locked architectural decision (see the blueprint's six locked corrections) requires explicit sign-off before merge, not just code review.
