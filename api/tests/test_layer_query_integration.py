"""Live-DB integration tests for the generic layer query route (Talus 15).

Targets the SQL/serialization contract of `GET /layers/{id}/features`: full-count vs page
(`numberMatched`/`numberReturned`), filter/sort correctness, geometry + resolved editor
names, and that every field/op/layer is validated (422/404) before it can touch SQL. Authz
(`view_allowed`) is monkeypatched to allow — the 403/cross-layer path is verified live, not
here; this isolates the query behavior.

Fixture: the hosted Signs demo twin, resolved by display name (the pilot built-ins retired —
decision-retire-builtins.md phase 2 — so `condition_code` is an owner-defined props field:
filter specs still use the bare name via the g2 virtual-column grammar, while the SQL side
reads `props ->>` and feature properties carry it inside the `props` blob).

Read-only: every case rolls back and closes. Skips if the DB is unreachable or the twin is
absent. Override the DSN with TEST_DATABASE_URL if the default doesn't match your stack.
"""
import asyncio
import json
import os

import psycopg
import pytest
from fastapi import HTTPException

from app.models.user import User
from app.routers import layer_query

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://talus:changeme@localhost:5432/talusgis")
# The API does NOT run as the owner. `_DSN` connects as `talus` (superuser/owner), which sees rows
# in catalogs the app role cannot — some information_schema views filter on role membership, so a
# thing visible to this test can be invisible in production. Anything asserting catalog
# INTROSPECTION must run as the app role or it cannot fail. (The lesson's writeup lives in
# app/geom_meta.py; the fk_domains module that learned it retired with the built-ins.)
_APP_ROLE = os.environ.get("TEST_APP_ROLE", "talus_app")


async def _open_or_skip() -> psycopg.AsyncConnection:
    try:
        return await psycopg.AsyncConnection.connect(_DSN, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 — any connect failure -> skip
        pytest.skip(f"no DB at {_DSN}: {exc.__class__.__name__}")


async def _twin_or_skip(conn: psycopg.AsyncConnection) -> str:
    """The Signs demo twin's box-specific tal_ id (display-name resolution — the seed's own
    convention). Skips when absent: these cases need its 12k-row corpus."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT layer_id FROM platform.layers WHERE display_name = 'Signs' "
            "AND layer_id LIKE %s ORDER BY created_at LIMIT 1", (r"tal\_%",))
        row = await cur.fetchone()
    if row is None:
        pytest.skip("no 'Signs' demo twin on this box (a development seed this release does not include)")
    return row[0]


async def _a_user(conn: psycopg.AsyncConnection) -> User:
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM platform.users LIMIT 1")
        uid = (await cur.fetchone())[0]
    return User(id=uid, username="t", email="t@t.invalid", display_name="t")


async def _as_app_role(conn: psycopg.AsyncConnection) -> None:
    """Run the rest of this connection as the API's own DB role, so catalog visibility matches
    production. Skips (rather than passes) when the role is absent, so a stack without it never
    reports a false green."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (_APP_ROLE,))
        if await cur.fetchone() is None:
            pytest.skip(f"role {_APP_ROLE} not present; set TEST_APP_ROLE")
        await cur.execute(f'SET ROLE "{_APP_ROLE}"')


async def _scalar(conn: psycopg.AsyncConnection, sql: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(sql)
        return (await cur.fetchone())[0]


def _allow_view(monkeypatch) -> None:
    async def _allow(user, layer_id):  # noqa: ANN001
        return True

    monkeypatch.setattr(layer_query, "view_allowed", _allow)


def test_query_returns_full_count_and_page(monkeypatch):
    _allow_view(monkeypatch)

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            user = await _a_user(conn)
            twin = await _twin_or_skip(conn)
            total = await _scalar(conn, f"SELECT count(*) FROM assets.{twin}")
            res = await layer_query.query_features(
                layer_id=twin, bbox=None, filters=[], sort=None,
                feature_ids=[], limit=2, offset=0, conn=conn, principal=user,
            )
            assert res["type"] == "FeatureCollection"
            assert res["numberMatched"] == total          # page-independent total
            assert res["numberReturned"] == len(res["features"]) == 2
            f = res["features"][0]
            assert f["type"] == "Feature" and isinstance(f["id"], str)
            assert f["geometry"]["type"] == "Point"        # geometry surfaced
            props = f["properties"]
            assert "condition_code" in props.get("props", {})  # owner field inside the props blob
            assert "updated_by_name" in props              # resolved editor name (JOIN users)
            assert "geom" not in props                     # geom became geometry, not a prop
        finally:
            await conn.rollback()
            await conn.close()

    asyncio.run(case())


def test_filter_in_matches_direct_count(monkeypatch):
    _allow_view(monkeypatch)

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            user = await _a_user(conn)
            twin = await _twin_or_skip(conn)
            expected = await _scalar(
                conn,
                f"SELECT count(*) FROM assets.{twin} WHERE props ->> 'condition_code' IN ('poor','replace')",
            )
            res = await layer_query.query_features(
                layer_id=twin, bbox=None, filters=["condition_code:in:poor,replace"],
                sort=None, feature_ids=[], limit=5, offset=0, conn=conn, principal=user,
            )
            assert res["numberMatched"] == expected
            assert all(
                f["properties"]["props"]["condition_code"] in ("poor", "replace")
                for f in res["features"]
            )
        finally:
            await conn.rollback()
            await conn.close()

    asyncio.run(case())


def test_sort_desc_orders_page(monkeypatch):
    _allow_view(monkeypatch)

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            user = await _a_user(conn)
            twin = await _twin_or_skip(conn)
            res = await layer_query.query_features(
                layer_id=twin, bbox=None, filters=[], sort="updated_at:desc",
                feature_ids=[], limit=5, offset=0, conn=conn, principal=user,
            )
            stamps = [f["properties"]["updated_at"] for f in res["features"]]
            assert stamps == sorted(stamps, reverse=True)
        finally:
            await conn.rollback()
            await conn.close()

    asyncio.run(case())


def test_bbox_narrows_match(monkeypatch):
    _allow_view(monkeypatch)

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            user = await _a_user(conn)
            twin = await _twin_or_skip(conn)
            total = await _scalar(conn, f"SELECT count(*) FROM assets.{twin}")
            res = await layer_query.query_features(
                layer_id=twin, bbox="-82.54,27.33,-82.52,27.35", filters=[],
                sort=None, feature_ids=[], limit=1, offset=0, conn=conn, principal=user,
            )
            assert 0 < res["numberMatched"] < total
        finally:
            await conn.rollback()
            await conn.close()

    asyncio.run(case())


def test_export_csv_raw_columns_and_full_count(monkeypatch):
    """CSV carries the RAW columns (codes; owner fields ride in the `props` column) — the
    `<col>_label` join retired with the built-ins' FK domains (decision-retire-builtins.md)."""
    _allow_view(monkeypatch)

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            user = await _a_user(conn)
            twin = await _twin_or_skip(conn)
            expected = await _scalar(
                conn,
                f"SELECT count(*) FROM assets.{twin} WHERE props ->> 'condition_code' IN ('poor','replace')",
            )
            await _as_app_role(conn)  # match production's catalog visibility for the column list
            res = await layer_query.export_features(
                layer_id=twin, format="csv", bbox=None,
                filters=["condition_code:in:poor,replace"], sort=None,
                feature_ids=[], srid=None, conn=conn, principal=user,
            )
            assert res.media_type == "text/csv"
            assert "attachment" in res.headers["content-disposition"]
            import csv as _csv
            import io as _io
            reader = list(_csv.reader(_io.StringIO(res.body.decode())))
            header = reader[0]
            assert "props" in header and "notes" in header
            assert not any(h.endswith("_label") for h in header)  # the label join is gone
            assert "geometry_wkt" in header and "updated_by_name" in header
            assert len(reader) - 1 == expected   # data rows == full filtered set (no paging)
        finally:
            await conn.rollback()
            await conn.close()

    asyncio.run(case())


def test_export_geojson_selected_ids_only(monkeypatch):
    _allow_view(monkeypatch)

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            user = await _a_user(conn)
            twin = await _twin_or_skip(conn)
            page = await layer_query.query_features(
                layer_id=twin, bbox=None, filters=[], sort=None,
                feature_ids=[], limit=1, offset=0, conn=conn, principal=user,
            )
            target = page["features"][0]["id"]
            res = await layer_query.export_features(
                layer_id=twin, format="geojson", bbox=None, filters=[], sort=None,
                feature_ids=[target], srid=None, conn=conn, principal=user,
            )
            assert res.media_type == "application/geo+json"
            d = json.loads(res.body)
            assert d["type"] == "FeatureCollection"
            assert len(d["features"]) == 1 and d["features"][0]["id"] == target
        finally:
            await conn.rollback()
            await conn.close()

    asyncio.run(case())


def test_features_selected_ids_page(monkeypatch):
    """decision-select-features.md F5: repeatable id on /features pages ONLY the selected rows —
    numberMatched is the selected-set size and paging stays honest over it."""
    _allow_view(monkeypatch)

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            user = await _a_user(conn)
            twin = await _twin_or_skip(conn)
            page = await layer_query.query_features(
                layer_id=twin, bbox=None, filters=[], sort=None,
                feature_ids=[], limit=3, offset=0, conn=conn, principal=user,
            )
            ids = [f["id"] for f in page["features"]]
            assert len(ids) == 3

            sel = await layer_query.query_features(
                layer_id=twin, bbox=None, filters=[], sort=None,
                feature_ids=ids, limit=2, offset=0, conn=conn, principal=user,
            )
            assert sel["numberMatched"] == 3          # the selected set, not the layer
            assert sel["numberReturned"] == 2         # paged over it
            assert {f["id"] for f in sel["features"]} <= set(ids)

            rest = await layer_query.query_features(
                layer_id=twin, bbox=None, filters=[], sort=None,
                feature_ids=ids, limit=2, offset=2, conn=conn, principal=user,
            )
            assert rest["numberReturned"] == 1        # the remainder page
            assert {f["id"] for f in rest["features"]} <= set(ids)
        finally:
            await conn.rollback()
            await conn.close()

    asyncio.run(case())


def test_extent_selected_ids_narrows_and_missing_ids_null(monkeypatch):
    """decision-zoom-to-selected.md F1: repeatable id on /extent bounds the bbox to just those
    rows (same _where ids branch as export-selected); all-ids-missing -> bbox null."""
    _allow_view(monkeypatch)

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            user = await _a_user(conn)
            twin = await _twin_or_skip(conn)
            full = await layer_query.layer_extent(
                layer_id=twin, feature_ids=[], conn=conn, principal=user,
            )
            assert full["bbox"] is not None  # the twin is seeded/spatial

            page = await layer_query.query_features(
                layer_id=twin, bbox=None, filters=[], sort=None,
                feature_ids=[], limit=1, offset=0, conn=conn, principal=user,
            )
            target = page["features"][0]["id"]
            one = await layer_query.layer_extent(
                layer_id=twin, feature_ids=[target], conn=conn, principal=user,
            )
            assert one["bbox"] is not None
            fx0, fy0, fx1, fy1 = full["bbox"]
            x0, y0, x1, y1 = one["bbox"]
            assert fx0 <= x0 <= x1 <= fx1 and fy0 <= y0 <= y1 <= fy1  # contained in the full extent

            missing = await layer_query.layer_extent(
                layer_id=twin,
                feature_ids=["00000000-0000-0000-0000-000000000000"],
                conn=conn, principal=user,
            )
            assert missing["bbox"] is None  # all ids absent -> null, client no-ops
        finally:
            await conn.rollback()
            await conn.close()

    asyncio.run(case())


def test_columns_returns_full_schema_with_custom_domains(monkeypatch):
    _allow_view(monkeypatch)

    # The A-1 canEdit flag rides this route via edit_allowed -> OpenFGA; stub it like view_allowed
    # (authz decisions are verified live, not here).
    async def _no_edit(conn, claims, principal, layer_id):  # noqa: ANN001
        return False, None

    monkeypatch.setattr(layer_query, "edit_allowed", _no_edit)

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            user = await _a_user(conn)
            twin = await _twin_or_skip(conn)
            await _as_app_role(conn)  # column introspection must match production visibility
            res = await layer_query.layer_columns(
                layer_id=twin, conn=conn, claims=None, principal=user,
            )
            names = {c["name"] for c in res["columns"]}
            assert res["layer_id"] == twin
            assert "geom" not in names                         # geometry is not a table column here
            assert {"id", "condition_code", "updated_at", "created_by"} <= names  # cols + owner fields
            by_name = {c["name"]: c for c in res["columns"]}
            assert by_name["condition_code"].get("props") is True         # an owner-defined field
            assert by_name["condition_code"].get("customDomain")          # carrying its V046 domain
            assert not by_name["updated_at"].get("customDomain")
        finally:
            await conn.rollback()
            await conn.close()

    asyncio.run(case())


def test_unknown_field_op_and_layer_rejected(monkeypatch):
    _allow_view(monkeypatch)

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            user = await _a_user(conn)
            base = dict(bbox=None, filters=[], sort=None, feature_ids=[], limit=5, offset=0, conn=conn, principal=user)

            twin = await _twin_or_skip(conn)
            with pytest.raises(HTTPException) as e1:  # field not on the table
                await layer_query.query_features(
                    layer_id=twin, **{**base, "filters": ["evil;drop:in:x"]}
                )
            assert e1.value.status_code == 422

            with pytest.raises(HTTPException) as e2:  # op not whitelisted
                await layer_query.query_features(
                    layer_id=twin, **{**base, "filters": ["condition_code:bogus:x"]}
                )
            assert e2.value.status_code == 422

            with pytest.raises(HTTPException) as e3:  # unknown sort field
                await layer_query.query_features(
                    layer_id=twin, **{**base, "sort": "nope:asc"}
                )
            assert e3.value.status_code == 422

            with pytest.raises(HTTPException) as e4:  # unshaped id — 404 at the shape gate
                await layer_query.query_features(layer_id="does_not_exist", **base)
            assert e4.value.status_code == 404

            with pytest.raises(HTTPException) as e5:  # well-shaped but unregistered — still 404
                await layer_query.query_features(layer_id="tal_00000000", **base)
            assert e5.value.status_code == 404
        finally:
            await conn.rollback()
            await conn.close()

    asyncio.run(case())


def test_columns_route_is_optional_auth_by_construction():
    """The claims dependency must be `_optional_claims` — None when there is no Authorization
    header, delegating to `_validated_claims` (and so still 401ing) when there is one. Asserted on
    the WIRING because the regression was a wiring change made in passing by an unrelated feature,
    and nothing about that feature's own tests could have noticed."""
    import inspect

    from app.auth import _optional_claims, _validated_claims

    dep = inspect.signature(layer_query.layer_columns).parameters["claims"].default.dependency
    assert dep is _optional_claims, f"claims dependency is {dep!r}"
    assert dep is not _validated_claims, "a hard 401 gate makes this route authenticated-only"


def test_columns_canedit_is_false_without_claims_and_never_consults_roles(monkeypatch):
    """Trap A: `_has_edit_role(None)` returns True (a stub-mode convenience), so handing
    `edit_allowed` a None claims dict reports edit capability for a caller whose roles say
    otherwise. Reachable for a PAT — a real principal resolved from HTTP Basic, carrying no Bearer.
    The route must not even ask."""
    _allow_view(monkeypatch)
    called = []

    async def _spy(conn, claims, principal, layer_id):  # noqa: ANN001
        called.append(claims)
        return True, "ok"  # would be a spoofed True if it were ever reached

    monkeypatch.setattr(layer_query, "edit_allowed", _spy)

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            user = await _a_user(conn)
            twin = await _twin_or_skip(conn)
            await _as_app_role(conn)
            res = await layer_query.layer_columns(
                layer_id=twin, conn=conn, claims=None, principal=user,
            )
            assert called == [], "edit_allowed was consulted with claims=None"
            assert res["canEdit"] is False
        finally:
            await conn.rollback()
            await conn.close()

    asyncio.run(case())


def test_columns_canedit_still_uses_claims_when_they_exist(monkeypatch):
    """The other side of the guard: a real Bearer must still get a real edit decision, or the fix
    would have been 'always false', which passes the test above and breaks the product."""
    _allow_view(monkeypatch)
    called = []

    async def _spy(conn, claims, principal, layer_id):  # noqa: ANN001
        called.append(claims)
        return True, "ok"

    monkeypatch.setattr(layer_query, "edit_allowed", _spy)
    claims = {"realm_access": {"roles": ["talus-editor"]}}

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            user = await _a_user(conn)
            twin = await _twin_or_skip(conn)
            await _as_app_role(conn)
            res = await layer_query.layer_columns(
                layer_id=twin, conn=conn, claims=claims, principal=user,
            )
            assert called == [claims]
            assert res["canEdit"] is True
        finally:
            await conn.rollback()
            await conn.close()

    asyncio.run(case())
