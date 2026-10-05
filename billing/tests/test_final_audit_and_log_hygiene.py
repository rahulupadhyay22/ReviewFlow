"""Audit and log hygiene across the merchant-facing billing flow (spec 07 DoD:
"each real transition writes exactly one [AuditLog row], with no payment id or
URL in its metadata"; "no log record ... contains the body, the signature or a
secret"; Security-Controls "never log tokens/payloads"). One flow drives an
INCOMPLETE subscription to ACTIVE through the sync (the only way in), upgrades
it and cancels it through the real HTTP views, with provider failures
interleaved. The provider is the FakeProvider: this verifies ReviewFlow's
handling only, not Razorpay's contract.

Also: billing audit rows are tenant-scoped (merchant B gets none)."""
import json
import logging

import pytest

from auditlog.models import AuditLog
from billing import services
from billing.exceptions import BillingProviderRejected, BillingProviderUnavailable
from billing.tests.snapshot_helpers import REF, START, entity, invoice
from core.tenancy import tenant_atomic, tenant_context

pytestmark = pytest.mark.django_db

CHECKOUT = "/api/v1/billing/checkout"
CANCEL = "/api/v1/billing/subscription/cancel"
KEY_SECRET = "key_secret_value"  # set by the `provider` fixture
IDENTIFIERS = ("pay_", "inv_", "sub_", "http", KEY_SECRET, "rzp_")


@pytest.fixture
def flow(lifecycle_setup, session_client):
    """Runs the whole flow once and returns the setup."""
    w = lifecycle_setup
    client = session_client(w.a.user.email)
    post = lambda path, body=None: client.post(  # noqa: E731
        path, body or {}, format="json", HTTP_X_CSRFTOKEN=client.csrf
    )

    w.sub(w.a, "INCOMPLETE")
    w.provider.entities[REF] = entity(w.small, status="active", start=START)
    w.provider.invoices = [invoice(payment_id="pay_AuditHygiene01", id="inv_AuditHygiene01")]
    with tenant_context(w.a.merchant.id):
        services.sync_subscription()  # INCOMPLETE -> ACTIVE on a paid invoice

    w.responses = []
    w.provider.update_error = BillingProviderRejected("plan_change_refused")
    w.responses.append(post(CHECKOUT, {"plan_id": str(w.big.id)}))  # refused: 409, no audit
    w.provider.update_error = BillingProviderUnavailable()
    w.responses.append(post(CHECKOUT, {"plan_id": str(w.big.id)}))  # timeout: 502, no audit
    w.provider.update_error = None
    w.responses.append(post(CHECKOUT, {"plan_id": str(w.big.id)}))  # upgrade
    w.provider.cancel_error = BillingProviderUnavailable()
    w.responses.append(post(CANCEL))  # 502, no audit
    w.provider.cancel_error = None
    w.responses.append(post(CANCEL))  # cancel at period end
    return w


def audit_rows(merchant):
    with tenant_context(merchant.id), tenant_atomic():
        return list(AuditLog.objects.filter(action__startswith="billing.").order_by("created_at", "id"))


def test_the_flow_ends_where_the_spec_says(flow):
    assert [r.status_code for r in flow.responses] == [409, 502, 200, 502, 200]
    assert flow.responses[0].json()["error"]["code"] == "plan_change_unsupported"


def test_billing_audit_metadata_never_carries_a_payment_id_url_reference_or_secret(flow):
    rows = audit_rows(flow.a.merchant)
    assert len(rows) >= 3  # activation, plan change, cancellation
    for row in rows:
        blob = json.dumps(row.metadata_json) if row.metadata_json is not None else ""
        for needle in IDENTIFIERS:
            assert needle not in blob, (row.action, needle)


def test_failed_provider_calls_wrote_no_audit_row(flow):
    actions = [r.action for r in audit_rows(flow.a.merchant)]
    # One row per real change: the sync activation, the upgrade and the cancel
    # request. The 409 and both 502s changed nothing, so wrote nothing.
    assert len(actions) == 3, actions
    assert len(set(actions)) == 3, actions


def test_billing_audit_rows_are_tenant_scoped(flow):
    assert audit_rows(flow.b.merchant) == []
    with tenant_context(flow.b.merchant.id), tenant_atomic():
        assert AuditLog.objects.filter(action__startswith="billing.").count() == 0


def test_no_log_record_of_the_flow_contains_a_reference_payment_id_or_secret(lifecycle_setup, session_client, caplog):
    w = lifecycle_setup
    client = session_client(w.a.user.email)
    with caplog.at_level(logging.DEBUG):
        w.sub(w.a, "INCOMPLETE")
        w.provider.entities[REF] = entity(w.small, status="active", start=START)
        w.provider.invoices = [invoice(payment_id="pay_LogHygiene001", id="inv_LogHygiene001")]
        with tenant_context(w.a.merchant.id):
            services.sync_subscription()
        w.provider.update_error = BillingProviderRejected("PLANTED_PROVIDER_CODE")
        client.post(CHECKOUT, {"plan_id": str(w.big.id)}, format="json", HTTP_X_CSRFTOKEN=client.csrf)
        w.provider.update_error = None
        w.provider.fetch_error = BillingProviderUnavailable()
        client.post(CANCEL, {}, format="json", HTTP_X_CSRFTOKEN=client.csrf)
    for secret in (REF, "pay_LogHygiene001", "inv_LogHygiene001", KEY_SECRET, "PLANTED_PROVIDER_CODE"):
        assert secret not in caplog.text, secret
