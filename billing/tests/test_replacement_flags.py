"""W2 of 07-plan-change-replacement: the four settings and the two accessors
that read them (`replacement_enabled`, `replacement_window`). Both flags default
OFF; the two windows are in seconds and have NO default, so a flag turned on
without its window is `503 billing_not_configured` for that kind.

The default-value tests read the loaded Django settings, so they assume the four
environment variables are unset, as in CI. Nothing here claims any Razorpay limit."""
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings as django_settings

from billing import services
from billing.exceptions import BillingNotConfigured

KINDS = ("UPGRADE", "DOWNGRADE")
WINDOW_SETTING = {
    "UPGRADE": "BILLING_REPLACEMENT_UPGRADE_AUTH_WINDOW",
    "DOWNGRADE": "BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN",
}
FLAG_SETTING = {
    "UPGRADE": "BILLING_REPLACEMENT_UPGRADE_ENABLED",
    "DOWNGRADE": "BILLING_REPLACEMENT_DOWNGRADE_ENABLED",
}
ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


# --- defaults ---------------------------------------------------------------------


def test_both_flags_default_off_and_both_windows_have_no_default():
    assert django_settings.BILLING_REPLACEMENT_UPGRADE_ENABLED is False
    assert django_settings.BILLING_REPLACEMENT_DOWNGRADE_ENABLED is False
    assert django_settings.BILLING_REPLACEMENT_UPGRADE_AUTH_WINDOW is None
    assert django_settings.BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN is None


@pytest.mark.parametrize("kind", KINDS)
def test_by_default_nothing_is_enabled_and_no_window_is_assumed(kind):
    assert services.replacement_enabled(kind) is False
    with pytest.raises(BillingNotConfigured):
        services.replacement_window(kind)


def test_the_env_example_documents_all_four_names_without_giving_any_a_value():
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    names = list(FLAG_SETTING.values()) + list(WINDOW_SETTING.values())
    for name in names:
        assert any(line.startswith(f"# {name}=") for line in lines), name
        # never an active assignment: the windows in particular have no default
        assert not any(line.startswith(f"{name}=") for line in lines), name


# --- the flags --------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_a_flag_turns_on_only_its_own_kind(settings, kind):
    other = "DOWNGRADE" if kind == "UPGRADE" else "UPGRADE"
    setattr(settings, FLAG_SETTING[kind], True)
    setattr(settings, FLAG_SETTING[other], False)
    assert services.replacement_enabled(kind) is True
    assert services.replacement_enabled(other) is False


@pytest.mark.parametrize("value", [False, None, 0, "", "false", "yes", 1])
def test_only_a_real_true_enables_a_kind(settings, value):
    settings.BILLING_REPLACEMENT_UPGRADE_ENABLED = value
    assert services.replacement_enabled("UPGRADE") is False


# --- the windows ------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_a_positive_integer_number_of_seconds_is_the_window(settings, kind):
    setattr(settings, WINDOW_SETTING[kind], 3600)
    assert services.replacement_window(kind) == timedelta(hours=1)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("bad", [None, 0, -5, "300", 1.5, True, False])
def test_an_unset_or_unusable_window_is_billing_not_configured(settings, kind, bad):
    setattr(settings, WINDOW_SETTING[kind], bad)
    with pytest.raises(BillingNotConfigured):
        services.replacement_window(kind)


def test_each_kind_reads_only_its_own_window(settings):
    settings.BILLING_REPLACEMENT_UPGRADE_AUTH_WINDOW = 600
    settings.BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN = None
    assert services.replacement_window("UPGRADE") == timedelta(minutes=10)
    with pytest.raises(BillingNotConfigured):
        services.replacement_window("DOWNGRADE")

    settings.BILLING_REPLACEMENT_UPGRADE_AUTH_WINDOW = None
    settings.BILLING_REPLACEMENT_DOWNGRADE_EXPIRE_MARGIN = 900
    assert services.replacement_window("DOWNGRADE") == timedelta(minutes=15)
    with pytest.raises(BillingNotConfigured):
        services.replacement_window("UPGRADE")


def test_a_flag_on_with_its_window_unset_is_the_503(settings):
    """The flag alone is not enough: with the window unset the kind answers 503."""
    settings.BILLING_REPLACEMENT_UPGRADE_ENABLED = True
    settings.BILLING_REPLACEMENT_UPGRADE_AUTH_WINDOW = None
    assert services.replacement_enabled("UPGRADE") is True
    with pytest.raises(BillingNotConfigured) as info:
        services.replacement_window("UPGRADE")
    assert (info.value.http_status, info.value.code) == (503, "billing_not_configured")
