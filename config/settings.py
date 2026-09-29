"""
ReviewFlow settings. Configured entirely from environment variables;
see .env.example and docs/10-development/Development-Setup.md.
"""
from pathlib import Path
from urllib.parse import urlsplit

import environ
from django.core.exceptions import ImproperlyConfigured
from kombu import Queue

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
# Does not override variables already set in the environment.
environ.Env.read_env(BASE_DIR / ".env")

# Required: no defaults, settings fail to load if missing.
SECRET_KEY = env("DJANGO_SECRET_KEY")
DATABASES = {"default": env.db("DATABASE_URL")}
REDIS_URL = env("REDIS_URL")
# Fernet key for encrypting secrets at rest (OAuth tokens in Phase 09, the
# TOTP secret here). Empty by default; core.crypto raises ImproperlyConfigured
# at first use, not here, so management commands that never touch crypto
# (e.g. collectstatic) still work without it set.
FERNET_KEY = env("FERNET_KEY", default="")

if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise ImproperlyConfigured("DATABASE_URL must point to PostgreSQL.")

DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "core",
    "accounts",
    "locations",
    "auditlog",
    "integrations",
    "customers",
    "transactions",
    "events",
    "apikeys",
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.SessionMerchantMiddleware",
    "apikeys.middleware.ApiKeyMiddleware",
    "core.middleware.TenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Dashboard auth: session + CSRF only (Authentication.md §1).
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["accounts.permissions.IsMerchantMember"],
    "EXCEPTION_HANDLER": "core.api.exception_handler",
    "DEFAULT_THROTTLE_RATES": {
        "login": "5/min",
        "invite_accept": "5/min",
        "login_totp": "5/min",
        "totp_manage": "5/min",
        # Public API (Authentication.md §2): independent per-key and
        # per-IP sliding-window limits.
        "api_key": env("API_KEY_RATE", default="600/min"),
        "api_key_ip": env("API_KEY_IP_RATE", default="1200/min"),
        # Provider webhook receivers (spec 06 Decision 15): unauthenticated,
        # so this runs before any lookup/signature check.
        "webhook_ip": env("WEBHOOK_IP_RATE", default="1200/min"),
    },
    # Trusted reverse proxies in front of the app. DRF throttles key on
    # REMOTE_ADDR when 0; with N > 0 they take the Nth-from-last
    # X-Forwarded-For entry. Never leave it unset: DRF would then trust the
    # whole client-supplied X-Forwarded-For header, letting a rotating value
    # bypass the login throttle. Set it to the real proxy hop count per
    # environment (Cloudflare -> Render) at deploy time.
    "NUM_PROXIES": env.int("DJANGO_NUM_PROXIES", default=0),
}

# Redis cache holds rate-limit counters only, never business data.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "rf",
    }
}

SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = env.bool("DJANGO_SECURE_COOKIES", default=True)

# Celery (SAD.md §5). Redis is broker/result backend only.
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_QUEUES = tuple(
    Queue(name) for name in ("events", "whatsapp", "google_sync", "default")
)
CELERY_TASK_DEFAULT_QUEUE = "default"
# Beat entries are added by the phases that build their tasks.
CELERY_BEAT_SCHEDULE = {
    "retry-failed-events": {
        "task": "events.tasks.retry_failed_events",
        "schedule": 300.0,
        "options": {"queue": "events"},
    },
}
CELERY_TIMEZONE = "UTC"

# Cloudflare R2 (blobs only -- SAD.md, Security-Controls.md §File / Object
# Storage). Read at settings load like every other platform secret; a CSV
# upload/import that never runs (e.g. most test runs) never touches these.
R2_ENDPOINT_URL = env("R2_ENDPOINT_URL", default="")
R2_ACCESS_KEY_ID = env("R2_ACCESS_KEY_ID", default="")
R2_SECRET_ACCESS_KEY = env("R2_SECRET_ACCESS_KEY", default="")
R2_BUCKET = env("R2_BUCKET", default="")

# CSV Import limits (spec 06 Decision 10).
CSV_IMPORT_MAX_BYTES = env.int("CSV_IMPORT_MAX_BYTES", default=5 * 1024 * 1024)
CSV_IMPORT_MAX_ROWS = env.int("CSV_IMPORT_MAX_ROWS", default=10_000)

# Shopify app (spec 06-shopify-app Decision 3). Platform secrets only --
# never stored per merchant, never in Integration.credentials_encrypted or
# config_json (Rules for implementation). Empty defaults: a deployment that
# never installs Shopify (or a test run) never needs these set.
SHOPIFY_CLIENT_ID = env("SHOPIFY_CLIENT_ID", default="")
SHOPIFY_CLIENT_SECRET = env("SHOPIFY_CLIENT_SECRET", default="")
# Optional, set only during a client-secret rotation (Shopify.md "Client-secret
# rotation"): verification and the OAuth callback HMAC then accept both the
# current and the previous secret.
SHOPIFY_CLIENT_SECRET_PREVIOUS = env("SHOPIFY_CLIENT_SECRET_PREVIOUS", default="")
# The API version pinned in every Admin GraphQL request URL -- this is what
# determines the payload version of ReviewFlow's shop-specific webhook
# subscriptions (spec Decision 3 V8/F20), not any shopify.app.toml setting.
SHOPIFY_API_VERSION = env("SHOPIFY_API_VERSION", default="")
# Must exactly match the redirect_uri configured in the Shopify Dev
# Dashboard for this app (F8).
SHOPIFY_REDIRECT_URI = env("SHOPIFY_REDIRECT_URI", default="")
# The post-OAuth ReviewFlow link-page URL on the dashboard/frontend origin
# (spec Decision 3, "Link page URL"; [User decision 2026-09-29, spec
# amendment]). Server-side configuration only -- never derived from a
# Shopify request parameter or the browser.
SHOPIFY_LINK_PAGE_URL = env("SHOPIFY_LINK_PAGE_URL", default="")

# The public host for the shop-specific webhook subscription `uri`
# (spec Decision 3 step 6: `https://<public API host>/api/v1/webhooks/shopify/{id}`).
# Derived from SHOPIFY_REDIRECT_URI's own origin, because the callback that
# receives that redirect_uri is served by this same API -- no separate env
# var. Empty when SHOPIFY_REDIRECT_URI is unset (e.g. most test runs).
if SHOPIFY_REDIRECT_URI:
    _shopify_redirect_parts = urlsplit(SHOPIFY_REDIRECT_URI)
    if _shopify_redirect_parts.scheme != "https":
        raise ImproperlyConfigured("SHOPIFY_REDIRECT_URI must be https.")
    SHOPIFY_WEBHOOK_BASE = f"https://{_shopify_redirect_parts.netloc}"
else:
    SHOPIFY_WEBHOOK_BASE = ""
