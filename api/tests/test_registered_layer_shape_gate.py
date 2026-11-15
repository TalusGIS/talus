"""The shared registry proof's id-shape gate (decision-retire-builtins.md phase 2).

`layer_query._require_registered` is the registry proof the generic read door AND its
importers (tiles_dyn, layer_views, geoservices) share; `routers/attachments` carries its
own copy. Since the pilot built-ins retired, `platform.layers` holds only the two
server-generated id shapes (`tal_<8hex>` slot / `vw_<12hex>` view), so both proofs now 404
anything else BEFORE the registry SELECT — the same belt-and-braces as symbology's gate
(test_symbology_field_gate.py, whose fake-connection + asyncio.run idiom this mirrors).

`search._registered_layer` is the same proof with SKIP semantics (returns False instead of
raising — the fan-out silently skips a layer it can't serve), tested in its own section.
Its shape gate also proves search's dispatch: an id that passes and fails _SLOT_RE IS a view.

Mutation checks: drop either regex, reorder the gate after the SELECT, or widen a pattern,
and a named test here goes red.
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.routers.attachments import _require_registered as attachments_gate
from app.routers.layer_query import _require_registered as layer_query_gate
from app.routers.search import _registered_layer as search_gate

_GATES = [
    pytest.param(layer_query_gate, id="layer_query"),
    pytest.param(attachments_gate, id="attachments"),
]


class _AsyncCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ExplodingConn:
    """Proves the shape gate runs before the registry SELECT: cursor() must be unreachable
    for a shape-rejected id."""

    def cursor(self, *a, **kw):  # pragma: no cover — reaching this IS the failure
        raise AssertionError("shape-rejected layer_id reached the database")


class _RegistryConn:
    """Answers the registry SELECT (hit or miss) and records the statement + params."""

    def __init__(self, *, registered=True):
        self.registered = registered
        self.executed = []

    def cursor(self, *a, **kw):
        conn = self

        class _Cur(_AsyncCM):
            async def execute(self, sql, params=None):
                assert "platform.layers" in sql, f"unexpected statement: {sql}"
                conn.executed.append((" ".join(sql.split()), params))

            async def fetchone(self):
                return (1,) if conn.registered else None

        return _Cur()


@pytest.mark.parametrize("gate", _GATES)
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
def test_unshaped_layer_id_404s_before_any_sql(gate, layer_id):
    with pytest.raises(HTTPException) as e:
        asyncio.run(gate(_ExplodingConn(), layer_id))
    assert e.value.status_code == 404


@pytest.mark.parametrize("gate", _GATES)
@pytest.mark.parametrize("layer_id", ["tal_b13fe969", "vw_0123456789ab"])
def test_shaped_layer_id_proceeds_to_the_registry(gate, layer_id):
    """Both server-generated shapes pass — a slot-only gate would break view-layer reads
    and attachments (the search.py-class error, decision doc §2.3)."""
    conn = _RegistryConn(registered=True)
    asyncio.run(gate(conn, layer_id))
    assert conn.executed, "the registry SELECT never ran"
    assert conn.executed[0][1] == (layer_id,), "layer_id must ride as a bound parameter"


@pytest.mark.parametrize("gate", _GATES)
def test_shaped_but_unregistered_still_404s(gate):
    """The second layer stays intact: a well-shaped id that is not a registry row is 404,
    so the shape gate never becomes the ONLY check."""
    conn = _RegistryConn(registered=False)
    with pytest.raises(HTTPException) as e:
        asyncio.run(gate(conn, "tal_00000000"))
    assert e.value.status_code == 404


# ---- search._registered_layer: the same gate with skip semantics ---------------------

@pytest.mark.parametrize(
    "layer_id",
    ["signs", "platform.layers", "tal_B13FE969", "tal_b13fe9690", "vw_0123456789ab0", ""],
)
def test_search_skips_unshaped_ids_before_any_sql(layer_id):
    """False (skip), never a raise, and never a registry SELECT — the fan-out's silent-skip
    contract holds even for a shape-rejected id."""
    assert asyncio.run(search_gate(_ExplodingConn(), layer_id)) is False


@pytest.mark.parametrize("layer_id", ["tal_b13fe969", "vw_0123456789ab"])
def test_search_admits_both_shapes_to_the_registry(layer_id):
    conn = _RegistryConn(registered=True)
    assert asyncio.run(search_gate(conn, layer_id)) is True
    assert conn.executed[0][1] == (layer_id,), "layer_id must ride as a bound parameter"


def test_search_shaped_but_unregistered_is_skipped():
    conn = _RegistryConn(registered=False)
    assert asyncio.run(search_gate(conn, "vw_000000000000")) is False
