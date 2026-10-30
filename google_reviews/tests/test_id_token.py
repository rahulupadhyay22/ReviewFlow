"""ID-token validation (spec 09 AM-2, G-15): every forgery case ends the attempt
FAILED with no connection write. Run against a generated test RSA key served as
the fixture JWKS; the only mock is the Google HTTP boundary (`http`)."""
import base64
import json
import time
import urllib.error
from types import SimpleNamespace

import pytest

from google_reviews.providers import google_business_profile as gbp
from google_reviews.providers.base import IdTokenInvalid
from google_reviews.tests.helpers import (
    CLIENT_ID,
    JWKS_URL,
    KEY,
    MISSING,
    OTHER_KEY,
    TOKEN_URL,
    Resp,
    attempt_row,
    audit_rows,
    b64,
    connection_rows,
    enqueues,  # noqa: F401
    exchange,
    google_settings,  # noqa: F401
    http,  # noqa: F401
    jwk,
    jwks,
    make_claims,
    pending_attempt,
    sign,
    wire,
)

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("google_settings", "http", "enqueues"),
]


@pytest.fixture
def owner(make_merchant):
    return make_merchant("A")


def tampered(nonce):
    header, payload, signature = sign(make_claims(nonce)).split(".")
    padded = payload + "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    claims["sub"] = "attacker-sub"
    return {"id_token": f"{header}.{b64(json.dumps(claims).encode())}.{signature}"}


NOW = int(time.time())

INVALID_CASES = {
    "alg-none": lambda n: {"alg": "none"},
    "alg-hs256-signed-with-the-public-key": lambda n: {"alg": "HS256"},
    "tampered-payload": tampered,
    "signed-by-a-key-not-in-the-jwks": lambda n: {"key": OTHER_KEY},
    "unknown-kid": lambda n: {"kid": "never-published"},
    "wrong-issuer": lambda n: {"claims": {"iss": "https://evil.example"}},
    "wrong-audience": lambda n: {"claims": {"aud": "another-client"}},
    "audience-list-without-azp": lambda n: {"claims": {"aud": [CLIENT_ID, "another-client"]}},
    "audience-list-with-wrong-azp": lambda n: {"claims": {"aud": [CLIENT_ID], "azp": "another-client"}},
    "audience-list-without-our-client": lambda n: {"claims": {"aud": ["another-client"], "azp": CLIENT_ID}},
    "expired": lambda n: {"claims": {"exp": NOW - 10}},
    "exp-not-an-integer": lambda n: {"claims": {"exp": "9999999999"}},
    "missing-nonce": lambda n: {"claims": {"nonce": MISSING}},
    "nonce-of-another-attempt": lambda n: {"claims": {"nonce": "the-nonce-of-another-attempt"}},
    "two-segments": lambda n: {"id_token": "aaa.bbb"},
    "garbage": lambda n: {"id_token": "not-a-jwt"},
}


def run(owner, http, **wire_kwargs):
    pk, nonce, _ = pending_attempt(owner)
    wire(http, nonce, **wire_kwargs)
    exchange(owner, pk)
    return pk


@pytest.mark.parametrize("name", list(INVALID_CASES))
def test_a_forged_or_invalid_id_token_fails_the_attempt_and_writes_no_connection(owner, http, name):
    pk, nonce, _ = pending_attempt(owner)
    wire(http, nonce, **INVALID_CASES[name](nonce))

    exchange(owner, pk)

    row = attempt_row(owner, pk)
    assert (row["status"], row["failure_code"], row["code_enc"]) == ("FAILED", "ID_TOKEN_INVALID", None)
    assert connection_rows(owner) == []
    assert [a for a in audit_rows(owner) if a["action"].startswith("google.")] == []


def test_a_correctly_signed_token_with_valid_claims_connects(owner, http):
    pk = run(owner, http)

    assert attempt_row(owner, pk)["status"] == "COMPLETED"
    assert len(connection_rows(owner)) == 1


@pytest.mark.parametrize("issuer", ["https://accounts.google.com", "accounts.google.com"])
def test_both_google_issuers_are_accepted(owner, http, issuer):
    pk = run(owner, http, claims={"iss": issuer})
    assert attempt_row(owner, pk)["status"] == "COMPLETED"


def test_an_audience_list_is_accepted_when_it_holds_our_client_and_azp_matches(owner, http):
    pk = run(owner, http, claims={"aud": [CLIENT_ID, "another-client"], "azp": CLIENT_ID})
    assert attempt_row(owner, pk)["status"] == "COMPLETED"


@pytest.mark.parametrize("delta,accepted", [(0, False), (1, False), (-1, True)], ids=["now==exp", "now>exp", "now<exp"])
def test_expiry_is_rejected_on_or_after_exp(owner, http, monkeypatch, delta, accepted):
    exp = int(time.time()) + 1000
    fake_now = float(exp + delta)  # the clock as the provider sees it
    monkeypatch.setattr(gbp, "time", SimpleNamespace(time=lambda: fake_now, monotonic=time.monotonic))
    pk = run(owner, http, claims={"exp": exp})

    row = attempt_row(owner, pk)
    if accepted:
        assert row["status"] == "COMPLETED"
    else:
        assert (row["status"], row["failure_code"]) == ("FAILED", "ID_TOKEN_INVALID")


def test_a_jwks_key_that_is_not_rs256_is_not_considered(owner, http):
    pk, nonce, _ = pending_attempt(owner)
    wire(http, nonce)
    http.route(JWKS_URL, jwks(jwk(KEY, alg="RS384")))

    exchange(owner, pk)

    assert attempt_row(owner, pk)["failure_code"] == "ID_TOKEN_INVALID"


@pytest.mark.parametrize(
    "answer",
    [Resp(500, {"error": "x"}), urllib.error.URLError("timed out"), Resp(200, b"<html>not json</html>")],
    ids=["5xx", "network", "unparseable"],
)
def test_an_unfetchable_jwks_ends_id_token_unverifiable_and_is_not_retried(owner, http, answer):
    pk, nonce, _ = pending_attempt(owner)
    wire(http, nonce)
    http.route(JWKS_URL, answer)

    exchange(owner, pk)
    exchange(owner, pk)

    row = attempt_row(owner, pk)
    assert (row["status"], row["failure_code"]) == ("FAILED", "ID_TOKEN_UNVERIFIABLE")
    assert len(http.to(TOKEN_URL)) == 1
    assert connection_rows(owner) == []


def test_tokeninfo_is_never_called(owner, http):
    run(owner, http)

    assert {c.url for c in http.calls} <= {TOKEN_URL, JWKS_URL}
    assert not [c for c in http.calls if "tokeninfo" in c.url]


# --- JWKS caching (provider level) ----------------------------------------------------------


def verify(token):
    return gbp.GoogleBusinessProfileProvider().verify_id_token(token)


def test_two_validations_within_max_age_fetch_the_jwks_once(http):
    http.route(JWKS_URL, Resp(200, jwks(), {"Cache-Control": "public, max-age=3600"}))
    token = sign(make_claims("n"))

    verify(token)
    verify(token)

    assert len(http.to(JWKS_URL)) == 1


def test_an_unknown_kid_triggers_exactly_one_refetch_and_a_rotated_key_then_verifies(http):
    cache = {"Cache-Control": "public, max-age=3600"}
    http.route(JWKS_URL, Resp(200, jwks(jwk(KEY)), cache), Resp(200, jwks(jwk(KEY), jwk(OTHER_KEY, "k2")), cache))
    verify(sign(make_claims("n")))
    assert len(http.to(JWKS_URL)) == 1

    claims = verify(sign(make_claims("n"), kid="k2", key=OTHER_KEY))

    assert len(http.to(JWKS_URL)) == 2
    assert claims.sub == "sub-123"


def test_a_still_unknown_kid_after_the_refetch_is_invalid(http):
    http.route(JWKS_URL, Resp(200, jwks(), {"Cache-Control": "public, max-age=3600"}))
    verify(sign(make_claims("n")))

    with pytest.raises(IdTokenInvalid):
        verify(sign(make_claims("n"), kid="nope"))

    assert len(http.to(JWKS_URL)) == 2
