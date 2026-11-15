"""A caller with no token claims holds no role. (Once-over finding A3,
decision-once-over-security-fixes.md.)

`_has_edit_role`, `_has_publish_role` and `_has_admin_role` used to return True for `claims is None`,
meant for stub mode (dev, no token at all). In keycloak mode `claims is None` ALSO describes every
caller on an optional-auth door who arrived with a personal access token: a real principal, resolved
from HTTP Basic, with no JWT. So such a caller was an administrator to any gate that asked the
helpers — a viewer's read-only token downloaded the metadata of items the viewer could not see.

The conftest forces keycloak mode for every test; the stub-mode cases below switch it back
explicitly, so both halves of the rule are pinned.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException

from app import authz
from app.authz import _has_admin_role, _has_edit_role, _has_publish_role
from app.config import settings
from app.models.user import User
from app.routers import metadata

HELPERS = [_has_edit_role, _has_publish_role, _has_admin_role]
ADMIN_CLAIMS = {"realm_access": {"roles": ["talus-admin", "talus-publisher", "talus-editor"]}}


@pytest.mark.parametrize("helper", HELPERS)
def test_no_claims_is_no_role_in_keycloak_mode(helper):
    assert settings.auth_mode == "keycloak"
    assert helper(None) is False


@pytest.mark.parametrize("helper", HELPERS)
def test_no_claims_is_still_authorized_in_stub_mode(helper, monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "stub")
    assert helper(None) is True


@pytest.mark.parametrize("helper", HELPERS)
def test_real_claims_are_read_as_before(helper):
    assert helper(ADMIN_CLAIMS) is True
    assert helper({"realm_access": {"roles": []}}) is False
    assert helper({}) is False


def _viewer() -> User:
    return User(id=uuid.uuid4(), username="viewer", email="viewer@example.org", display_name="Viewer")


class _NoConn:
    """The gates below must not reach the database for a signed-in caller."""


@pytest.mark.parametrize("item_type", ["layer", "web_map", "raster"])
def test_token_caller_without_view_is_refused_metadata_download(item_type, monkeypatch):
    """The finding itself, at the gate: a principal with NO claims (a token caller) who can neither
    view nor own the item is refused. Before the fix `_authorize_manage` answered True for a
    web_map or raster (admin by default), and `manage_allowed` did the same for a layer."""
    asked: list[tuple] = []

    async def _deny(user_id, relation, object_id, *, object_type="layer"):
        asked.append((relation, object_type))
        return False

    async def _no_view(user, layer_id, **kw):
        return False

    async def _not_owner(user, layer_id, **kw):
        return False

    monkeypatch.setattr(metadata, "_openfga_check", _deny)
    monkeypatch.setattr(metadata, "view_allowed", _no_view)
    monkeypatch.setattr(authz, "is_owner", _not_owner)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(metadata._authorize_metadata_read(_NoConn(), None, _viewer(), item_type, str(uuid.uuid4())))
    assert exc.value.status_code == 403
    if item_type != "layer":
        assert ("owner", item_type) in asked, "the owner check was skipped — admin was assumed"


def test_token_caller_who_can_view_still_downloads(monkeypatch):
    """The fix narrows nothing for a token whose owner may see the item."""
    async def _allow_view_only(user_id, relation, object_id, *, object_type="layer"):
        return relation == "can_view"

    monkeypatch.setattr(metadata, "_openfga_check", _allow_view_only)
    asyncio.run(metadata._authorize_metadata_read(_NoConn(), None, _viewer(), "raster", str(uuid.uuid4())))


def test_manage_allowed_for_a_token_caller_falls_to_ownership(monkeypatch):
    seen: list[str] = []

    async def _owner(user, layer_id, **kw):
        seen.append(layer_id)
        return False

    monkeypatch.setattr(authz, "is_owner", _owner)
    assert asyncio.run(authz.manage_allowed(None, _viewer(), "layer_x")) is False
    assert seen == ["layer_x"], "ownership was never checked — admin was assumed"
