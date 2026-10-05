"""The billing write throttle rate is environment-backed (BILLING_WRITE_RATE)
with 10/min as the default (spec 07 O16). The default is read in process, as
apikeys/tests/test_key_rate_limits.py does for the API-key rates; the override
is read in a fresh interpreter, because settings are loaded once per process."""
import os
import subprocess
import sys

from django.conf import settings
from rest_framework.settings import api_settings as drf_api_settings

from billing.views import BillingWriteRateThrottle

PROBE = (
    "import django; django.setup(); "
    "from django.conf import settings; "
    "print(settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['billing_write'])"
)


def test_the_default_billing_write_rate_is_10_per_minute():
    assert drf_api_settings.DEFAULT_THROTTLE_RATES["billing_write"] == "10/min"
    assert BillingWriteRateThrottle().num_requests == 10
    assert BillingWriteRateThrottle().duration == 60


def test_billing_write_rate_env_overrides_the_default():
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings", "BILLING_WRITE_RATE": "3/min"}
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "3/min"
