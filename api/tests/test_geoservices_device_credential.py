"""A Talus Field device credential cannot edit through the GeoServices door.

A device credential is a read_write personal access token that also carries the layer list of ONE
pack (V108). The native write doors enforce that list (`auth.rw_pat_gate`) and have no delete for it.
The GeoServices edit gate checked only the token's scope, so the same credential could add, change
and DELETE features on every layer its owner may edit — on a lost phone, for up to 30 days — while
the devices page says it reaches the pack's layers and can never delete. The Field app never calls
this door, so the credential is refused here outright: inside its pack or outside it.

Pure logic, in the style of test_geoservices_edit_gate.py: the refusal must come BEFORE the owner's
roles are fetched or the edit decision is run, so neither is reachable in these tests.
"""
import asyncio

import pytest

from app.config import settings
from app.models.user import User
from app.routers import geoservices
from app.routers.geoservices import _edit_deny_reason

SLOT = "tal_4c4d0b1e"
OTHER_SLOT = "tal_00000001"
SAYS_DEVICE_CREDENTIAL = "Talus Field device credential"

META = {"attachments_enabled": True, "editable": True, "editing_enabled": True,
        "edit_add": True, "edit_attributes": True, "edit_geometry": True, "edit_delete": True}

USER = User(
    id="00000000-0000-0000-0000-0000000000fd",
    username="device-credential-test",
    email="device-credential-test@talusgis.invalid",
    display_name="Device Credential Test",
)


class _Request:
    def __init__(self, pat_scope, pat_layer_ids):
        self.state = type("_State", (), {"pat_scope": pat_scope, "pat_layer_ids": pat_layer_ids})()


@pytest.fixture
def no_downstream(monkeypatch):
    """Fail loudly if the gate goes on to resolve roles or run the edit decision."""
    reached: list[str] = []

    async def _roles(user_id):
        reached.append("roles")
        return {"sub": user_id, "realm_access": {"roles": ["talus-editor"]}}

    async def _edit_allowed(conn, claims, user, layer_id):
        reached.append("edit_allowed")
        return True, "ok"

    monkeypatch.setattr(geoservices, "_pat_owner_claims", _roles)
    monkeypatch.setattr(geoservices, "edit_allowed", _edit_allowed)
    return reached


def _reason(layer_id, *, pat_scope, pat_layer_ids):
    assert settings.auth_mode == "keycloak"
    return asyncio.run(_edit_deny_reason(_Request(pat_scope, pat_layer_ids), None, USER, None, layer_id, dict(META)))


@pytest.mark.parametrize(
    "label,layer_id,layer_ids",
    [
        ("a layer outside its pack", SLOT, [OTHER_SLOT]),
        ("a layer inside its pack", SLOT, [SLOT, OTHER_SLOT]),
        ("an empty layer list is still a device credential", SLOT, []),
    ],
)
def test_device_credential_is_refused_before_roles_or_access_are_consulted(label, layer_id, layer_ids, no_downstream):
    reason = _reason(layer_id, pat_scope="read_write", pat_layer_ids=layer_ids)
    assert reason is not None and SAYS_DEVICE_CREDENTIAL in reason, label
    assert no_downstream == [], f"{label}: the gate went on to {no_downstream}"


def test_an_ordinary_read_and_edit_token_still_reaches_the_edit_decision(no_downstream):
    """The control: the same call with no layer list is NOT refused here — so the tests above fail
    for the layer list and nothing else."""
    assert _reason(SLOT, pat_scope="read_write", pat_layer_ids=None) is None
    assert no_downstream == ["roles", "edit_allowed"]


def test_a_read_only_token_keeps_its_own_sentence(no_downstream):
    reason = _reason(SLOT, pat_scope="read", pat_layer_ids=None)
    assert reason is not None and "This access token is read-only." in reason
    assert no_downstream == []


# --- the layer list itself: "could not read it" is never "there is none" -------------------------


class _Cursor:
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a):
        raise self._exc


class _Conn:
    def __init__(self, exc):
        self._exc = exc
        self.rolled_back = False

    def cursor(self):
        return _Cursor(self._exc)

    async def rollback(self):
        self.rolled_back = True


def test_a_failed_read_of_the_layer_list_is_an_error_not_an_ordinary_token():
    """None means "an ordinary token", which the gate above lets through. A database error must
    therefore propagate — otherwise a device credential would pass as an ordinary one."""
    from psycopg import errors as pg_errors

    from app import auth

    with pytest.raises(pg_errors.OperationalError):
        asyncio.run(auth._pat_layer_ids(_Conn(pg_errors.OperationalError("connection lost")), "token-id"))


def test_only_the_missing_column_reads_as_no_list():
    from psycopg import errors as pg_errors

    from app import auth

    conn = _Conn(pg_errors.UndefinedColumn("column layer_ids does not exist"))
    assert asyncio.run(auth._pat_layer_ids(conn, "token-id")) is None
    assert conn.rolled_back is True
