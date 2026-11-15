"""Live-DB tests for the declared field surface (`_esri_fields`) and the per-response GlobalID
contract (`_run_query`) — the two places the ESRI identity contracts actually live.

★ The regression this file guards: `globalIdFieldName` (PR #57, completed 2026-07-26).

PR #57 scoped its GlobalID fix to the LAYER DOCUMENT and stopped there. So the layer doc declared
`globalIdField: "GlobalID"`, the service advertised `supportsApplyEditsWithGlobalIds: True` twice,
every feature carried a populated GUID — and every *query response* still told the client its result
set had no GlobalID column. The contradiction survived a week and was finally detected by ArcGIS Pro
resolving it against us, out loud and unprompted, in an error dialog: "The table does not have a
Global ID Field." On a borrowed Windows box, four Drive messages deep, for a one-line conditional.

That conditional is a pure function of `out_names`. This file asserts it directly, so the next
regression costs a test run instead of a rig sitting.

Scope note: `_esri_fields` and `_run_query` are genuinely DB-backed — they read the live column
list, the V044 owner-field registry and the domain tables. So these are integration tests in the
house style: read-only, every case rolls back, skips when the DB is unreachable. They assert the
SHAPE of the contract, never a particular layer's data, so they hold on any stack.

The seam this closes: tests/test_geoservices_where_parser.py builds its `lookup` by hand. Nothing
proved that hand-built shape matched what `_esri_fields` really returns — these tests do.
"""
import asyncio
import os

import psycopg
import pytest

from app.routers.geoservices import _CHUTE_FIELD, _esri_fields, _layer_row, _run_query

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://talus:changeme@localhost:5432/talusgis")


async def _open_or_skip() -> psycopg.AsyncConnection:
    try:
        return await psycopg.AsyncConnection.connect(_DSN, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 — any connect failure -> skip
        pytest.skip(f"no DB at {_DSN}: {exc.__class__.__name__}")


async def _a_hosted_layer(conn, *, attachments: bool = False) -> str:
    """Any ready generic-write-path layer, chosen at runtime so this never pins a dev-box fixture.

    `attachments` selects one with the chute enabled — required to exercise the synthetic field.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT layer_id FROM platform.layers "
            "WHERE status = 'ready' AND layer_id LIKE 'tal\\_%' "
            + ("AND attachments_enabled " if attachments else "")
            + "ORDER BY layer_id LIMIT 1"
        )
        row = await cur.fetchone()
    if row is None:
        pytest.skip("no ready hosted layer"
                    + (" with attachments_enabled" if attachments else "") + " in this database")
    return row[0]


class _Request:
    """`_run_query` touches the request only through `_advertised_base`, which falls back to
    `str(request.base_url)` when the org has no advertised base configured."""
    base_url = "http://test.invalid/"


def run_case(coro_fn):
    """The house idiom: open, run, always roll back and close (no pytest-asyncio in this suite)."""
    async def case():
        conn = await _open_or_skip()
        try:
            return await coro_fn(conn)
        finally:
            await conn.rollback()
            await conn.close()

    return asyncio.run(case())


# ---- `_esri_fields`: the declared surface --------------------------------------------------------

def test_objectid_and_globalid_are_always_declared():
    """Both system fields, with the exact Esri types and the read-only posture.

    `esriFieldTypeGlobalID` (not ...GUID) plus length 38 is what Pro's Append / preserve-GlobalIDs
    gates on — with the wrong type Pro aborts CLIENT-side ("WARNING 000594") and never sends a
    feature. `editable: False` is the fence that stops Append mapping a source column onto it.
    """
    def _case(conn):
        async def go():
            layer_id = await _a_hosted_layer(conn)
            fields, lookup = await _esri_fields(conn, layer_id, is_anon=False)
            by_name = {f["name"]: f for f in fields}

            assert by_name["OBJECTID"]["type"] == "esriFieldTypeOID"
            assert by_name["OBJECTID"]["editable"] is False

            gid = by_name["GlobalID"]
            assert gid["type"] == "esriFieldTypeGlobalID", "esriFieldTypeGUID aborts Pro's Append"
            assert gid["editable"] is False
            assert gid["length"] == 38, "the braced-uppercase GUID wire form Esri uses"

            # The lookup shape the query serializer and _compile_where both depend on.
            assert lookup["OBJECTID"] == {"source": "objectid", "kind": "real",
                                          "esri_type": "esriFieldTypeOID"}
            assert lookup["GlobalID"]["source"] == "id"
            assert lookup["GlobalID"]["kind"] == "fid", "the kind _compile_where routes GUIDs on"
        return go()

    run_case(_case)


def test_every_field_has_the_keys_the_serializer_reads():
    """A missing key here surfaces as a KeyError mid-response, after headers are sent."""
    def _case(conn):
        async def go():
            fields, lookup = await _esri_fields(conn, await _a_hosted_layer(conn), is_anon=False)
            required = {"name", "type", "alias", "sqlType", "nullable", "editable",
                        "domain", "defaultValue"}
            for f in fields:
                assert required <= set(f), f"{f.get('name')} is missing {required - set(f)}"
            for name, entry in lookup.items():
                assert {"source", "kind", "esri_type"} <= set(entry), name
                assert name in {f["name"] for f in fields}, f"{name} in lookup but not declared"
        return go()

    run_case(_case)


def test_internal_plumbing_columns_are_never_declared():
    """`_EXCLUDED_COLUMNS` keeps geometry and internal bookkeeping off the esri surface — the raw
    `geom` column becomes the feature's geometry, never an attribute."""
    def _case(conn):
        async def go():
            fields, _ = await _esri_fields(conn, await _a_hosted_layer(conn), is_anon=False)
            names = {f["name"].lower() for f in fields}
            assert "geom" not in names
        return go()

    run_case(_case)


def test_the_chute_field_appears_only_when_asked_for():
    """The synthetic QGIS write-chute field: `source: None` because it has NO storage column, which
    is what makes writes to it dissolve in `_split_attrs` rather than hit the table."""
    def _case(conn):
        async def go():
            layer_id = await _a_hosted_layer(conn, attachments=True)

            off_fields, off_lookup = await _esri_fields(conn, layer_id, is_anon=False, chute=False)
            assert not any(f["name"].startswith(_CHUTE_FIELD) for f in off_fields)
            assert not any(k.startswith(_CHUTE_FIELD) for k in off_lookup)

            on_fields, on_lookup = await _esri_fields(conn, layer_id, is_anon=False, chute=True)
            chute = [f for f in on_fields if f["name"].startswith(_CHUTE_FIELD)]
            assert len(chute) == 1, "exactly one chute field, whatever the collision suffix"
            entry = on_lookup[chute[0]["name"]]
            assert entry["kind"] == "chute"
            assert entry["source"] is None, "a storage column here would make the writes real"

            # The chute is additive: it changes nothing else about the declared surface.
            assert ([f["name"] for f in on_fields if not f["name"].startswith(_CHUTE_FIELD)]
                    == [f["name"] for f in off_fields])
        return go()

    run_case(_case)


def test_anonymous_callers_get_a_scrubbed_surface():
    """The anon posture must match /features exactly — same `_ANON_SCRUB_COLUMNS` on both doors."""
    def _case(conn):
        async def go():
            from app.routers.layer_query import _ANON_SCRUB_COLUMNS
            layer_id = await _a_hosted_layer(conn)
            named, _ = await _esri_fields(conn, layer_id, is_anon=False)
            anon, _ = await _esri_fields(conn, layer_id, is_anon=True)

            anon_names = {f["name"] for f in anon}
            assert anon_names <= {f["name"] for f in named}, "anon must never gain a field"
            assert anon_names.isdisjoint(_ANON_SCRUB_COLUMNS)
        return go()

    run_case(_case)


# ---- ★ `_run_query`: the per-response GlobalID contract (the PR #57 regression) -------------------

def _query(conn, layer_id, meta, **params):
    return _run_query(conn, layer_id, meta, {"where": "1=1", "resultRecordCount": "1", **params},
                      False, request=_Request(), mount="/geoservices")


def test_globalid_field_name_is_declared_when_globalid_is_in_the_result():
    """★ THE regression. `outFields=*` includes GlobalID, so the response must NAME it.

    Reporting `""` here while the layer doc declares `globalIdField: "GlobalID"` is the exact
    contradiction Pro surfaced as "The table does not have a Global ID Field."
    """
    def _case(conn):
        async def go():
            layer_id = await _a_hosted_layer(conn)
            meta = await _layer_row(conn, layer_id)
            doc = await _query(conn, layer_id, meta, outFields="*")

            assert doc["objectIdFieldName"] == "OBJECTID"
            assert doc["globalIdFieldName"] == "GlobalID", (
                "the layer document declares globalIdField and we advertise "
                "supportsApplyEditsWithGlobalIds — a response that denies it is the PR #57 gap"
            )
            assert "GlobalID" in {f["name"] for f in doc["fields"]}
        return go()

    run_case(_case)


def test_globalid_field_name_is_empty_when_globalid_was_not_requested():
    """The other half — and the reason the conditional is CORRECT rather than lazy. Esri's contract
    is per-response: `""` is the honest answer when the caller did not ask for the column."""
    def _case(conn):
        async def go():
            layer_id = await _a_hosted_layer(conn)
            meta = await _layer_row(conn, layer_id)
            doc = await _query(conn, layer_id, meta, outFields="OBJECTID")

            assert doc["globalIdFieldName"] == ""
            assert {f["name"] for f in doc["fields"]} == {"OBJECTID"}
        return go()

    run_case(_case)


def test_globalid_field_name_survives_the_callers_spelling():
    """`out_names` carries DECLARED casing, so the literal compare holds however outFields was
    spelled — the detail that keeps the fix from being case-fragile."""
    def _case(conn):
        async def go():
            layer_id = await _a_hosted_layer(conn)
            meta = await _layer_row(conn, layer_id)
            for spelling in ("GlobalID", "globalid", "GLOBALID", "OBJECTID,globalid"):
                doc = await _query(conn, layer_id, meta, outFields=spelling)
                assert doc["globalIdFieldName"] == "GlobalID", f"outFields={spelling!r}"
        return go()

    run_case(_case)


# ---- the case-clash regression (decision-esri-field-name-clash.md §5.1) --------------------------

def test_no_two_declared_names_are_equal_under_lower():
    """★ The test that would have caught the original bug in seconds, on every ready layer at once.

    A real ArcGIS FeatureServer CANNOT serve two fields differing only by case — gdb names are
    case-insensitive. We did: `_esri_fields` seeds hardcoded `OBJECTID`/`GlobalID` and then appends
    every V044 registry field with no case-folded dedupe, so a layer whose ESRI export carried an
    `objectid` props key was advertised with both. OGR then refuses the duplicate, `QgsOfflineEditing`
    aborts the feature copy AND REPORTS SUCCESS, and the crew carries an empty GeoPackage into the
    field. Eight layers across two stacks were in that state before V087.

    Deliberately a SWEEP, not a fixture: the defect arrived through bulk ingest, so the useful
    question is "does any layer on this stack advertise a clash", and the answer must be no on every
    stack, forever. Runs with and without the chute, since that field is appended last.
    """
    def _case(conn):
        async def go():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT layer_id FROM platform.layers "
                    "WHERE status = 'ready' AND layer_id LIKE 'tal\\_%' ORDER BY layer_id"
                )
                layer_ids = [r[0] for r in await cur.fetchall()]
            if not layer_ids:
                pytest.skip("no ready hosted layers in this database")

            for layer_id in layer_ids:
                for chute in (False, True):
                    fields, lookup = await _esri_fields(conn, layer_id, is_anon=False, chute=chute)
                    names = [f["name"] for f in fields]
                    folded = [n.lower() for n in names]
                    dupes = {n for n in folded if folded.count(n) > 1}
                    assert not dupes, f"{layer_id} (chute={chute}) advertises case-equal names: {dupes} in {names}"
                    # The serializer indexes `lookup` by the DECLARED name — a name with no entry is a
                    # KeyError mid-response, after headers are sent.
                    assert set(names) == set(lookup), f"{layer_id}: fields[] and lookup disagree"
        return go()

    run_case(_case)


def test_no_reserved_key_survives_in_the_registry():
    """The DB-side half of V087: the repair ran, so no layer still carries a clashing registry key.
    Independent of `_esri_fields` — this asserts the DATA is fixed, not that the view hides it (§3.1:
    fix the data, not the view)."""
    def _case(conn):
        async def go():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT layer_id, field_key FROM platform.layer_fields "
                    "WHERE lower(field_key) IN ('objectid', 'globalid')"
                )
                left = await cur.fetchall()
            assert left == [], f"V087 did not repair: {left}"
        return go()

    run_case(_case)
