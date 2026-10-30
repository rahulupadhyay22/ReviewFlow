"""Shared test helpers for google_reviews (feature-local, like
whatsapp/tests/helpers.py): the Google HTTP fake, a test RSA key + ID-token
signer, flow drivers and small DB readers.

The ONLY mocked boundary is `urllib.request.urlopen` as seen by the provider.
The RSA key is generated at import; its public half is served as the fixture
JWKS. No Google key material is committed.
"""
import base64
import hashlib
import hmac
import io
import json
import time
import urllib.error
import uuid
from datetime import timedelta
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.db import connection
from django.utils import timezone

from accounts.models import TeamMember, User
from auditlog.models import AuditLog
from core.crypto import encrypt
from core.tenancy import tenant_atomic, tenant_context
from google_reviews import services
from google_reviews.models import GoogleOAuthState
from google_reviews.providers import google_business_profile as gbp
from google_reviews.tests.conftest import PASSWORD

FIXTURES = Path(__file__).parent / "fixtures"
CLIENT_ID = "test-client-id.apps.googleusercontent.com"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ACCOUNTS_URL = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
LOCATIONS_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1/"
REVIEWS_BASE = "https://mybusiness.googleapis.com/v4/"
ISS = "https://accounts.google.com"
MISSING = object()  # remove this key from a claim/body

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

GOOGLE_TABLES = [
    "google_reviews_googleconnection",
    "google_reviews_googleoauthstate",
    "google_reviews_googlelocation",
    "google_reviews_googlereview",
]


def fixture_json(name):
    return json.loads((FIXTURES / name).read_text())


def sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


# --- the Google HTTP fake -----------------------------------------------------------


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status, self.body, self.headers = status, body, headers or {}


class _Ok:
    def __init__(self, raw, headers):
        self._raw = raw
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._raw


class FakeHttp:
    """Replaces urllib.request.urlopen. Routes by longest URL prefix. Each route
    holds responses consumed in order (the last one repeats); an item may be a
    JSON-able value (200), a Resp, an exception instance (raised), or a callable
    taking the request. Every call is recorded with connection.in_atomic_block."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def route(self, prefix, *responses):
        self.routes[prefix] = list(responses)

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(
            SimpleNamespace(method=req.get_method(), url=url, in_atomic=connection.in_atomic_block, data=req.data, timeout=timeout)
        )
        prefixes = [p for p in self.routes if url.startswith(p)]
        if not prefixes:
            raise AssertionError(f"unexpected Google call: {url}")
        queue = self.routes[max(prefixes, key=len)]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(item):
            item = item(req)
        if isinstance(item, Exception):
            raise item
        resp = item if isinstance(item, Resp) else Resp(200, item)
        raw = resp.body if isinstance(resp.body, bytes) else json.dumps(resp.body).encode()
        if resp.status >= 400:
            hdrs = Message()
            for key, value in resp.headers.items():
                hdrs[key] = value
            raise urllib.error.HTTPError(url, resp.status, "error", hdrs, io.BytesIO(raw))
        return _Ok(raw, resp.headers)

    def to(self, prefix):
        return [c for c in self.calls if c.url.startswith(prefix)]

    @staticmethod
    def form(req):
        return {k: v[0] for k, v in parse_qs(req.data.decode()).items()}


@pytest.fixture
def http(monkeypatch):
    gbp.reset_jwks_cache()
    fake = FakeHttp()
    monkeypatch.setattr(gbp.urllib.request, "urlopen", fake)
    yield fake
    gbp.reset_jwks_cache()


@pytest.fixture
def google_settings(settings):
    settings.GOOGLE_INTEGRATION_ENABLED = True
    settings.GOOGLE_OAUTH_CLIENT_ID = CLIENT_ID
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "test-client-secret"
    settings.GOOGLE_OAUTH_REDIRECT_URI = "https://app.example.com/google/callback"


@pytest.fixture
def enqueues(monkeypatch):
    """Records exchange_oauth_code.delay and refresh_connection_token.delay as
    (args, in_atomic_block). Celery is eager under pytest, so without this a
    delay would run the task inline."""
    from google_reviews import tasks

    rec = SimpleNamespace(exchange=[], refresh=[])
    monkeypatch.setattr(
        tasks.exchange_oauth_code, "delay", lambda *a: rec.exchange.append((a, connection.in_atomic_block))
    )
    monkeypatch.setattr(
        tasks.refresh_connection_token, "delay", lambda *a: rec.refresh.append((a, connection.in_atomic_block))
    )
    return rec


# --- ID tokens ------------------------------------------------------------------------


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwk(key, kid="k1", **over):
    numbers = key.public_key().public_numbers()
    out = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }
    out.update(over)
    return out


def jwks(*keys):
    return {"keys": list(keys) or [jwk(KEY)]}


def make_claims(nonce, /, **over):
    now = int(time.time())
    claims = {
        "iss": ISS,
        "aud": CLIENT_ID,
        "sub": "sub-123",
        "email": "owner@example.com",
        "email_verified": True,
        "nonce": nonce,
        "iat": now,
        "exp": now + 3600,
    }
    for name, value in over.items():
        if value is MISSING:
            claims.pop(name, None)
        else:
            claims[name] = value
    return claims


def sign(claims, *, kid="k1", alg="RS256", key=KEY):
    header = {"alg": alg, "kid": kid, "typ": "JWT"}
    signing = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"
    if alg == "RS256":
        signature = key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
    elif alg == "HS256":
        pem = KEY.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        signature = hmac.new(pem, signing.encode(), hashlib.sha256).digest()
    else:  # "none"
        signature = b""
    return f"{signing}.{b64(signature)}"


def token_body(id_token, **over):
    body = fixture_json("token_response.json")
    body["id_token"] = id_token
    for name, value in over.items():
        if value is MISSING:
            body.pop(name, None)
        else:
            body[name] = value
    return body


def wire(http, nonce, *, claims=None, token=None, kid="k1", alg="RS256", key=KEY, max_age=None, id_token=None):
    """A token endpoint answer carrying a signed ID token, plus the JWKS route.
    Returns the ID token string."""
    id_token = id_token or sign(make_claims(nonce, **(claims or {})), kid=kid, alg=alg, key=key)
    http.route(TOKEN_URL, token_body(id_token, **(token or {})))
    headers = {"Cache-Control": f"public, max-age={max_age}"} if max_age is not None else {}
    http.route(JWKS_URL, Resp(200, jwks(), headers))
    return id_token


# --- flow drivers and readers ---------------------------------------------------------


def start(owner):
    """POST /google/oauth/start through the service. Returns (state, nonce)."""
    with tenant_context(owner.merchant_id):
        url = services.begin_google_oauth(actor=owner.user)["authorization_url"]
    query = parse_qs(urlparse(url).query)
    return query["state"][0], query["nonce"][0]


def claim(owner, state, code="auth-code-1"):
    with tenant_context(owner.merchant_id):
        return services.complete_google_oauth(actor=owner.user, code=code, state=state)


def pending_attempt(owner, code="auth-code-1"):
    """start + complete. Returns (attempt pk, nonce, state)."""
    state, nonce = start(owner)
    return claim(owner, state, code).pk, nonce, state


def exchange(owner, attempt_pk):
    with tenant_context(owner.merchant_id):
        services.exchange_attempt(attempt_pk)


def attempt_row(owner, pk):
    with tenant_context(owner.merchant_id), tenant_atomic():
        return GoogleOAuthState.objects.filter(pk=pk).values(
            "status", "failure_code", "code_enc", "google_connection_id", "completed_at", "expires_at", "user_id"
        ).get()


def connection_rows(owner):
    from google_reviews.models import GoogleConnection

    with tenant_context(owner.merchant_id), tenant_atomic():
        return list(GoogleConnection.objects.order_by("created_at").values())


def audit_rows(owner):
    with tenant_context(owner.merchant_id), tenant_atomic():
        return list(AuditLog.objects.order_by("created_at").values("action", "metadata_json", "target_id", "actor_user_id"))


def make_state(owner, *, status="ISSUED", expires_in=600, code=False, failure_code=None, connection=None, user=None):
    """A GoogleOAuthState row written directly (to set up lifecycle edge cases)."""
    with tenant_context(owner.merchant_id), tenant_atomic():
        return GoogleOAuthState.objects.create(
            merchant_id=owner.merchant_id,
            user=user or owner.user,
            state_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            nonce_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            status=status,
            code_enc=encrypt("auth-code-1") if code else None,
            failure_code=failure_code,
            google_connection=connection,
            expires_at=timezone.now() + timedelta(seconds=expires_in),
        )


def db_text(merchant_id):
    """Every row of every Google table and of the audit log, as JSON text, read
    under the tenant context (used to prove a secret is stored nowhere)."""
    out = []
    with tenant_context(merchant_id), tenant_atomic(), connection.cursor() as cursor:
        for table in [*GOOGLE_TABLES, "auditlog_auditlog"]:
            cursor.execute(f"SELECT row_to_json(t)::text FROM {table} t")
            out.extend(row[0] for row in cursor.fetchall())
    return "\n".join(out)


def add_member(merchant, role, email):
    user = User.objects.create_user(email, PASSWORD)
    with tenant_context(merchant.id), tenant_atomic():
        return TeamMember.objects.create(merchant=merchant, user=user, role=role, accepted_at=timezone.now())


def make_key(owner, scopes=("reviews:read",)):
    from apikeys import services as apikey_services

    with tenant_context(owner.merchant_id):
        return apikey_services.create_api_key(actor=owner, scopes=list(scopes))


def api_post(client, path, body=None):
    return client.post(f"/api/v1/{path}", body or {}, format="json", HTTP_X_CSRFTOKEN=client.csrf)
