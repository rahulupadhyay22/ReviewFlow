"""Billing domain errors (spec 07). Each declares its HTTP mapping for
core.api.exception_handler. Messages are fixed text: never a provider
response body, payload value or credential."""
from core.exceptions import ReviewFlowError


class BillingProviderUnavailable(ReviewFlowError):
    """Razorpay timed out, failed at the transport level, answered 5xx, or
    returned something unparseable. Nothing is left behind locally."""

    http_status = 502
    code = "billing_provider_unavailable"

    def __init__(self):
        super().__init__("The payment provider did not respond as expected.")


class BillingProviderRejected(ReviewFlowError):
    """Razorpay answered 4xx. Carries the provider's error code, the HTTP
    status and a short sanitised `reason` (never the description or body). The
    status and reason exist only so a refusal can be classified
    (billing.services.is_update_unsupported_refusal, spec
    07-plan-change-replacement): they are excluded from `__str__`, logs, audit
    metadata and every response. No HTTP mapping of its own: callers translate
    it (e.g. a refused plan update becomes PlanChangeUnsupported)."""

    def __init__(
        self,
        provider_code: str | None = None,
        *,
        status: int | None = None,
        reason: str | None = None,
    ):
        self.provider_code = provider_code
        self.status = status
        self.reason = reason
        super().__init__("The payment provider refused the request.")


class BillingNotConfigured(ReviewFlowError):
    """RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are unset."""

    http_status = 503
    code = "billing_not_configured"

    def __init__(self):
        super().__init__("Billing is not configured.")


class PlanNotAvailable(ReviewFlowError):
    """Missing, malformed, unknown or inactive plan, or one without a
    provider_plan_id. Raised as a domain error (not a DRF ValidationError)
    so it renders the same 422 shape."""

    http_status = 422
    code = "validation_error"

    def __init__(self):
        super().__init__("Invalid input.")
        self.field_errors = {"plan_id": ["Choose an available plan."]}


class SubscriptionPastDue(ReviewFlowError):
    http_status = 409
    code = "subscription_past_due"

    def __init__(self):
        super().__init__("The subscription is past due. Cancel it before checking out again.")


class SubscriptionCancelling(ReviewFlowError):
    http_status = 409
    code = "subscription_cancelling"

    def __init__(self):
        super().__init__("The subscription is set to cancel at the end of the period.")


class SubscriptionActivating(ReviewFlowError):
    http_status = 409
    code = "subscription_activating"

    def __init__(self):
        super().__init__("The subscription is being activated. Try again shortly.")


class PlanChangeUnsupported(ReviewFlowError):
    http_status = 409
    code = "plan_change_unsupported"

    def __init__(self):
        super().__init__("The payment method on this subscription does not allow a plan change.")


class SubscriptionNotCancellable(ReviewFlowError):
    http_status = 409
    code = "subscription_not_cancellable"

    def __init__(self):
        super().__init__("There is no subscription to cancel.")


class WebhookRejected(ReviewFlowError):
    """A Razorpay webhook with a missing or invalid signature, or a missing
    event id. Every failure mode is this identical response (fail closed,
    Authentication.md §3)."""

    http_status = 401
    code = "invalid_signature"

    def __init__(self):
        super().__init__("Invalid webhook signature.")


class InvalidWebhookPayload(ReviewFlowError):
    http_status = 400
    code = "invalid_payload"

    def __init__(self):
        super().__init__("The webhook payload is not a valid event.")


class ProviderStateUnsupported(ReviewFlowError):
    """The provider subscription is in a state ReviewFlow never touches:
    `authenticated` on a CANCELLED/EXPIRED row, `paused`, or an unrecognized
    status (spec "Which provider states may be touched"). Nothing is changed
    and the merchant contacts support."""

    http_status = 409
    code = "subscription_provider_state_unsupported"

    def __init__(self):
        super().__init__(
            "The payment provider subscription is in a state that cannot be changed from here. "
            "Contact support."
        )


class SubscriptionPlanUnsupported(ReviewFlowError):
    """A known provider status whose plan maps to no ReviewFlow Plan row.
    Nothing is changed and the merchant contacts support."""

    http_status = 409
    code = "subscription_plan_unsupported"

    def __init__(self):
        super().__init__("The payment provider subscription uses a plan ReviewFlow does not offer. Contact support.")


class ReplacementInProgress(ReviewFlowError):
    """A plan-change replacement is already pending (any other plan, including
    the current one), or the retired slot is still occupied."""

    http_status = 409
    code = "replacement_in_progress"

    def __init__(self):
        super().__init__("A plan change is already in progress.")


class NoCreditAcknowledgementRequired(ReviewFlowError):
    """A replacement upgrade forfeits the old plan's unused paid time, so the
    OWNER must acknowledge it. Nothing has changed when this is raised."""

    http_status = 422
    code = "no_credit_acknowledgement_required"

    def __init__(self):
        super().__init__("Acknowledge that unused time on the current plan is not credited.")


class ReplacementActivating(ReviewFlowError):
    """The provider reports the replacement `active`: ReviewFlow never cancels
    it. Nothing is changed."""

    http_status = 409
    code = "replacement_activating"

    def __init__(self):
        super().__init__("The new plan is being activated.")


class ReplacementCommitted(ReviewFlowError):
    """A committed downgrade replacement cannot be abandoned to keep the old
    plan. Nothing is changed."""

    http_status = 409
    code = "replacement_committed"

    def __init__(self):
        super().__init__("The plan change can no longer be undone.")


class PlanUnchanged(ReviewFlowError):
    """Checkout for the plan already in force, or one with the same price
    (spec: `422 validation_error`). A domain error, not a DRF ValidationError,
    so it renders the same 422 shape without touching the request."""

    http_status = 422
    code = "validation_error"

    def __init__(self):
        super().__init__("Invalid input.")
        self.field_errors = {"plan_id": ["Choose a different plan."]}
