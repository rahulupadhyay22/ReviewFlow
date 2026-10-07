"""W2 of 07-plan-change-replacement: the refusal classifier
`billing.services.is_update_unsupported_refusal` (G-0).

The known-refusal set ships EMPTY, so with it empty nothing classifies. The
non-empty cases use clearly synthetic values: no real Razorpay refusal is known,
and none is asserted here."""
import pytest

from billing import razorpay, services
from billing.exceptions import BillingProviderRejected, BillingProviderUnavailable

SYNTHETIC = (400, "SYNTHETIC_CODE", "synthetic_reason")


def _rejected(code="SYNTHETIC_CODE", status=400, reason="synthetic_reason"):
    return BillingProviderRejected(code, status=status, reason=reason)


@pytest.fixture
def synthetic_set(monkeypatch):
    monkeypatch.setattr(razorpay, "UPDATE_UNSUPPORTED_REFUSALS", frozenset({SYNTHETIC}))


# --- shipped (empty) set: nothing classifies -----------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        _rejected(),
        _rejected("BAD_REQUEST_ERROR", 400, "input_validation_failed"),
        _rejected("plan_change_refused", 400, None),  # codes the merged tests use
        _rejected("PLANTED_PROVIDER_CODE", 400, None),
        BillingProviderRejected(),
        BillingProviderRejected("BAD_REQUEST_ERROR"),
    ],
)
def test_with_the_shipped_empty_set_no_refusal_classifies(error):
    assert razorpay.UPDATE_UNSUPPORTED_REFUSALS == frozenset()
    assert services.is_update_unsupported_refusal(error) is False


# --- a populated set: exact match only -----------------------------------------------


def test_the_exact_status_code_and_reason_classify(synthetic_set):
    assert services.is_update_unsupported_refusal(_rejected()) is True


@pytest.mark.parametrize(
    "error",
    [
        _rejected(status=422),  # different status
        _rejected(code="OTHER_CODE"),  # different code
        _rejected(reason="other_reason"),  # different reason
        _rejected(reason=None),  # missing reason
        _rejected(code=None),  # missing code
        _rejected(status=None),  # missing status
        _rejected(status=True),  # a bool is not a status
        _rejected(status="400"),  # not an int
        _rejected(code=400),  # not a string
        _rejected(reason=400),  # not a string
    ],
)
def test_anything_short_of_an_exact_match_does_not_classify(synthetic_set, error):
    assert services.is_update_unsupported_refusal(error) is False


@pytest.mark.parametrize(
    "error",
    [BillingProviderUnavailable(), ValueError("x"), RuntimeError("x"), None, "text", (400, "SYNTHETIC_CODE")],
)
def test_only_a_rejected_error_can_classify(synthetic_set, error):
    assert services.is_update_unsupported_refusal(error) is False


def test_the_set_is_read_at_call_time_so_evidence_changes_take_effect(monkeypatch):
    error = _rejected()
    assert services.is_update_unsupported_refusal(error) is False
    monkeypatch.setattr(razorpay, "UPDATE_UNSUPPORTED_REFUSALS", frozenset({SYNTHETIC}))
    assert services.is_update_unsupported_refusal(error) is True
    monkeypatch.setattr(razorpay, "UPDATE_UNSUPPORTED_REFUSALS", frozenset())
    assert services.is_update_unsupported_refusal(error) is False
