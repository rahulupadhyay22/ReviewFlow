"""Django Admin for events (spec 16): the read-only dead-letter review.

IntegrationEvent is deliberately NOT registered as a ModelAdmin: the stock
changelist evaluates its queryset while rendering and the change form opens
its own transaction.atomic(), both outside the tenant transaction. This view
reads only through auditlog.services.staff_merchant_read and renders plain
dicts after the block. The payload is never selected (spec 16 D3). The route
is added by config.admin.ReviewFlowAdminSite.get_urls()."""
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.template.response import TemplateResponse
from django.views.decorators.http import require_GET

from accounts.models import Merchant
from auditlog.services import StaffAccessDenied, staff_merchant_read
from events.services import list_dead_letter_events

DEAD_LETTER_RESOURCE = "events.integrationevent.dead_letter"


@require_GET
def dead_letter_events_view(request, merchant_id):
    try:
        with staff_merchant_read(
            staff_user=request.user, merchant_id=merchant_id, resource=DEAD_LETTER_RESOURCE
        ) as merchant:
            total, events = list_dead_letter_events()
    except Merchant.DoesNotExist:
        raise Http404("Merchant not found.") from None
    except StaffAccessDenied:
        # Admin views bypass the DRF exception handler; an unmapped domain
        # error would be a 500. admin_view already blocks non-staff, so this
        # is defence in depth.
        raise PermissionDenied from None
    context = {
        **admin.site.each_context(request),
        "title": f"Dead-letter events: {merchant.name}",
        "merchant": merchant,
        "total": total,
        "events": events,
    }
    return TemplateResponse(request, "admin/events/dead_letter_events.html", context).render()
