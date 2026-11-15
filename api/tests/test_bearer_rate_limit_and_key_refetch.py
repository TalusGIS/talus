"""A Bearer header is not a pass through the rate limiter, and a token cannot make the api refetch
the sign-in server's keys at will. (Once-over finding A7, decision-once-over-security-fixes.md.)

1. The limiter exempted any request whose Authorization header began "Bearer " — the mistake its own
   docstring describes for Basic and had already fixed there. A junk Bearer therefore bought an
   unthrottled path to every door, each request costing a token parse. Now a Bearer is exempt only
   while its hash is in a short-lived set that `auth._validated_claims` writes AFTER the token has
   validated; any other Bearer is charged to a per-address class of its own.
2. A token naming an unknown key id made the key client refetch the sign-in server's key set, twice,
   on every request. Now an unknown id refetches at most once per interval.
"""
import asyncio
import time

import jwt
import pytest
from jwt import PyJWKClientError
from starlette.responses import Response

from app import auth, ratelimit
from app.auth import _get_signing_key as REAL_GET_SIGNING_KEY  # conftest replaces the module attribute
from app.config import settings
from app.ratelimit import AnonRateLimitMiddleware

# --- 1. the limiter ------------------------------------------------------------------------------


class _Req:
    def __init__(self, authorization=None, ip="203.0.113.7", path="/api/v1/me/layers", method="GET"):
        self.headers = {} if authorization is None else {"authorization": authorization}
        self.method = method
        self.url = type("_U", (), {"path": path})()
        self.client = type("_C", (), {"host": ip})()


@pytest.fixture
def mw(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_trust_forwarded_for", False)
    monkeypatch.setattr(settings, "bearer_bootstrap_rate_per_min", 50)
    monkeypatch.setattr(ratelimit, "_validated_bearer", {})
    return AnonRateLimitMiddleware(app=None)


def _send(mw, req, n=1):
    async def _next(request):
        return Response(status_code=200)

    async def _run():
        return [(await mw.dispatch(req, _next)).status_code for _ in range(n)]

    return asyncio.run(_run())


def test_junk_bearer_is_throttled_per_address(mw):
    codes = _send(mw, _Req("Bearer not-a-token"), 200)
    assert codes[:50] == [200] * 50, "the class budget should pass before it throttles"
    assert codes.count(429) == 150, "a junk Bearer skipped the limiter"


def test_each_different_junk_bearer_shares_the_one_address_budget(mw):
    codes = [_send(mw, _Req(f"Bearer junk-{i}"))[0] for i in range(200)]
    assert codes.count(200) == 50 and codes.count(429) == 150


def test_another_address_has_its_own_budget(mw):
    _send(mw, _Req("Bearer junk", ip="203.0.113.7"), 200)
    assert _send(mw, _Req("Bearer junk", ip="203.0.113.8"), 10) == [200] * 10


def test_a_validated_bearer_is_never_charged(mw):
    token = "header.payload.signature"
    ratelimit.note_validated_bearer(token, time.time() + 300)
    assert _send(mw, _Req(f"Bearer {token}"), 500) == [200] * 500


def test_a_real_session_still_works_from_an_address_whose_bearer_budget_is_spent(mw):
    _send(mw, _Req("Bearer junk"), 200)                       # the flood
    assert _send(mw, _Req("Bearer junk"))[0] == 429
    token = "real.session.token"
    ratelimit.note_validated_bearer(token, time.time() + 300)  # what auth does once it validates
    assert _send(mw, _Req(f"Bearer {token}"), 20) == [200] * 20


def test_validation_is_not_remembered_past_the_tokens_own_expiry(mw):
    token = "about.to.expire"
    ratelimit.note_validated_bearer(token, time.time() - 1)    # already expired: never recorded
    assert ratelimit._validated_bearer == {}
    ratelimit.note_validated_bearer(token, None)               # no exp claim: never recorded
    assert ratelimit._validated_bearer == {}


def test_a_recorded_bearer_falls_back_to_the_class_when_its_entry_lapses(mw, monkeypatch):
    token = "lapsing.session.token"
    ratelimit.note_validated_bearer(token, time.time() + 300)
    real = time.monotonic
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: real() + ratelimit._VALIDATED_TTL_S + 1)
    codes = _send(mw, _Req(f"Bearer {token}"), 200)
    assert codes.count(429) == 150


def test_an_oversize_bearer_header_is_charged_not_hashed(mw):
    codes = _send(mw, _Req("Bearer " + "A" * 100_000), 60)
    assert codes.count(429) == 10


def test_the_anonymous_budget_and_the_bearer_budget_are_separate(mw, monkeypatch):
    monkeypatch.setattr(settings, "anon_read_rate_per_min", 5)
    assert _send(mw, _Req(None), 10).count(429) == 5          # anonymous budget spent…
    assert _send(mw, _Req("Bearer junk"))[0] == 200           # …an unproven Bearer still gets through


def test_stub_mode_keeps_bearer_exempt(mw, monkeypatch):
    """Dev only: nothing validates tokens in stub mode, so nothing could ever be recorded."""
    monkeypatch.setattr(settings, "auth_mode", "stub")
    assert _send(mw, _Req("Bearer anything"), 200) == [200] * 200


def test_validated_claims_records_the_token(mw, make_token):
    token = make_token("11111111-1111-1111-1111-111111111111")
    creds = type("_Creds", (), {"scheme": "Bearer", "credentials": token})()
    claims = asyncio.run(auth._validated_claims(creds))
    assert claims["sub"]
    assert _send(mw, _Req(f"Bearer {token}"), 300) == [200] * 300


def test_a_rejected_token_is_not_recorded(mw, make_token):
    from fastapi import HTTPException

    bad = make_token("11111111-1111-1111-1111-111111111111")[:-4] + "AAAA"   # signature broken
    creds = type("_Creds", (), {"scheme": "Bearer", "credentials": bad})()
    with pytest.raises(HTTPException):
        asyncio.run(auth._validated_claims(creds))
    assert ratelimit._validated_bearer == {}


# --- 2. the key refetch --------------------------------------------------------------------------


class _Key:
    def __init__(self, kid):
        self.key_id = kid
        self.key = f"key-for-{kid}"


class _Client:
    """Counts fetches the way PyJWKClient exposes them: get_signing_keys(refresh=True) is a network
    call; refresh=False answers from the cache."""

    def __init__(self, kids):
        self.kids = list(kids)
        self.after_refresh = None
        self.refreshes = 0
        self.cached_reads = 0

    def get_signing_keys(self, refresh=False):
        if refresh:
            self.refreshes += 1
            if self.after_refresh is not None:
                self.kids = list(self.after_refresh)
        else:
            self.cached_reads += 1
        return [_Key(k) for k in self.kids]

    @staticmethod
    def match_kid(keys, kid):
        return next((k for k in keys if k.key_id == kid), None)

    def get_signing_key_from_jwt(self, token):  # the old path; must no longer be used
        raise AssertionError("PyJWT's own lookup refetches on every unknown key id")


def _token(kid):
    return jwt.encode({"sub": "x"}, "secret", algorithm="HS256", headers={"kid": kid})


@pytest.fixture
def client(monkeypatch):
    c = _Client(["current"])
    monkeypatch.setattr(auth, "_jwks_client", c)
    monkeypatch.setattr(auth, "_jwks_last_forced_refetch", None)
    return c


def test_known_key_id_never_refetches(client):
    for _ in range(50):
        assert asyncio.run(REAL_GET_SIGNING_KEY(_token("current"))).key == "key-for-current"
    assert client.refreshes == 0


def test_a_hundred_unknown_key_ids_cost_one_refetch(client):
    for i in range(100):
        with pytest.raises(PyJWKClientError):
            asyncio.run(REAL_GET_SIGNING_KEY(_token(f"unknown-{i}")))
    assert client.refreshes == 1, f"{client.refreshes} fetches of the sign-in server's keys"


def test_a_real_key_rotation_is_picked_up_by_that_one_refetch(client):
    client.after_refresh = ["current", "rotated"]
    assert asyncio.run(REAL_GET_SIGNING_KEY(_token("rotated"))).key == "key-for-rotated"
    assert client.refreshes == 1


def test_the_budget_refills_after_the_interval(client, monkeypatch):
    with pytest.raises(PyJWKClientError):
        asyncio.run(REAL_GET_SIGNING_KEY(_token("unknown-a")))
    with pytest.raises(PyJWKClientError):
        asyncio.run(REAL_GET_SIGNING_KEY(_token("unknown-b")))
    assert client.refreshes == 1
    real = time.monotonic
    monkeypatch.setattr(auth.time, "monotonic", lambda: real() + auth._JWKS_FORCED_REFETCH_MIN_INTERVAL_S + 1)
    client.after_refresh = ["current", "rotated-later"]
    assert asyncio.run(REAL_GET_SIGNING_KEY(_token("rotated-later"))).key == "key-for-rotated-later"
    assert client.refreshes == 2


def test_a_token_that_is_not_a_token_never_reaches_the_key_client(client):
    with pytest.raises(jwt.PyJWTError):
        asyncio.run(REAL_GET_SIGNING_KEY("not-a-jwt"))
    assert client.refreshes == 0 and client.cached_reads == 0


def test_an_unknown_key_id_with_the_budget_spent_is_a_refusal_not_a_pass(client):
    """Fail-closed: the budget bounds the fetches, never the answer."""
    for _ in range(3):
        with pytest.raises(PyJWKClientError):
            asyncio.run(REAL_GET_SIGNING_KEY(_token("unknown")))
