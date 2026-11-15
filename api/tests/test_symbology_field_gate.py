"""The symbology field gate — `_require_field`'s full truth table
(decision-retire-builtins.md phase 1).

WHY THIS FILE EXISTS: the gate had no tests at all. It validated candidate renderer
fields against `information_schema` ONLY, so a hosted (slot) layer — whose owner-defined
fields live in the `props` jsonb — could be symbolized on `row_version` but never on its
own `condition_code`. Found while rebuilding the demo layers on the substrate: the whole
point of V022's seeded ramp ("signs coloured Good/Fair/Poor/Replace at a glance") could
not be re-authored through the product API.

Pure units against fake connections: the gate's job is branch selection, and a fake
proves every branch deterministically on any box, in CI, forever. Each case is written
as a MUTATION CHECK — remove a predicate from the gate and a named test here goes red:

  * drop `AND active`                  -> test_props_lookup_filters_on_active
  * drop the denylist / reorder it     -> test_denylisted_name_rejected_even_as_props_field
  * drop the props branch (regression) -> test_active_props_field_is_accepted
  * widen the column check             -> test_unknown_field_is_rejected

Mirrors tests/test_public_sharing.py — the same fake-connection + asyncio.run idiom (no
pytest-asyncio in this suite).
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.routers.symbology import _NON_SYMBOLOGY_COLUMNS, _require_field, _require_registered


class _AsyncCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeCursor(_AsyncCM):
    """Answers the gate's two possible SELECTs by dispatching on the statement text:
    the layer_fields lookup (is this an active owner-defined field?) and the
    information_schema lookup (is this a real column?)."""

    def __init__(self, conn):
        self._conn = conn
        self._row = None

    async def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
        if "platform.layer_fields" in sql:
            self._row = (1,) if self._conn.active_props else None
        elif "information_schema.columns" in sql:
            self._row = (1,) if self._conn.real_column else None
        else:  # pragma: no cover - the gate issues no other statement
            raise AssertionError(f"unexpected statement: {sql}")

    async def fetchone(self):
        return self._row


class _FakeConn:
    """active_props: a row in platform.layer_fields with active = true.
    real_column: a row in information_schema.columns for assets.<layer_id>."""

    def __init__(self, *, active_props=False, real_column=False):
        self.active_props = active_props
        self.real_column = real_column
        self.executed = []

    def cursor(self, *a, **kw):
        return _FakeCursor(self)


def _check(conn, field, layer_id="tal_b13fe969"):
    return asyncio.run(_require_field(conn, layer_id, field))


# ---- the fix: owner-defined fields are symbolizable ---------------------------------

def test_active_props_field_is_accepted():
    """A slot layer's own coded field. Before the fix this raised 422 and the demo's
    condition ramp could not be authored through the API at all."""
    conn = _FakeConn(active_props=True, real_column=False)
    _check(conn, "condition_code")


def test_props_field_never_becomes_an_identifier():
    """The props branch must pass the field as a BOUND PARAMETER, never interpolate it.
    This is what makes widening the gate safe."""
    conn = _FakeConn(active_props=True)
    _check(conn, "condition_code")
    stmt, params = conn.executed[0]
    assert "condition_code" not in stmt, "field name was interpolated into SQL"
    assert params == ("tal_b13fe969", "condition_code")


# ---- mutation checks: the gate must not have become permissive ----------------------

def test_inactive_props_field_is_rejected():
    """A RETIRED field is not a symbology candidate: no layer_fields hit, no real column."""
    conn = _FakeConn(active_props=False, real_column=False)
    with pytest.raises(HTTPException) as e:
        _check(conn, "condition_code")
    assert e.value.status_code == 422


def test_props_lookup_filters_on_active():
    """Guards the `AND active` predicate STRUCTURALLY, by asserting on the statement.

    The fake above answers from a flag, so it cannot distinguish a query that filters on
    `active` from one that does not — deleting the predicate leaves every behavioural
    test green. Caught by mutation-testing this file: only an assertion about the SQL
    itself can fail when the predicate goes missing."""
    conn = _FakeConn(active_props=True)
    _check(conn, "condition_code")
    stmt, _ = conn.executed[0]
    assert "platform.layer_fields" in stmt
    assert "AND active" in stmt, "the props lookup no longer filters out retired fields"


def test_unknown_field_is_rejected():
    conn = _FakeConn(active_props=False, real_column=False)
    with pytest.raises(HTTPException) as e:
        _check(conn, "no_such_field")
    assert e.value.status_code == 422
    assert "no field" in e.value.detail


@pytest.mark.parametrize("field", sorted(_NON_SYMBOLOGY_COLUMNS))
def test_denylisted_name_rejected_even_as_props_field(field):
    """The denylist outranks the props branch, so this gate and GET /layers/{id}/fields
    agree: a name the list refuses to offer is a name the gate refuses to accept. The
    county-boundary twin re-creates `shape_area`/`source_globalid` as owner-defined
    fields, which is exactly how a props field can carry a denylisted name."""
    conn = _FakeConn(active_props=True, real_column=True)
    with pytest.raises(HTTPException) as e:
        _check(conn, field)
    assert e.value.status_code == 422
    assert "not symbolizable" in e.value.detail


# ---- unchanged behaviour: non-props names still take the column path -----------------

def test_real_column_falls_through_to_information_schema():
    """A name with no layer_fields row (a real spine column like `notes`, or any view
    column) falls through to the information_schema path exactly as before."""
    conn = _FakeConn(active_props=False, real_column=True)
    _check(conn, "notes")
    assert any("information_schema.columns" in stmt for stmt, _ in conn.executed)


# ---- the phase-2 id-shape gate (decision-retire-builtins.md): 404 BEFORE any SQL -----

class _ExplodingConn:
    """Proves the shape gate runs before the registry SELECT: cursor() must be unreachable
    for a shape-rejected id."""

    def cursor(self, *a, **kw):  # pragma: no cover — reaching this IS the failure
        raise AssertionError("shape-rejected layer_id reached the database")


@pytest.mark.parametrize(
    "layer_id",
    [
        "signs",  # retired built-in: still a registry row on a pre-V095 box, walled anyway
        "platform.layers",  # dotted — could never be a registry row, must not name one
        "tal_B13FE969",  # uppercase hex
        "tal_b13fe9690",  # 9 hex (slot ids are exactly 8)
        "vw_0123456789ab0",  # 13 hex (view ids are exactly 12)
        "vw_0123456789a",  # 11 hex
        "",
    ],
)
def test_unshaped_layer_id_404s_before_any_sql(layer_id):
    with pytest.raises(HTTPException) as e:
        asyncio.run(_require_registered(_ExplodingConn(), layer_id))
    assert e.value.status_code == 404


class _RegistryConn(_FakeConn):
    """Answers the registry SELECT with a hit, so a well-shaped id proceeds past the gate."""

    def cursor(self, *a, **kw):
        cur = _FakeCursor(self)
        parent_execute = cur.execute

        async def execute(sql, params=None):
            if "platform.layers" in sql:
                self.executed.append((" ".join(sql.split()), params))
                cur._row = (1,)
                return
            await parent_execute(sql, params)

        cur.execute = execute
        return cur


@pytest.mark.parametrize("layer_id", ["tal_b13fe969", "vw_0123456789ab"])
def test_shaped_layer_id_proceeds_to_the_registry(layer_id):
    """Both server-generated shapes pass — a slot-only gate here would silently break
    view-layer styling (the search.py-class error, decision doc §2.3)."""
    conn = _RegistryConn()
    asyncio.run(_require_registered(conn, layer_id))
    assert any("platform.layers" in stmt for stmt, _ in conn.executed)
    assert conn.executed[0][1] == (layer_id,), "layer_id must ride as a bound parameter"
