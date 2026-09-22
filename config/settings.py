"""
ReviewFlow settings. Configured entirely from environment variables;
see .env.example and docs/10-development/Development-Setup.md.
"""
from pathlib import Path

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
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.SessionMerchantMiddleware",
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
CELERY_BEAT_SCHEDULE = {}
CELERY_TIMEZONE = "UTC"
