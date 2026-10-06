"""Plain helpers for the plan-change replacement tests (spec
07-plan-change-replacement). Not a conftest, and no test module imports another.

`set_replacement` / `set_retired` put a subscription row into the state the later
phases will create, by writing the columns directly: the code that creates those
states (checkout, switch, abandon) does not exist yet, and these helpers do not
stand in for it. `replacement_entity` builds the provider entity a replacement
ref would return, in the shape billing.services consumes."""
from datetime import timedelta

from django.utils import timezone as dj_timezone

from billing.models import Subscription
from billing.tests import snapshot_helpers
from core.tenancy import tenant_atomic, tenant_context

REPL_REF = "sub_test_repl"
RETIRED_REF = "sub_test_retired"


def set_replacement(
    merchant, *, ref=REPL_REF, expires_in=timedelta(hours=1), committed=False, downgrade_to=None
):
    """The merchant's subscription gets a replacement ref and its deadline. A
    downgrade replacement also sets pending_plan (spec rule 8); `committed`
    records the commit-intent marker."""
    fields = {
        "replacement_provider_ref": ref,
        "replacement_expires_at": dj_timezone.now() + expires_in,
        "replacement_committed_at": dj_timezone.now() if committed else None,
    }
    if downgrade_to is not None:
        fields["pending_plan"] = downgrade_to
    with tenant_context(merchant.id), tenant_atomic():
        Subscription.objects.update(**fields)


def clear_replacement(merchant):
    """The replacement columns back to NULL, as an abandon will leave them."""
    with tenant_context(merchant.id), tenant_atomic():
        Subscription.objects.update(
            replacement_provider_ref=None,
            replacement_expires_at=None,
            replacement_committed_at=None,
            replacement_cancel_confirmed_at=None,
            pending_plan=None,
        )


def set_retired(merchant, *, ref=RETIRED_REF, kind=Subscription.RetiredKind.SWITCHED_OLD):
    with tenant_context(merchant.id), tenant_atomic():
        Subscription.objects.update(retired_provider_ref=ref, retired_kind=kind)


def replacement_entity(plan, *, ref=REPL_REF, status="authenticated", **kwargs):
    """The provider entity for a replacement ref (see snapshot_helpers.entity)."""
    return snapshot_helpers.entity(plan, status=status, ref=ref, **kwargs)
