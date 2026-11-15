"""Dynamic MVT — api-side ``ST_AsMVT`` for filtered / labeled / view layers (decision-dynamic-mvt.md).

The static tile path (``tiles.py`` → pg_tileserv → ``assets.slot_mvt``, V061) serves an
unfiltered layer's tiles with geom + id only. The three cases that need PER-REQUEST logic —
an attribute filter, label attributes, a view layer — used to fall off tiles onto the
2,000-per-viewport GeoJSON path. This route keeps them on tiles: it composes the same MVT
query pg_tileserv would run, but WHERE and the attribute list come from the request.

Injection discipline — the whole point of the api-side architecture: the predicate and the
field list compile through the SAME machinery ``/features`` uses (``layer_query._where`` /
introspected column whitelist / ``platform.layer_fields``): whitelisted ops, bound values,
bound props keys, ``sql.Identifier`` only for introspection-proven names. ``layer_id`` is
proven a ``platform.layers`` row before it names a relation (table OR ``vw_*`` view — a view
is a relation, which is what makes views first-class tiles here with zero registration).

Authz mirrors the static tile route exactly (``layer_query._authorize_read``): authenticated →
``view_allowed`` (403); anonymous → ``public_shared`` only (404 — existence-hiding); anon
requests additionally may not select ``_ANON_SCRUB_COLUMNS`` (422 — the tile surface must not
become a scrub bypass). Dynamic tiles are **never cached** (``Cache-Control: no-store``, all
callers): a predicate in a shared cache key is the poisoning surface the decision doc rules out.

The MVT layer name inside the tile is the ``layer_id`` — same convention as ``slot_mvt``
(V061 passes the token as the ST_AsMVT name), so the SPA's existing ``source-layer`` wiring
carries over unchanged.
"""
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from psycopg import AsyncConnection, sql

from ..auth import get_optional_user
from ..db import get_db
from ..models.user import User
from .layer_query import (
    _ANON_SCRUB_COLUMNS,
    _assets_table,
    _authorize_read,
    _col,
    _columns,
    _props_fields,
    _require_registered,
    _where,
)

# Single source of truth for both caps (no drift with the persisted web-map validators):
# a tile request may carry at most as many filter conditions / attribute fields as a saved
# map may persist (decision-feature-filter.md §4a.5 / decision-map-popups.md §3).
from .web_maps import _MAX_FILTERS, _MAX_POPUP_FIELDS

log = structlog.get_logger(__name__)

router = APIRouter(tags=["tiles"])

_TILE_EXTENT = 4096  # MVT tile extent — matches slot_mvt (V061) and pg_tileserv's default

# information_schema data_types ST_AsMVT accepts as-is. numeric downcasts to float8 (labels/
# filters display precision, not accounting precision); everything else (uuid, timestamp,
# date, jsonb, …) rides as ::text — same display-typed posture as the GeoJSON properties.
_MVT_NATIVE_TYPES = frozenset(
    {"text", "character varying", "character", "integer", "bigint", "smallint",
     "boolean", "double precision", "real"}
)


def _attr_item(name: str, data_type: str) -> sql.Composed:
    """One introspection-proven REAL column as an MVT attribute, cast to an ST_AsMVT-safe type."""
    if data_type in _MVT_NATIVE_TYPES:
        return sql.SQL("{c}").format(c=_col(name))
    cast = sql.SQL("::float8") if data_type == "numeric" else sql.SQL("::text")
    return sql.SQL("{c}{cast} AS {a}").format(c=_col(name), cast=cast, a=sql.Identifier(name))


def _parse_fields(
    raw: str | None, cols: dict[str, str], props_fields: dict[str, Any], is_anon: bool
) -> tuple[list[sql.Composed], dict[str, Any]]:
    """The ``fields=`` param (comma-separated attribute names for labels/popups) → validated
    SELECT items + bound params. Every name must be a real column or an active owner-defined
    props field — the same validity sets the filter compiler uses; a props KEY binds as a
    param (never an identifier). ``geom`` is not an attribute; ``id`` always ships anyway.
    ANONYMOUS callers may not select the scrub set (422): the /features scrub (Talus 23)
    holds on the tile surface too."""
    items: list[sql.Composed] = []
    params: dict[str, Any] = {}
    if not raw:
        return items, params
    names = [n.strip() for n in raw.split(",") if n.strip()]
    seen: set[str] = set()
    fields = [n for n in names if not (n in seen or seen.add(n))]
    if len(fields) > _MAX_POPUP_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"at most {_MAX_POPUP_FIELDS} fields per tile request",
        )
    for i, name in enumerate(fields):
        if name in ("geom", "id"):
            continue  # geometry is not an attribute; id is always included
        if name in cols:
            if is_anon and name in _ANON_SCRUB_COLUMNS:
                raise HTTPException(status_code=422, detail=f"unknown field '{name}'")
            items.append(_attr_item(name, cols[name]))
        elif name in props_fields:
            key = f"fld{i}"
            params[key] = name
            items.append(
                sql.SQL("(t.props ->> {k}) AS {a}").format(
                    k=sql.Placeholder(key), a=sql.Identifier(name)
                )
            )
        else:
            raise HTTPException(status_code=422, detail=f"unknown field '{name}'")
    return items, params


async def _geom_srid(conn: AsyncConnection, layer_id: str) -> int:
    """The relation's geom SRID, for transforming the tile envelope into storage CRS
    (decision-crs-native-slice1.md). ``geometry_columns`` covers tables AND views whose geom
    keeps its typmod; a bare-typed view geom (srid 0/absent) falls back to sampling one row;
    an empty layer defaults to canonical 4326. NOT Find_SRID — that raises on views."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT srid FROM geometry_columns WHERE f_table_schema = 'assets' "
            "AND f_table_name = %s AND f_geometry_column = 'geom'",
            (layer_id,),
        )
        row = await cur.fetchone()
        if row and row[0]:
            return int(row[0])
        await cur.execute(
            sql.SQL("SELECT ST_SRID(t.geom) FROM {tbl} AS t WHERE t.geom IS NOT NULL LIMIT 1").format(
                tbl=_assets_table(layer_id)
            )
        )
        row = await cur.fetchone()
        return int(row[0]) if row and row[0] else 4326


@router.get(
    "/tiles-dyn/{layer_id}/{z}/{x}/{y}.pbf",
    summary="Dynamic MVT tile — per-request filter + attributes; serves filtered/labeled/view layers",
)
async def dynamic_tile(
    layer_id: str,
    z: int = Path(ge=0, le=24),
    x: int = Path(ge=0),
    y: int = Path(ge=0),
    filters: list[str] = Query(
        default=[], alias="filter",
        description="Repeatable 'field:op:value' — the same grammar as /layers/{id}/features",
    ),
    fields: str | None = Query(
        None, description="Comma-separated attribute names to include in the tile (labels/popups)"
    ),
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> Response:
    await _require_registered(conn, layer_id)
    is_anon = await _authorize_read(conn, principal, layer_id)
    if len(filters) > _MAX_FILTERS:
        raise HTTPException(status_code=422, detail=f"at most {_MAX_FILTERS} filter conditions")

    cols = await _columns(conn, layer_id)
    if not cols:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    if "geom" not in cols:
        raise HTTPException(status_code=422, detail="layer has no geometry to tile")
    props_fields = await _props_fields(conn, layer_id)

    # WHERE from the shared compiler — no bbox (the tile envelope IS the spatial bound), no ids.
    where_sql, params = _where(filters, None, None, set(cols), props_fields)
    attr_items, attr_params = _parse_fields(fields, cols, props_fields, is_anon)
    srid = await _geom_srid(conn, layer_id)

    # Same query shape as assets.slot_mvt (V061, V118): envelope pre-filter at the relation's own
    # storage SRID (index-friendly; converted by assets.shape_in_srid so a world-scale or far-away
    # tile on a natively stored layer is an empty tile, not an error), geometry to 3857 for the tile grid, id::text whenever the
    # relation has one (a view author may exclude id — identify then has nothing to fetch by,
    # matching the GeoJSON path's behavior), plus the validated per-request attributes. The MVT
    # layer name = layer_id (bound param).
    select_items = [
        sql.SQL(
            "ST_AsMVTGeom(ST_Transform(t.geom, 3857), ST_TileEnvelope({z}, {x}, {y})) AS geom"
        ).format(z=sql.Placeholder("_z"), x=sql.Placeholder("_x"), y=sql.Placeholder("_y")),
    ]
    if "id" in cols:
        select_items.append(sql.SQL("t.id::text AS {a}").format(a=sql.Identifier("id")))
    select_items.extend(attr_items)
    q = sql.SQL(
        "SELECT ST_AsMVT(tile, {name}, {extent}, 'geom') FROM ("
        "SELECT {sel} FROM {tbl} AS t "
        "WHERE t.geom && (SELECT assets.shape_in_srid(ST_TileEnvelope({z}, {x}, {y}), {srid})) AND ({where})"
        ") AS tile WHERE tile.geom IS NOT NULL"
    ).format(
        name=sql.Placeholder("_name"),
        extent=sql.Literal(_TILE_EXTENT),
        sel=sql.SQL(", ").join(select_items),
        tbl=_assets_table(layer_id),
        z=sql.Placeholder("_z"),
        x=sql.Placeholder("_x"),
        y=sql.Placeholder("_y"),
        srid=sql.Placeholder("_srid"),
        where=where_sql,
    )
    async with conn.cursor() as cur:
        await cur.execute(
            q,
            {**params, **attr_params, "_name": layer_id, "_z": z, "_x": x, "_y": y, "_srid": srid},
        )
        row = await cur.fetchone()

    # NEVER cached (decision-dynamic-mvt.md #5): a predicate must not live in any shared cache
    # key, and an authed user's filtered tile must not outlive the request in a browser cache.
    headers = {"Cache-Control": "no-store"}
    tile = row[0] if row else None
    if not tile:
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers=headers)
    return Response(
        content=bytes(tile),
        media_type="application/vnd.mapbox-vector-tile",
        headers=headers,
    )
