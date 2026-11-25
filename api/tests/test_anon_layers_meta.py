"""What a signed-out visitor is told about the layers of a public map (web_maps._anon_layers_meta).

The reply must carry the layer's display field, or a visitor's popup is titled with the feature's
id while a signed-in user sees the address. Pure units against a fake connection, mutation-check
style: undo one thing and a named test goes red.

  * drop displayField from the reply                    -> test_owner_chosen_display_field_is_returned
  * drop the read-time default for an unchosen layer    -> test_unchosen_layer_gets_the_worked_out_default
  * work out a default for a layer that has a choice    -> test_default_is_asked_only_for_unchosen_layers
  * read or return a layer that is not public           -> test_non_public_layer_is_never_read_or_returned
  * let a visitor edit or manage                        -> test_visitor_reply_stays_read_only

asyncio.run(case()) idiom, as elsewhere in this suite (no pytest-asyncio).
"""
import asyncio
from datetime import datetime, timezone

from app.routers import web_maps

_WHEN = datetime(2026, 11, 25, tzinfo=timezone.utc)

# layer_id -> (display_name, display_color, renderer, geom_type, native_storage, working_srid,
#              updated_at, display_field) — the columns _anon_layers_meta selects, minus the id.
_LAYERS = {
    "tal_public_chosen":   ("Parcels", "#aa0000", None, "polygon", True, 3857, _WHEN, "fulladdress"),
    "tal_public_unchosen": ("Hydrants", "#0000aa", None, "point", True, 3857, _WHEN, None),
    "tal_private":         ("Staff notes", "#00aa00", None, "point", True, 3857, _WHEN, "secret_field"),
}
_PUBLIC = {"tal_public_chosen", "tal_public_unchosen"}


class _AsyncCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeCursor(_AsyncCM):
    def __init__(self, conn):
        self._conn = conn
        self._ids = []

    async def execute(self, sql, params=None):
        self._ids = list(params[0])
        self._conn.asked_ids.append(self._ids)

    async def fetchall(self):
        # Like the real query: only the ids it was asked for come back.
        return [(lid, *_LAYERS[lid]) for lid in self._ids if lid in _LAYERS]


class _FakeConn:
    def __init__(self):
        self.asked_ids = []

    def cursor(self):
        return _FakeCursor(self)


def _run(monkeypatch, layer_ids, derived=None):
    """Call _anon_layers_meta for a map naming `layer_ids`. Returns (reply, conn, ids the default was
    asked for)."""
    conn = _FakeConn()
    derive_calls = []

    async def fake_public(_conn, object_id, object_type="layer"):
        return object_id in _PUBLIC

    async def fake_derive(_conn, ids):
        derive_calls.append(set(ids))
        return {lid: v for lid, v in (derived or {}).items() if lid in ids}

    monkeypatch.setattr(web_maps, "public_object_allowed", fake_public)
    monkeypatch.setattr(web_maps, "derive_display_fields", fake_derive)
    definition = {"operationalLayers": [{"kind": "layer", "layerId": lid} for lid in layer_ids]}
    reply = asyncio.run(web_maps._anon_layers_meta(conn, definition))
    return reply, conn, derive_calls


def _by_id(reply):
    return {m["id"]: m for m in reply}


def test_owner_chosen_display_field_is_returned(monkeypatch):
    reply, _, _ = _run(monkeypatch, ["tal_public_chosen"])
    assert _by_id(reply)["tal_public_chosen"]["displayField"] == "fulladdress"


def test_unchosen_layer_gets_the_worked_out_default(monkeypatch):
    reply, _, _ = _run(monkeypatch, ["tal_public_unchosen"], derived={"tal_public_unchosen": "hydrantid"})
    assert _by_id(reply)["tal_public_unchosen"]["displayField"] == "hydrantid"


def test_unchosen_layer_with_no_usable_field_is_null_not_missing(monkeypatch):
    reply, _, _ = _run(monkeypatch, ["tal_public_unchosen"], derived={})
    meta = _by_id(reply)["tal_public_unchosen"]
    assert "displayField" in meta and meta["displayField"] is None


def test_default_is_asked_only_for_unchosen_layers(monkeypatch):
    reply, _, derive_calls = _run(
        monkeypatch, ["tal_public_chosen", "tal_public_unchosen"],
        derived={"tal_public_chosen": "WRONG", "tal_public_unchosen": "hydrantid"},
    )
    assert derive_calls == [{"tal_public_unchosen"}]
    assert _by_id(reply)["tal_public_chosen"]["displayField"] == "fulladdress"  # the owner's choice wins


def test_non_public_layer_is_never_read_or_returned(monkeypatch):
    reply, conn, derive_calls = _run(
        monkeypatch, ["tal_public_chosen", "tal_private"], derived={"tal_private": "secret_field"},
    )
    assert set(_by_id(reply)) == {"tal_public_chosen"}
    assert all("tal_private" not in ids for ids in conn.asked_ids)      # never queried
    assert all("tal_private" not in ids for ids in derive_calls)         # no default worked out for it
    assert "secret_field" not in str(reply)


def test_map_with_only_non_public_layers_reads_nothing(monkeypatch):
    reply, conn, derive_calls = _run(monkeypatch, ["tal_private"])
    assert reply == [] and conn.asked_ids == [] and derive_calls == []


def test_visitor_reply_stays_read_only(monkeypatch):
    reply, _, _ = _run(monkeypatch, ["tal_public_chosen", "tal_public_unchosen"], derived={})
    for meta in reply:
        assert meta["canView"] is True
        assert meta["canEdit"] is False and meta["canManage"] is False and meta["editable"] is False
