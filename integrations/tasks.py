"""Celery tasks for Phase 06 ingestion (spec 06 Decision 10). Thin wrapper
around integrations.services -- no business logic here (Coding-Standards.md
§1). Never scheduled with eta/countdown."""
from celery import shared_task

from core.tenancy import tenant_task
from integrations import services


@shared_task(queue="events")
@tenant_task
def import_csv(merchant_id, integration_id, object_key):
    """merchant_id is explicit and first (Multi-Tenancy.md §Layer 1) --
    tenant_task sets the tenant context before any query."""
    services.import_csv_rows(integration_id=integration_id, object_key=object_key)
