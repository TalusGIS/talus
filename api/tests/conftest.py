"""Unit-test harness for the auth path: in-process RS256 keypair + injection seam.

No live Keycloak, no live DB. Tokens are minted with a throwaway RSA key whose
matching public key is fed into auth._get_signing_key, so jwt.decode performs
real RS256 signature verification entirely in-process. _user_from_db is
overridden per-test by the individual tests.
"""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import auth
from app.config import settings



@pytest.fixture(scope="session", autouse=True)
def _configure_logging():
    """Route the suite's structured log calls through the real pipeline (and
    assert configure_logging() itself doesn't raise). Not strictly required —
    structlog.get_logger() works unconfigured — but this exercises the wiring."""
    from app.logging_config import configure_logging

    configure_logging()


@pytest.fixture(scope="session")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(autouse=True)
def force_keycloak_mode(monkeypatch):
    """Take the JWT-validation branch of get_current_user regardless of .env."""
    monkeypatch.setattr(settings, "auth_mode", "keycloak")


@pytest.fixture(autouse=True)
def inject_signing_key(monkeypatch, rsa_keypair):
    """Replace the JWKS network fetch with our in-process public key.

    PyJWT's decode() is called as jwt.decode(token, signing_key.key, ...), so the
    seam just needs to return an object exposing `.key`.
    """
    _, public_key = rsa_keypair

    class _Key:
        key = public_key

    async def _fake_get_signing_key(token: str):
        return _Key()

    monkeypatch.setattr(auth, "_get_signing_key", _fake_get_signing_key)


@pytest.fixture
def make_token(rsa_keypair):
    """Factory: mint a valid RS256 token for `sub`, with claims that satisfy
    get_current_user's require-list. iss/aud are read from the same settings the
    validator checks against, so the token is self-consistent regardless of .env."""
    private_key, _ = rsa_keypair

    def _make(sub: str, **overrides):
        now = int(time.time())
        claims = {
            "sub": sub,
            "iss": settings.keycloak_issuer,
            "aud": settings.keycloak_audience,
            "iat": now,
            "exp": now + 300,
        }
        claims.update(overrides)
        return jwt.encode(claims, private_key, algorithm="RS256")

    return _make
