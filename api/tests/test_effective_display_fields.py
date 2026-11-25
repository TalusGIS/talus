"""Which field a layer's features are titled with (display_field.effective_display_fields).

An owner's starred field is checked when it is set. It is checked again here on every read, because
it can go stale afterwards, and because a signed-out visitor is never sent the editor columns. Pure
units against a fake connection, mutation-check style: undo one thing and a named test goes red.

  * keep a star without checking the layer still has the field -> test_retired_owner_field_gives_the_default
                                                                 test_column_dropped_from_a_view_gives_the_default
  * ignore `withheld`                                           -> test_withheld_column_gives_the_default
  * apply `withheld` to everyone                                -> test_withheld_applies_only_when_passed
  * work out a default for a layer whose star stands            -> test_live_star_is_kept_and_no_default_is_asked
  * count the geometry column as a field                        -> test_geometry_column_is_never_a_field
  * count a retired owner field as a field                      -> test_only_active_owner_fields_are_asked_for

asyncio.run(case()) idiom, as elsewhere in this suite (no pytest-asyncio).
"""
import asyncio

from app import display_field

# What the fake database holds. Real columns per table or view, and ACTIVE owner fields per layer
# (a retired owner field is simply not in the second map, as the real query's `AND active` leaves it out).
_COLUMNS = {
    "tal_0a0a0a0a": ["id", "notes", "created_by", "updated_by"],
    "vw_parcels": ["id", "fulladdress", "owner_name"],
}
_ACTIVE_FIELDS = {"tal_0a0a0a0a": ["street", "status"]}


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self._conn.sql.append(sql)
        ids = list(params[0])
        if "information_schema.columns" in sql:
            self._rows = [(t, c) for t in ids for c in _COLUMNS.get(t, [])]
        elif "platform.layer_fields" in sql:
            self._rows = [(lid, f) for lid in ids for f in _ACTIVE_FIELDS.get(lid, [])]
        else:  # pragma: no cover - a query this test does not know is a test bug
            raise AssertionError(f"unexpected query: {sql}")

    async def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self):
        self.sql = []

    def cursor(self):
        return _FakeCursor(self)


def _run(monkeypatch, chosen, derived=None, **kw):
    """Returns (result, conn, the layer sets a default was asked for)."""
    conn = _FakeConn()
    derive_calls = []

    async def fake_derive(_conn, ids):
        derive_calls.append(set(ids))
        return {lid: v for lid, v in (derived or {}).items() if lid in ids}

    monkeypatch.setattr(display_field, "derive_display_fields", fake_derive)
    result = asyncio.run(display_field.effective_display_fields(conn, chosen, **kw))
    return result, conn, derive_calls


def test_live_star_is_kept_and_no_default_is_asked(monkeypatch):
    result, _, derive_calls = _run(
        monkeypatch, {"vw_parcels": "fulladdress", "tal_0a0a0a0a": "street"},
        derived={"vw_parcels": "WRONG", "tal_0a0a0a0a": "WRONG"},
    )
    assert result == {"vw_parcels": "fulladdress", "tal_0a0a0a0a": "street"}
    assert derive_calls == [set()]  # nothing left to work out


def test_retired_owner_field_gives_the_default(monkeypatch):
    # "old_field" was starred while it was active; it has since been retired.
    result, _, derive_calls = _run(
        monkeypatch, {"tal_0a0a0a0a": "old_field"}, derived={"tal_0a0a0a0a": "street"},
    )
    assert result == {"tal_0a0a0a0a": "street"}
    assert derive_calls == [{"tal_0a0a0a0a"}]


def test_column_dropped_from_a_view_gives_the_default(monkeypatch):
    result, _, _ = _run(monkeypatch, {"vw_parcels": "site_address"}, derived={"vw_parcels": "fulladdress"})
    assert result == {"vw_parcels": "fulladdress"}


def test_withheld_column_gives_the_default(monkeypatch):
    result, _, _ = _run(
        monkeypatch, {"tal_0a0a0a0a": "created_by"}, derived={"tal_0a0a0a0a": "street"},
        withheld=frozenset({"created_by", "updated_by"}),
    )
    assert result == {"tal_0a0a0a0a": "street"}


def test_withheld_applies_only_when_passed(monkeypatch):
    # A signed-in caller is sent the editor columns, so the owner's star on one stands.
    result, _, _ = _run(monkeypatch, {"tal_0a0a0a0a": "created_by"}, derived={"tal_0a0a0a0a": "street"})
    assert result == {"tal_0a0a0a0a": "created_by"}


def test_unstarred_layer_gets_the_default_and_no_field_lookup(monkeypatch):
    result, conn, derive_calls = _run(monkeypatch, {"vw_parcels": None}, derived={"vw_parcels": "fulladdress"})
    assert result == {"vw_parcels": "fulladdress"}
    assert conn.sql == []  # no star to check, so the layer's fields are never read
    assert derive_calls == [{"vw_parcels"}]


def test_layer_with_nothing_usable_is_absent(monkeypatch):
    result, _, _ = _run(monkeypatch, {"tal_0a0a0a0a": "old_field", "vw_parcels": None}, derived={})
    assert result == {}


def test_a_star_is_checked_against_its_own_layer(monkeypatch):
    # "fulladdress" is a field of the view, not of the slot: a star must not pass on another layer's field.
    result, _, _ = _run(
        monkeypatch, {"tal_0a0a0a0a": "fulladdress", "vw_parcels": "fulladdress"},
        derived={"tal_0a0a0a0a": "street"},
    )
    assert result == {"tal_0a0a0a0a": "street", "vw_parcels": "fulladdress"}


def test_geometry_column_is_never_a_field(monkeypatch):
    _, conn, _ = _run(monkeypatch, {"vw_parcels": "geom"}, derived={})
    columns_sql = next(q for q in conn.sql if "information_schema.columns" in q)
    assert "column_name <> 'geom'" in columns_sql


def test_only_active_owner_fields_are_asked_for(monkeypatch):
    _, conn, _ = _run(monkeypatch, {"tal_0a0a0a0a": "street"}, derived={})
    fields_sql = next(q for q in conn.sql if "platform.layer_fields" in q)
    assert "AND active" in fields_sql
