"""Unit tests for the user-resolution path in app.auth.get_current_user.

Goal: prove resolution works for a `sub` that is NOT the magic validate-phase1
UUID (00000000-0000-0000-0000-000000000001), so the lookup is not accidentally
coupled to the well-known test user.

Scope note: this is a unit test of the LOOKUP path. It deliberately does NOT
verify the real uuid5-derivation contract (sub -> platform.users row) — that's a
separate live-DB check.
"""
import asyncio
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app import auth
from app.auth import STUB_USER, get_current_user
from app.models.user import User

# Arbitrary, fixed, non-magic sub. Deterministic and obviously unrelated to the
# stub UUID …0001. The uuid5 seed string is kept verbatim so the value is unchanged;
# only the constant name was generalized off the retired dev-crew-1 login.
NON_MAGIC_SUB_ID = uuid5(NAMESPACE_URL, "talus-test://dev-crew-1")


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_resolves_non_magic_user(monkeypatch, make_token):
    # Guard: the test subject must genuinely differ from the stub user.
    assert NON_MAGIC_SUB_ID != STUB_USER.id

    expected = User(
        id=NON_MAGIC_SUB_ID,
        username="test-user-1",
        email="test-user-1@talusgis.invalid",
        display_name="Test User One",
    )

    async def fake_user_from_db(conn, user_id):
        return expected if user_id == NON_MAGIC_SUB_ID else None

    monkeypatch.setattr(auth, "_user_from_db", fake_user_from_db)

    token = make_token(str(NON_MAGIC_SUB_ID))
    claims = asyncio.run(auth._validated_claims(creds=_creds(token)))
    user = asyncio.run(get_current_user(claims=claims, conn=None))

    assert isinstance(user, User)
    assert user.id == NON_MAGIC_SUB_ID
    assert user.id != STUB_USER.id


def test_unknown_user_raises_401(monkeypatch, make_token):
    # Same valid token, but no platform.users row resolves for this sub.
    async def fake_user_from_db(conn, user_id):
        return None

    monkeypatch.setattr(auth, "_user_from_db", fake_user_from_db)

    token = make_token(str(NON_MAGIC_SUB_ID))
    claims = asyncio.run(auth._validated_claims(creds=_creds(token)))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_current_user(claims=claims, conn=None))

    assert exc_info.value.status_code == 401
