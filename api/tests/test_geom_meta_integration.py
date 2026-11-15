"""Live-DB integration tests for `app.geom_meta` — the geometry type + storage SRID behind the
Open Data fact strip (handoff-auth-opendata-4 §3.2/§6.6).

Read-only: every case rolls back and closes. Skips if the DB is unreachable.

These run as the API's OWN DB ROLE, not the owner. That is not ceremony: `/columns` reported
`domain: null` for every coded column in production for months while the tests passed, because the
tests connect as `talus` (the owner) and the old introspection filtered on
`pg_has_role(tblowner,'USAGE')` — see app/fk_domains. Catalog introspection asserted as the owner
CANNOT FAIL, so it proves nothing. `geom_meta` reads pg_catalog and is expected to be role-blind;
this is what makes that an assertion rather than a hope.
"""
import asyncio
import os

import psycopg
import pytest

from app.geom_meta import geom_meta

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://talus:changeme@localhost:5432/talusgis")
_APP_ROLE = os.environ.get("TEST_APP_ROLE", "talus_app")


async def _twins_or_skip(conn) -> tuple[str, str]:
    """(Signs twin, County Boundary twin) tal_ ids by display name — the demo layers the
    fixtures ride since the pilot built-ins retired (decision-retire-builtins.md phase 2).
    Skips when absent."""
    ids = []
    for name in ("Signs", "County Boundary"):
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT layer_id FROM platform.layers WHERE display_name = %s "
                "AND layer_id LIKE %s ORDER BY created_at LIMIT 1", (name, r"tal\_%"))
            row = await cur.fetchone()
        if row is None:
            pytest.skip(f"no {name!r} demo twin on this box (a development seed this release does not include)")
        ids.append(row[0])
    return ids[0], ids[1]


async def _open_or_skip() -> psycopg.AsyncConnection:
    try:
        return await psycopg.AsyncConnection.connect(_DSN, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 — any connect failure -> skip
        pytest.skip(f"no DB at {_DSN}: {exc.__class__.__name__}")


async def _as_app_role(conn: psycopg.AsyncConnection) -> None:
    """Skips (rather than passes) when the role is absent, so a stack without it never reports a
    false green."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (_APP_ROLE,))
        if await cur.fetchone() is None:
            pytest.skip(f"role {_APP_ROLE} not present; set TEST_APP_ROLE")
        await cur.execute(f'SET ROLE "{_APP_ROLE}"')


def _run(case) -> None:
    async def wrapper() -> None:
        conn = await _open_or_skip()
        try:
            await _as_app_role(conn)
            await case(conn)
        finally:
            await conn.rollback()
            await conn.close()

    asyncio.run(wrapper())


def test_geom_meta_visible_to_the_app_role():
    """The whole point: the API role must see the typmod, not just the owner."""

    async def case(conn):
        signs_twin, county_twin = await _twins_or_skip(conn)
        got = await geom_meta(conn, [signs_twin, county_twin])
        assert got.get(signs_twin) == ("Point", 4326)
        assert got.get(county_twin) == ("MultiPolygon", 4326)

    _run(case)


def test_geom_meta_omits_tables_with_no_geometry():
    """A geometry-less assets relation (a hosted table) is ABSENT from the result — that absence
    is the signal the caller's has_geom set reads. Probed with a throwaway no-geom table created
    inside this rolled-back transaction, so the case needs no seeded fixture."""

    async def case(conn):
        signs_twin, _ = await _twins_or_skip(conn)
        async with conn.cursor() as cur:
            # The probe table needs owner privileges; geom_meta itself then runs back as the
            # app role (pg_catalog reads are role-blind — the property under test).
            await cur.execute("RESET ROLE")
            await cur.execute("CREATE TABLE assets._gm_probe_nogeom (id integer)")
            await cur.execute(f'SET ROLE "{_APP_ROLE}"')
        got = await geom_meta(conn, ["_gm_probe_nogeom", signs_twin])
        assert "_gm_probe_nogeom" not in got
        assert signs_twin in got  # the control: the query DID run and DID match something

    _run(case)


def test_geom_meta_returns_postgis_spelling_not_a_ui_label():
    """Multi* is preserved. Collapsing MultiPolygon -> 'Polygon' is presentation and belongs to the
    client; the API must not pre-chew it, or a caller that needs the real type cannot get it."""

    async def case(conn):
        _, county_twin = await _twins_or_skip(conn)
        got = await geom_meta(conn, [county_twin])
        gtype, _ = got[county_twin]
        assert gtype == "MultiPolygon"
        assert gtype not in ("Polygon", "polygon")

    _run(case)


def test_geom_meta_empty_input_makes_no_query():
    async def case(conn):
        assert await geom_meta(conn, []) == {}

    _run(case)


def test_geom_meta_unknown_layer_is_absent_not_an_error():
    """A layer_id that names no table must not raise — the Open Data list would 500 for the whole
    catalog because one row was mid-delete."""

    async def case(conn):
        assert await geom_meta(conn, ["no_such_layer_xyz"]) == {}

    _run(case)
