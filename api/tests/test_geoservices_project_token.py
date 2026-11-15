"""A generated field project's embedded token writes only to the project's own layers.

Once-over 2026-11-15 K6. The token is written in clear into the .qgz, and anyone the project is shared
with can read it. It was minted with no layer list, so through the GeoServices door, which QField uses,
it could add, change and delete on every layer its publisher may edit, every edit recorded as the
publisher's. It now carries the project's layers (`field_projects._scope_project_token`), and the edit
gate tells it from a Talus Field device credential, which carries a list too, by
`field_projects.embedded_pat_id`.

Pure logic, in the style of test_geoservices_device_credential.py.
"""
import asyncio

import pytest

from app.config import settings
from app.models.user import User
from app.routers import geoservices
from app.routers.geoservices import _edit_deny_reason, _is_field_project_token

SLOT = "tal_4c4d0b1e"
OTHER_SLOT = "tal_00000001"
SAYS_PROJECT_ONLY = "can edit only the layers in that project"
SAYS_DEVICE_CREDENTIAL = "Talus Field device credential"

META = {"attachments_enabled": True, "editable": True, "editing_enabled": True,
        "edit_add": True, "edit_attributes": True, "edit_geometry": True, "edit_delete": True}

USER = User(
    id="00000000-0000-0000-0000-0000000000fe",
    username="project-token-test",
    email="project-token-test@talusgis.invalid",
    display_name="Project Token Test",
)


class _Request:
    def __init__(self, pat_layer_ids, token_id="tok-1"):
        self.state = type("_State", (), {
            "pat_scope": "read_write", "pat_layer_ids": pat_layer_ids, "pat_token_id": token_id,
        })()


@pytest.fixture
def downstream(monkeypatch):
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


@pytest.fixture
def is_project(monkeypatch):
    """Answer the project-token question without a database; record what was asked."""
    asked: list = []
    answer = {"value": True}

    async def _lookup(conn, token_id):
        asked.append(token_id)
        return answer["value"]

    monkeypatch.setattr(geoservices, "_is_field_project_token", _lookup)
    return answer, asked


def _reason(layer_id, layer_ids):
    assert settings.auth_mode == "keycloak"
    return asyncio.run(_edit_deny_reason(_Request(layer_ids), None, USER, None, layer_id, dict(META)))


def test_a_project_token_edits_a_layer_in_its_project(downstream, is_project):
    assert _reason(SLOT, [SLOT, OTHER_SLOT]) is None
    assert downstream == ["roles", "edit_allowed"], "the owner's own rights must still decide"
    assert is_project[1] == ["tok-1"]


def test_a_project_token_is_refused_on_a_layer_outside_its_project(downstream, is_project):
    reason = _reason(SLOT, [OTHER_SLOT])
    assert reason is not None and SAYS_PROJECT_ONLY in reason
    assert downstream == [], "refused before the owner's roles or the edit decision are reached"


def test_a_project_token_with_an_empty_list_edits_nothing(downstream, is_project):
    reason = _reason(SLOT, [])
    assert reason is not None and SAYS_PROJECT_ONLY in reason
    assert downstream == []


def test_a_listed_token_that_is_not_a_project_token_is_still_a_device_credential(downstream, is_project):
    is_project[0]["value"] = False
    reason = _reason(SLOT, [SLOT])
    assert reason is not None and SAYS_DEVICE_CREDENTIAL in reason
    assert downstream == []


# --- the lookup itself ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, row):
        self._row = row
        self.sql = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, q, params=None):
        self.sql = (q, params)

    async def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, row):
        self.cur = _Cursor(row)

    def cursor(self):
        return self.cur


def test_the_lookup_reads_the_embedded_token_column():
    conn = _Conn((1,))
    assert asyncio.run(_is_field_project_token(conn, "tok-9")) is True
    q, params = conn.cur.sql
    assert "embedded_pat_id" in q and params == ("tok-9",)


def test_the_lookup_answers_no_when_no_project_holds_the_token():
    assert asyncio.run(_is_field_project_token(_Conn(None), "tok-9")) is False


def test_no_token_id_is_never_a_project_token():
    """Fail closed: a request with no token id is not a project token, so a listed one is refused."""
    assert asyncio.run(_is_field_project_token(None, None)) is False


# --- the list is set where the token is minted and where it is reused -----------------------------


def test_generate_and_regenerate_both_give_the_token_the_projects_layers():
    """The gate above only narrows a token that carries a list. Before K6 the project's token was
    minted with none, so these two call sites are the fix itself."""
    import inspect

    from app.routers import field_projects

    for fn in (field_projects.generate_field_project, field_projects.regenerate_field_project):
        src = inspect.getsource(fn)
        assert "_scope_project_token(" in src, f"{fn.__name__} does not set the project token's layer list"


def test_the_list_is_the_projects_layer_ids():
    from types import SimpleNamespace

    from app.routers.field_projects import _scope_project_token

    class _Cur:
        def __init__(self):
            self.calls = []

        async def execute(self, q, params=None):
            self.calls.append((q, params))

    cur = _Cur()
    specs = [SimpleNamespace(layer_id=SLOT), SimpleNamespace(layer_id=OTHER_SLOT)]
    asyncio.run(_scope_project_token(cur, "pat-1", specs))
    (q, params), = cur.calls
    assert "UPDATE platform.personal_access_tokens SET layer_ids" in q
    assert params == ([SLOT, OTHER_SLOT], "pat-1")
