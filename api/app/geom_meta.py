"""Geometry type + storage SRID for a layer's `geom` column.

The Open Data dataset page shows a fact strip — feature count, geometry type, CRS, updated,
license — so a visitor can decide between Shapefile and GeoJSON, or check a projection, WITHOUT
downloading first (handoff-auth-opendata-4 §3.2). Feature count, updated and license were already
on `OpenDataset`; geometry type and SRID were not, and they are the two the strip exists for.

WHY pg_catalog
--------------
Read straight off the column's typmod rather than through `geometry_columns` or
`information_schema`. Unlike the coded-domain case (`information_schema.constraint_column_usage` filters on
`pg_has_role(tblowner,'USAGE')` against the REFERENCED table's owner and silently returned zero rows
for the API role — the module that documented it, `fk_domains.py`, retired with the pilot built-ins;
`docs/handoff-auth-opendata-4.md` keeps the long-form record), both of those views ARE
visible to `talus_app` for this query — verified, 10/10 layers via all three sources. So this is not
a repeat of that bug.

pg_catalog is chosen anyway because it depends on nothing: `geometry_columns` ends in
`has_table_privilege(c.oid,'SELECT')` and `information_schema.columns` is likewise privilege-scoped,
so both couple this metadata to a grant that could be narrowed later, in a code path whose failure
mode is silence (a missing cell, not an error). It also keeps one introspection idiom in the
codebase instead of three.

HONESTY OVER COVERAGE
---------------------
An unconstrained `geometry` column has `atttypmod = -1`, for which PostGIS reports type 'Geometry'
and SRID 0 — i.e. "unknown", not "4326". Both are returned as None so the caller omits the cell.
Deriving a value from a sample row's `ST_SRID`/`GeometryType` would state a projection confidently
for a table that does not actually constrain one, and a wrong CRS is worse than an absent one
(§3.2). Every layer in the dev corpus is typmod-constrained, so this is the rare path, not the
common one.
"""
from psycopg import AsyncConnection

# One query for the whole catalog, matching the batch shape `_metadata_and_geom` already uses —
# the Open Data list renders every curated dataset, so per-row introspection would be N queries.
_GEOM_META_SQL = """
    SELECT c.relname,
           postgis_typmod_type(a.atttypmod),
           postgis_typmod_srid(a.atttypmod)
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_type t ON t.oid = a.atttypid
    WHERE n.nspname = 'assets'
      AND c.relname = ANY(%s)
      AND a.attname = 'geom'
      AND a.attnum > 0
      AND NOT a.attisdropped
      AND t.typname = 'geometry'
"""


async def geom_meta(
    conn: AsyncConnection, layer_ids: list[str]
) -> dict[str, tuple[str | None, int | None]]:
    """layer_id -> (geometry_type, srid) for each layer whose asset table HAS a `geom` column.

    The geometry type is PostGIS's own spelling — 'Point', 'MultiPolygon', 'MultiLineString' — not a
    UI label; collapsing Multi* to "Line"/"Polygon" is presentation and belongs to the client. The
    SRID is the storage CRS, which is what a downloader needs: `/export` reprojects at the edge on
    request, but the layer's own CRS is the honest default to advertise.

    A layer absent from the result has no geometry (a hosted table) — the same signal the caller's
    existing `has_geom` set carried. A layer PRESENT with (None, None) has a geom column with no
    typmod constraint: the cell is omitted rather than guessed.
    """
    if not layer_ids:
        return {}
    async with conn.cursor() as cur:
        await cur.execute(_GEOM_META_SQL, (layer_ids,))
        out: dict[str, tuple[str | None, int | None]] = {}
        for name, gtype, srid in await cur.fetchall():
            # typmod -1 => PostGIS reports ('Geometry', 0), which means "unconstrained", not 4326.
            out[name] = (
                None if not gtype or gtype == "Geometry" else gtype,
                None if not srid else srid,
            )
        return out
