"""Plain helpers and constants for the Razorpay webhook tests (W6). Not a
conftest: importing from a conftest is the circular-import trap, and REF here
differs from test_services.REF. The fixtures that use them (webhook_secret,
webhook_setup) live in billing/tests/conftest.py."""
import hashlib
import hmac
import json
from pathlib import Path

from rest_framework.test import APIClient

from auditlog.models import AuditLog
from billing import razorpay, tasks
from billing.models import BillingEvent, PaymentAttempt, Subscription, UsageRecord
from core.tenancy import tenant_atomic, tenant_context

URL = "/api/v1/billing/webhooks/razorpay"
SECRET = "whsec_local_test_only"
FIXTURE = Path(__file__).parent / "fixtures" / "razorpay_subscription_charged.json"
REF = "sub_TestSub000001"
PLAN_ID = "plan_TestPlan00001"
START, END = 1790000000, 1792592000
# The real fetch_invoices (the D1 stop marker), captured at import time,
# before any fixture replaces it.
ORIGINAL_FETCH_INVOICES = razorpay.fetch_invoices


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def charged(**over) -> dict:
    payload = json.loads(FIXTURE.read_text())
    payload.update(over)
    return payload


def event(event_type="subscription.charged", ref=REF, status="active", **over) -> dict:
    payload = charged(event=event_type)
    payload["payload"]["subscription"]["entity"].update(id=ref, status=status)
    if event_type != "subscription.charged":
        payload["payload"].pop("payment", None)
        payload["contains"] = ["subscription"]
    payload.update(over)
    return payload


def deliver(body, *, event_id="evt_test_0001", signature="__sign__", client=None, raw=False, secret=SECRET):
    data = body if raw else json.dumps(body).encode()
    headers = {}
    if signature == "__sign__":
        headers["HTTP_X_RAZORPAY_SIGNATURE"] = sign(data, secret)
    elif signature is not None:
        headers["HTTP_X_RAZORPAY_SIGNATURE"] = signature
    if event_id is not None:
        headers["HTTP_X_RAZORPAY_EVENT_ID"] = event_id
    return (client or APIClient()).post(URL, data=data, content_type="application/json", **headers)


def rows(merchant):
    with tenant_context(merchant.id), tenant_atomic():
        sub = Subscription.objects.get()
        return {
            "status": sub.status,
            "events": list(BillingEvent.objects.values_list("provider_event_id", "processed_at")),
            "payments": list(PaymentAttempt.objects.values_list("provider_attempt_id", flat=True)),
            "usage": UsageRecord.objects.count(),
            "audits": list(
                AuditLog.objects.filter(action__startswith="billing.").values_list("action", flat=True)
            ),
        }


def no_billing_rows(*merchants):
    for m in merchants:
        r = rows(m)
        assert r["events"] == [] and r["payments"] == []


def deliver_committed(capture, *args, **kwargs):
    """deliver() with on-commit callbacks run, asserting a 200."""
    with capture(execute=True):
        resp = deliver(*args, **kwargs)
    assert resp.status_code == 200
    return resp


def processed(merchant) -> dict:
    """{provider_event_id: has been processed} for the merchant's events."""
    return {event_id: at is not None for event_id, at in rows(merchant)["events"]}


def run_task(merchant_id) -> None:
    """The real Celery task body, run inline."""
    tasks.sync_subscription(merchant_id)
