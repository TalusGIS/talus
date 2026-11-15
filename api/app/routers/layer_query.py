"""Generic, introspection-driven feature query for ANY registered layer (Talus 15).

The catalog **Data table** reads from here. Unlike `layer_features.py` (slot-only CRUD,
slot-regex gated), this serves **every** layer in `platform.layers` — hosted slots, Talus 38
view layers, and geometry-less hosted tables — from one handler. View-gated; read-only (no
mutation).

Injection discipline (mirrors `symbology.py` / `layer_features.py`): `layer_id` is proven a
`platform.layers` row before it names a table; every **filter/sort field** is validated
against `information_schema` for that table before it is wrapped in `psycopg.sql.Identifier`;
ops come from a fixed whitelist; every value is a bound parameter. The relation name equals
the `layer_id` in the `assets` schema uniformly (tal_xxx -> assets.tal_xxx, vw_xxx ->
assets.vw_xxx), so one resolution path serves slot + view layers.

Geometry is **optional**: a layer with a `geom` column emits GeoJSON geometry; one without
(a hosted table) emits `geometry: null`. Coded fields come back as raw codes — the client
resolves labels from the customDomain metadata it already holds (V046);
`created_by`/`updated_by` UUIDs are additionally resolved
to a display name here (the client has no users endpoint). The response carries the OGC API
Features members `numberMatched` (rows satisfying the filter, ignoring the page) and
`numberReturned` (this page).
"""
import csv
import io
import json
import math
import re
import sqlite3
import struct
import zipfile
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

import structlog
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from .. import last_accessed
from ..auth import _optional_claims, get_current_user, get_optional_user
from ..authz import edit_allowed, public_object_allowed, view_allowed
from ..config import settings
from ..db import get_db
from ..geo import parse_bbox
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["layer-query"])

# Bounded operator set (Q4). Each is rendered safely; see _parse_filters for how each maps to
# SQL. `in`/`eq`/`ne`/`ilike` compare via ::text so they are type-uniform across coded/text/bool;
# `gte`/`lte` stay native so date/numeric ranges keep their type semantics and index use.
# This frozenset is the SINGLE SOURCE OF TRUTH for the op whitelist — web_maps.py imports it to
# validate persisted filter conditions, so the render endpoint and the saved web map can't drift.
# gt/lt/like/notin joined for the GeoServices WHERE subset (interop I-3): strict range bounds,
# verbatim SQL LIKE (client-supplied wildcards, case-sensitive — Esri parity; `ilike` remains the
# portal's contains-style op), and NOT IN (ArcGIS Pro excludes its in-session edits from every
# fetch via `OBJECTID NOT IN(…)` — found live on the Pro rig). Supersets only — every existing
# filter string parses unchanged.
_VALID_OPS = frozenset({"in", "notin", "eq", "ne", "gt", "lt", "gte", "lte", "ilike", "like"})

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 5000

# Columns withheld from the ANONYMOUS (public) read surface (Talus 23 §F2; Talus 33): editor
# identity (the audit FKs + their resolved names) and AI provenance — internal metadata a public
# viewer has no business seeing. Decision (Open Data): a public download carries geometry +
# attributes, NOT attachments (see decision-open-data.md §9). Authenticated callers are unaffected.
# (`cover_photo_id` left this set with the pilot built-ins — no such column exists on any slot, and
# a dead name here would silently scrub a future owner-defined field that happened to reuse it.)
_ANON_SCRUB_COLUMNS = frozenset({"created_by", "updated_by", "ai_provenance"})


# --- guards + introspection -----------------------------------------------------

# The two server-generated layer-id shapes — the ONLY platform.layers populations since the
# pilot built-ins retired (decision-retire-builtins.md phase 2). Proven before the registry
# check, so no other string can ever name a relation — here or in the importers that share
# this proof (tiles_dyn, layer_views, geoservices). Belt to the registry check's braces,
# mirroring symbology.py.
_SLOT_RE = re.compile(r"^tal_[0-9a-f]{8}$")
_VIEW_RE = re.compile(r"^vw_[0-9a-f]{12}$")


async def _require_registered(conn: AsyncConnection, layer_id: str) -> None:
    """layer_id must be shaped like a slot or view id AND be a row in platform.layers, else
    404 (existence-hiding, matching the registry miss). The shape gate runs first; the
    registry check binds layer_id as a parameter — together they make it one of a fixed
    known-safe set before it names a relation."""
    if not (_SLOT_RE.fullmatch(layer_id) or _VIEW_RE.fullmatch(layer_id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.layers WHERE layer_id = %s", (layer_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")


async def _columns(conn: AsyncConnection, layer_id: str) -> dict[str, str]:
    """column_name -> data_type for assets.<layer_id> (table name == layer_id, bound param).
    This is the validation whitelist: only a name in here may be a filter/sort field."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'assets' AND table_name = %s ORDER BY ordinal_position",
            (layer_id,),
        )
        return {r[0]: r[1] for r in await cur.fetchall()}


async def _props_fields(conn: AsyncConnection, layer_id: str) -> dict[str, tuple[str, str | None, str]]:
    """field_key -> (data_type, domain_id, label) for the layer's ACTIVE owner-defined fields
    (platform.layer_fields, V044/V046) — the layer's VIRTUAL columns (decision-geometry-calc.md §3.4).
    Values live under the real `props` jsonb column; in SQL the key is always a BOUND PARAM
    (`t.props ->> %s`), never an identifier, so the injection posture is unchanged. Empty for
    a layer with none (a view's queryable schema is its real relation columns). The V044
    reserved-key CHECK guarantees a field_key can never shadow a real column, so the two
    validity sets are disjoint by construction."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT field_key, data_type, domain_id::text, label FROM platform.layer_fields "
            "WHERE layer_id = %s AND active ORDER BY sort_order, field_key",
            (layer_id,),
        )
        return {r[0]: (r[1], r[2], r[3]) for r in await cur.fetchall()}


# layer_fields.data_type -> the information_schema-style type name /columns emits for a virtual
# props column, so the client's existing type→control mapping (friendlyType/filterKind) works
# unchanged. 'coded' stays text-typed on the wire; its custom domain rides the customDomain marker.
_PROPS_TYPE_NAME = {"number": "numeric", "text": "text", "date": "date", "boolean": "boolean", "coded": "text"}


async def _view_source_fields(
    conn: AsyncConnection, layer_id: str
) -> dict[str, tuple[str, str | None, str]]:
    """For a VIEW layer: the SOURCE's ACTIVE owner-defined fields, restricted to the keys the
    view's stored def exposes — the metadata resolver behind the fork-B enrichment
    (decision-view-props-fields.md) on /columns and /layers/{id}/fields. Empty for a non-view.
    A view's props-derived computed columns introspect as bare typed relation columns; matching
    them back to the source's defs (unambiguous — the V044 reserved-key CHECK keeps field keys
    disjoint from real column names) recovers the owner's label + customDomain. Restricting to
    ``allowed_columns`` keeps a joined output (``<alias>_<key>``) that happens to collide with an
    UNEXPOSED source field key from borrowing that field's metadata."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT source_layer_id, allowed_columns FROM platform.layer_views "
            "WHERE view_layer_id = %s",
            (layer_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return {}
    source_id, allowed = row
    exposed = set(allowed or [])
    return {k: v for k, v in (await _props_fields(conn, source_id)).items() if k in exposed}


def _props_cast(data_type: str) -> sql.SQL:
    """The range-op cast for a props extraction, from the FIXED declared-type map (never user
    input): number compares ::numeric, date ::date, everything else as the extracted text."""
    if data_type == "number":
        return sql.SQL("::numeric")
    if data_type == "date":
        return sql.SQL("::date")
    return sql.SQL("")


def _assets_table(layer_id: str) -> sql.Identifier:
    """assets.<layer_id> — ONLY after _require_registered/_columns prove the id is a real
    registry row + table. Never an f-string of raw input."""
    return sql.Identifier("assets", layer_id)


def _col(field: str) -> sql.Identifier:
    """A column reference on the aliased layer table (t.<field>)."""
    return sql.Identifier("t", field)


# --- filter parsing -------------------------------------------------------------


def _parse_filters(
    raw: list[str], valid: set[str], props_fields: dict[str, tuple[str, str | None]] | None = None
) -> tuple[list[sql.Composed], dict[str, Any]]:
    """Turn `field:op:value` specs into validated WHERE fragments + bound params. Every field
    is checked against `valid` (the layer's real columns) or `props_fields` (its owner-defined
    virtual columns) and every op against the whitelist BEFORE it reaches SQL; a real column
    becomes an Identifier, a props field's KEY binds as a param (`t.props ->> %s` — never an
    identifier); values are always bound. Text-typed ops (in/eq/ne/ilike) compare the raw
    extraction, matching the real-column ::text posture; range ops cast by the field's declared
    type (decision-geometry-calc.md §3.4)."""
    props_fields = props_fields or {}
    clauses: list[sql.Composed] = []
    params: dict[str, Any] = {}
    for i, spec in enumerate(raw):
        parts = spec.split(":", 2)
        if len(parts) != 3:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"bad filter '{spec}' — expected 'field:op:value'",
            )
        field, op, value = parts
        if field not in valid and field not in props_fields:
            raise HTTPException(status_code=422, detail=f"unknown filter field '{field}'")
        if op not in _VALID_OPS:
            raise HTTPException(status_code=422, detail=f"unknown filter op '{op}'")
        key = f"f{i}"
        ph = sql.Placeholder(key)
        if field in valid:
            col = _col(field)
            lhs_text = sql.SQL("{c}::text").format(c=col)
            lhs_ranged = sql.SQL("{c}").format(c=col)
        else:  # owner-defined props field — key bound, cast from the fixed declared-type map
            kp = sql.Placeholder(f"pk{i}")
            params[f"pk{i}"] = field
            lhs_text = sql.SQL("(t.props ->> {k})").format(k=kp)
            lhs_ranged = sql.SQL("((t.props ->> {k}){cast})").format(
                k=kp, cast=_props_cast(props_fields[field][0])
            )
        if op == "in":
            clauses.append(sql.SQL("{l} = ANY({p})").format(l=lhs_text, p=ph))
            params[key] = value.split(",")
        elif op == "notin":
            # NOT(=ANY): matches SQL NOT IN semantics exactly, incl. a NULL field being excluded.
            clauses.append(sql.SQL("NOT ({l} = ANY({p}))").format(l=lhs_text, p=ph))
            params[key] = value.split(",")
        elif op == "eq":
            clauses.append(sql.SQL("{l} = {p}").format(l=lhs_text, p=ph))
            params[key] = value
        elif op == "ne":
            # "is not": SQL <> is NULL (not TRUE) for null-valued rows, so a null field is NOT
            # returned by an ne filter — matches AGO's "is not" (decision-feature-filter.md §4a.4).
            clauses.append(sql.SQL("{l} <> {p}").format(l=lhs_text, p=ph))
            params[key] = value
        elif op == "gte":
            clauses.append(sql.SQL("{l} >= {p}").format(l=lhs_ranged, p=ph))
            params[key] = value
        elif op == "lte":
            clauses.append(sql.SQL("{l} <= {p}").format(l=lhs_ranged, p=ph))
            params[key] = value
        elif op == "gt":
            clauses.append(sql.SQL("{l} > {p}").format(l=lhs_ranged, p=ph))
            params[key] = value
        elif op == "lt":
            clauses.append(sql.SQL("{l} < {p}").format(l=lhs_ranged, p=ph))
            params[key] = value
        elif op == "like":
            # Verbatim pattern — the CALLER supplies wildcards (the GeoServices WHERE compiler
            # passes SQL LIKE patterns through untouched); case-sensitive, matching SQL semantics.
            clauses.append(sql.SQL("{l} LIKE {p}").format(l=lhs_text, p=ph))
            params[key] = value
        else:  # ilike
            clauses.append(sql.SQL("{l} ILIKE {p}").format(l=lhs_text, p=ph))
            params[key] = f"%{value}%"
    return clauses, params


def _order_by(
    sort: str | None, valid: set[str], props_fields: dict[str, tuple[str, str | None]] | None = None
) -> tuple[sql.Composed, dict[str, Any]]:
    """ORDER BY <field> <dir>, t.id (stable tiebreak). `sort` is 'field' or 'field:dir'.
    Returns (sql, params): a real column sorts by Identifier (no params); an owner-defined props
    field sorts by the bound-key extraction, cast by its declared type."""
    props_fields = props_fields or {}
    tiebreak = sql.SQL(", t.id") if "id" in valid else sql.SQL("")
    if not sort:
        return (sql.SQL("ORDER BY t.id") if "id" in valid else sql.SQL("")), {}
    parts = sort.split(":", 1)
    field = parts[0]
    direction = (parts[1] if len(parts) > 1 else "asc").lower()
    if field not in valid and field not in props_fields:
        raise HTTPException(status_code=422, detail=f"unknown sort field '{field}'")
    if direction not in ("asc", "desc"):
        raise HTTPException(status_code=422, detail="sort direction must be 'asc' or 'desc'")
    if field in valid:
        return sql.SQL("ORDER BY {c} {d}{t}").format(
            c=_col(field), d=sql.SQL(direction.upper()), t=tiebreak
        ), {}
    # ★ NULLS LAST on the props branch ONLY (decision-dashboards-polish.md §2). Postgres defaults
    # to NULLS FIRST for DESC, so "sort biggest first" led with the blanks — and blanks live in the
    # owner-defined props fields, which is where this was reported.
    #
    # Deliberately NOT applied to the real-column branch above. It would be a semantic no-op there
    # (every real slot column is NOT NULL except notes/ai_provenance) and it is NOT free: Postgres
    # does not use the NOT NULL constraint to prove the clause redundant, so the explicit clause
    # defeats the btree. Measured on a 327k-row layer: `objectid DESC` is an Index Scan Backward at
    # 4.9 ms; `objectid DESC NULLS LAST` is a Parallel Seq Scan + top-N heapsort at 80.6 ms — 16x
    # for nothing. Here it costs nothing, because props carries only a GIN index, which cannot serve
    # ordering: the plan is a sort either way (57 ms, identical before and after).
    #
    # If a nullable btree-indexed real column ever appears, the upgrade is to teach _columns to
    # return is_nullable and emit the clause per-column rather than per-branch.
    return sql.SQL("ORDER BY ((t.props ->> {k}){cast}) {d} NULLS LAST{t}").format(
        k=sql.Placeholder("_sortpk"),
        cast=_props_cast(props_fields[field][0]),
        d=sql.SQL(direction.upper()),
        t=tiebreak,
    ), {"_sortpk": field}


def _where(
    filters: list[str],
    bbox: str | None,
    feature_ids: list[str] | None,
    valid: set[str],
    props_fields: dict[str, tuple[str, str | None]] | None = None,
    geom_intersect: tuple[str, int] | None = None,
    storage_srid: int = 4326,
) -> tuple[sql.Composed, dict[str, Any]]:
    """The shared WHERE both the paged query and the export build: field filters (validated
    against real columns + owner-defined props fields), an optional bbox, an optional explicit id
    set (export-selected), and an optional EXACT geometry intersect (``geom_intersect`` =
    ``(geojson, srid)`` — the GeoServices façade's non-envelope spatial filters). Returns a single
    composed predicate (TRUE when empty) + bound params.

    ``storage_srid`` is the layer's STORAGE srid (``_geom_srid``): a native-storage layer keeps its
    geometry in the working CRS (decision-crs-native-slice1.md), so the 4326 bbox envelope and the
    filter geometry are transformed INTO it before the ``&&`` / ST_Intersects — the pack builder's
    pattern (paid/api/field/packs.py). Before 2026-09-26 the envelope was always 4326 against
    ``t.geom``, so on a State Plane layer every bbox query matched nothing and the changes feed
    reported every row as *outside* the pack's extent — Refresh on the phone removed them all."""
    clauses, params = _parse_filters(filters, valid, props_fields)
    srid = int(storage_srid) if storage_srid else 4326
    if bbox is not None:
        if "geom" not in valid:
            raise HTTPException(status_code=422, detail="layer has no geometry to filter by bounds")
        lon_min, lat_min, lon_max, lat_max = parse_bbox(bbox)
        env = sql.SQL("ST_MakeEnvelope({a}, {b}, {c}, {d}, 4326)").format(
            a=sql.Placeholder("bbminx"),
            b=sql.Placeholder("bbminy"),
            c=sql.Placeholder("bbmaxx"),
            d=sql.Placeholder("bbmaxy"),
        )
        if srid != 4326:
            # assets.shape_in_srid (V118), not a bare ST_Transform: a box far outside the storage
            # system's area cannot be converted — the library raises, and the door answered 500 — and
            # a world-scale box converts into nonsense. The function clips to the system's area of
            # use first and yields NULL (matches no row) when nothing is left. The scalar sub-select
            # has it evaluated once per query.
            env = sql.SQL("(SELECT assets.shape_in_srid({e}, {s}))").format(e=env, s=sql.Literal(srid))
        clauses.append(sql.SQL("t.geom && {e}").format(e=env))
        params |= {"bbminx": lon_min, "bbminy": lat_min, "bbmaxx": lon_max, "bbmaxy": lat_max}
    if geom_intersect is not None:
        # Exact ST_Intersects against an arbitrary filter geometry (polygon/polyline/multipoint/
        # point), reprojected to the layer's STORAGE CRS. ST_Intersects carries its own `&&` bbox
        # pre-filter, so the GiST index is still used. ST_MakeValid guards a slightly-malformed
        # client polygon (winding/self-touch) from erroring the whole query.
        if "geom" not in valid:
            raise HTTPException(status_code=422, detail="layer has no geometry to filter by")
        gjson, gsrid = geom_intersect
        # shape_in_srid for the same reason as the box above: a filter shape the storage system
        # cannot hold yields NULL, and ST_Intersects with NULL matches no row.
        clauses.append(sql.SQL(
            "ST_Intersects(t.geom, (SELECT ST_MakeValid(assets.shape_in_srid("
            "ST_SetSRID(ST_GeomFromGeoJSON({g}), {s}), {t}))))").format(
                g=sql.Placeholder("_gjson"), s=sql.Placeholder("_gsrid"), t=sql.Literal(srid)))
        params |= {"_gjson": gjson, "_gsrid": gsrid}
    if feature_ids:
        if "id" not in valid:
            raise HTTPException(status_code=422, detail="layer has no id to select by")
        clauses.append(sql.SQL("t.id::text = ANY({p})").format(p=sql.Placeholder("_ids")))
        params["_ids"] = feature_ids
    return (sql.SQL(" AND ").join(clauses) if clauses else sql.SQL("TRUE")), params


async def _authorize_read(
    conn: AsyncConnection, principal: User | None, layer_id: str
) -> bool:
    """Optional-auth read gate (Talus 23). AUTHENTICATED → view_allowed (403 on fail); ANONYMOUS →
    public_shared (404 on fail — existence-hiding). Returns True iff the caller is anonymous, so the
    response can scrub editor identity. Both branches fail closed (503 on OpenFGA error)."""
    if principal is not None:
        if not await view_allowed(principal, layer_id):
            log.info(
                "authz_rejected", capability=f"{layer_id}:view",
                reason="no_view_access", user_id=str(principal.id),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"not authorized to view {layer_id}"
            )
        return False
    if not await public_object_allowed(conn, layer_id, object_type="layer"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return True


# --- routes ---------------------------------------------------------------------


@router.get(
    "/layers/{layer_id}/columns",
    summary="Full column schema of a layer (name/type/coded-domain) — view-gated",
)
async def layer_columns(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    # _OPTIONAL_claims, not _validated_claims: the latter raises a uniform 401 on a
    # missing-or-non-Bearer credential, which silently made this optional-auth route
    # authenticated-only (see the docstring's history note). _optional_claims returns None when
    # there is NO Authorization header and otherwise delegates to _validated_claims — so a
    # presented-but-invalid token still 401s and never downgrades to anonymous.
    claims: dict[str, Any] | None = Depends(_optional_claims),
    principal: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    """Every column of the layer (minus `geom`, which is geometry not a table column), with
    its data_type — owner-defined fields ride along with a customDomain marker when coded.
    The metadata source for the catalog
    Data table + Fields view — UNLIKE `/layers/{id}/fields` (the symbology-denylisted subset),
    this is the FULL schema (id, audit columns included), which the table/fields views need.

    Also carries two per-caller capability flags the record-detail attachments panel needs
    (decision-attachments.md A-1): `canEdit` (this caller's edit decision) and `attachmentsEnabled`
    (the layer's attachments toggle) — so the drawer can show upload/delete affordances honestly from
    both the map-popup and catalog-table paths without extra round-trips. Anon callers get canEdit:false.

    Optional-auth (Talus 23): public layers are anon-readable; the anon schema omits the editor
    audit columns (created_by/updated_by), matching the scrub on /features.

    HISTORY (decision-columns-anon.md): this route WAS authenticated-only for a while, and not on
    purpose. `c70d53a` added the two capability flags above; `edit_allowed()` needs the token's realm
    roles, so it reached for `_validated_claims` — which 401s a missing bearer — and converted an
    optional-auth route into an authenticated one as a side effect of a UI flag. Anonymous public
    maps and embeds carrying a VIEW layer lost their popup/label attributes for the whole session
    (AssetMap.ensureViewTileFields builds the dynamic-MVT `fields=` list from this response and
    swallows the failure), and every personal access token — which arrives as HTTP Basic, not Bearer
    — was rejected despite this being one of the documented PAT read surfaces. It needed claims WHEN
    THEY EXIST, not claims-or-401."""
    await _require_registered(conn, layer_id)
    is_anon = await _authorize_read(conn, principal, layer_id)
    cols = await _columns(conn, layer_id)
    if not cols:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    columns = [
        {"name": name, "type": dtype}
        for name, dtype in cols.items()
        if name != "geom" and not (is_anon and name in _ANON_SCRUB_COLUMNS)
    ]
    # Owner-defined fields as first-class VIRTUAL columns (decision-geometry-calc.md §3.4): pg-style
    # type so the client's existing controls apply; `props: true` marks the value's home (the client
    # reads properties.props[name]); the owner-authored label rides along; a coded field carries its
    # custom domain id. Anon sees them — the props blob itself has never been scrubbed.
    for key, (dtype, domain_id, label) in (await _props_fields(conn, layer_id)).items():
        entry: dict[str, Any] = {
            "name": key, "type": _PROPS_TYPE_NAME.get(dtype, "text"),
            "props": True, "label": label,
        }
        if domain_id:
            entry["customDomain"] = domain_id
        columns.append(entry)
    # A VIEW's props-derived computed columns introspect as bare typed columns above; re-attach
    # the owner's label + customDomain from the SOURCE's field defs (decision-view-props-fields.md
    # fork B) so coded fields on views resolve labels — without this every view resurfaces the
    # raw-codes gap (#136/#141 class). NOT `props: true`: on a view the value is a real relation
    # column, read from properties[name] like any other.
    for key, (_dtype, domain_id, label) in (await _view_source_fields(conn, layer_id)).items():
        for entry in columns:
            if entry["name"] == key:
                entry["label"] = label
                if domain_id:
                    entry["customDomain"] = domain_id
    # Capability flags for the record-detail attachments panel (A-1). canEdit is the full edit decision
    # (anon → false); attachmentsEnabled is the layer's toggle. Both additive — existing consumers ignore them.
    # `claims is not None`: a PAT caller is a real principal from HTTP Basic carrying NO Bearer, so
    # _optional_claims is None and its roles are unknown HERE — report canEdit false rather than
    # guess. (Until the once-over fix A3 this guard was the only thing between such a caller and a
    # spoofed True: the role helpers answered True for None claims in every mode. They now do so in
    # stub mode only, and the guard stays as the second lock.) Anonymous callers are already
    # excluded by `principal is not None`, which also keeps a None user away from edit_allowed's
    # non-Optional `user: User`.
    can_edit = False
    if principal is not None and claims is not None:
        can_edit, _ = await edit_allowed(conn, claims, principal, layer_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT attachments_enabled FROM platform.layers WHERE layer_id = %s", (layer_id,)
        )
        ae_row = await cur.fetchone()
    return {
        "layer_id": layer_id,
        "columns": columns,
        "canEdit": bool(can_edit),
        "attachmentsEnabled": bool(ae_row[0]) if ae_row else False,
    }


@router.get(
    "/layers/{layer_id}/features",
    summary="Query a layer's features (filter / sort / paginate) — view-gated",
)
async def query_features(
    layer_id: str,
    bbox: str | None = Query(
        None, description="EPSG:4326 bounds 'lon_min,lat_min,lon_max,lat_max' (optional)"
    ),
    filters: list[str] = Query(
        default=[], alias="filter", description="Repeatable 'field:op:value' (op in in/eq/ne/gte/lte/ilike)"
    ),
    sort: str | None = Query(None, description="'field' or 'field:asc'|'field:desc'"),
    feature_ids: list[str] = Query(
        default=[], alias="id",
        description="Repeatable id to page only selected rows (Show-selected-records mode)",
    ),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    await _require_registered(conn, layer_id)
    is_anon = await _authorize_read(conn, principal, layer_id)
    # Third surface for the _where ids branch (export, extent, now the page itself —
    # decision-select-features.md F5): bound params, AND-composed with filters/bbox, so paging,
    # sort, and numberMatched stay honest over the selected subset.
    total, features = await feature_page(
        conn, layer_id, is_anon,
        filters=filters, bbox=bbox, feature_ids=feature_ids or None, sort=sort,
        limit=limit, offset=offset,
    )
    await last_accessed.touch(conn, "layer", layer_id)  # stale-content signal (throttled, best-effort)
    return {
        "type": "FeatureCollection",
        "numberMatched": total,
        "numberReturned": len(features),
        "features": features,
    }


async def feature_page(
    conn: AsyncConnection,
    layer_id: str,
    is_anon: bool,
    *,
    filters: list[str],
    bbox: str | None,
    feature_ids: list[str] | None,
    sort: str | None,
    limit: int,
    offset: int,
    geom_intersect: tuple[str, int] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """The one paged-GeoJSON builder (extracted for interop I-1): query_features above AND the OGC
    items/single-feature routes (ogc.py) all delegate here, so the SELECT shape, anon scrub, filter/
    bbox/sort machinery, and native→4326 reprojection stay in exactly one place. Caller has already
    run _require_registered + _authorize_read (is_anon is that result). ``geom_intersect`` is the
    optional exact spatial filter (GeoServices non-envelope geometry). Returns
    (numberMatched, features)."""
    cols = await _columns(conn, layer_id)
    if not cols:  # registered but no table (shouldn't happen) -> treat as not found
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    valid = set(cols)
    props_fields = await _props_fields(conn, layer_id)

    where_sql, params = _where(
        filters, bbox, feature_ids, valid, props_fields, geom_intersect,
        storage_srid=(await _geom_srid(conn, layer_id) or 4326) if (bbox is not None or geom_intersect) else 4326,
    )
    tbl = _assets_table(layer_id)

    # numberMatched: the full filtered count, page-independent (OGC API Features).
    count_q = sql.SQL("SELECT count(*) FROM {tbl} AS t WHERE {where}").format(tbl=tbl, where=where_sql)

    # SELECT list, built from the introspected columns. id -> text; geom -> GeoJSON geometry;
    # created_by/updated_by -> resolved display name (the client has no users endpoint).
    select_items: list[sql.Composed] = []
    joins: list[sql.Composed] = []
    for name in cols:  # insertion order == ordinal_position
        if name == "geom":
            continue
        # Anonymous scrub (Talus 23): never expose editor identity or AI provenance on the public
        # surface — public data is fine, who-edited-it / how-it-was-made is not.
        if is_anon and name in _ANON_SCRUB_COLUMNS:
            continue
        if name == "id":
            select_items.append(
                sql.SQL("{c}::text AS {a}").format(c=_col("id"), a=sql.Identifier("id"))
            )
        else:
            select_items.append(sql.SQL("{c}").format(c=_col(name)))
    if "geom" in valid:
        # GeoJSON is WGS-84 by RFC 7946 — ST_Transform to 4326 (a no-op for canonical-4326 layers; reprojects
        # native-storage layers native→4326 for display/API). decision-crs-native-slice1.md §7.
        # 15 decimal digits so float8 coordinates round-trip bit-exact — the snap engine's index must carry
        # the true stored values (decision-web-editor-snapping.md §3.2).
        select_items.append(sql.SQL("ST_AsGeoJSON(ST_Transform(t.geom, 4326), 15)::jsonb AS geometry"))
    if not is_anon and "created_by" in valid:
        joins.append(sql.SQL("LEFT JOIN platform.users cu ON t.created_by = cu.id"))
        select_items.append(
            sql.SQL("COALESCE(cu.display_name, cu.username, cu.email) AS created_by_name")
        )
    if not is_anon and "updated_by" in valid:
        joins.append(sql.SQL("LEFT JOIN platform.users uu ON t.updated_by = uu.id"))
        select_items.append(
            sql.SQL("COALESCE(uu.display_name, uu.username, uu.email) AS updated_by_name")
        )

    order_sql, order_params = _order_by(sort, valid, props_fields)
    data_q = sql.SQL(
        "SELECT {sel} FROM {tbl} AS t {joins} WHERE {where} {order} LIMIT {lim} OFFSET {off}"
    ).format(
        sel=sql.SQL(", ").join(select_items),
        tbl=tbl,
        joins=sql.SQL(" ").join(joins) if joins else sql.SQL(""),
        where=where_sql,
        order=order_sql,
        lim=sql.Placeholder("_lim"),
        off=sql.Placeholder("_off"),
    )

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(count_q, params)
        total = (await cur.fetchone())["count"]
        await cur.execute(data_q, {**params, **order_params, "_lim": limit, "_off": offset})
        rows = await cur.fetchall()

    features: list[dict[str, Any]] = []
    for row in rows:
        fid = row.pop("id", None)
        geometry = row.pop("geometry", None)
        features.append(
            {"type": "Feature", "id": fid, "geometry": geometry, "properties": row}
        )

    return total, features


# --- export ---------------------------------------------------------------------

# Safety ceiling on an export row count — generous (county-scale imports run ~330k, the bench
# pilot 12k). Honors "get ALL your data out" while bounding an unbounded in-memory build; a hit
# is logged, never silent.
_EXPORT_CAP = 250_000


_EXPORT_BATCH = 2000
_EXPORT_TOO_LARGE = ("This export is too large to build at once. Filter it (for example to a smaller area or "
                     "fewer features) and export again, or read the layer from a desktop client.")


def _row_bytes(row: dict[str, Any]) -> int:
    """A cheap estimate of what one fetched row holds: the text and binary values by length, nested
    JSON by its serialised length, anything else at a flat 16 bytes."""
    n = 0
    for v in row.values():
        if isinstance(v, (str, bytes, bytearray)):
            n += len(v)
        elif isinstance(v, (dict, list)):
            n += len(json.dumps(v, default=str))
        else:
            n += 16
    return n


async def _fetch_export_rows(conn: AsyncConnection, q, params: dict[str, Any], *,
                             layer_id: str, fmt: str) -> list[dict[str, Any]]:
    """Fetch an export's rows in batches from a server-side cursor, counting what they hold, and stop
    with 413 past `settings.export_max_bytes` (once-over 2026-11-15 K2). A client-side cursor takes the
    whole result at `execute`, so the count could only come after the memory was spent."""
    ceiling = settings.export_max_bytes
    rows: list[dict[str, Any]] = []
    held = 0
    async with conn.cursor(name="talus_export", row_factory=dict_row) as cur:
        await cur.execute(q, params)
        while True:
            batch = await cur.fetchmany(_EXPORT_BATCH)
            if not batch:
                break
            held += sum(_row_bytes(r) for r in batch)
            rows.extend(batch)
            if held > ceiling:
                log.warning("export_refused_too_large", layer_id=layer_id, fmt=fmt,
                            held_bytes=held, ceiling=ceiling)
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                    detail=_EXPORT_TOO_LARGE)
    return rows


def _truncation_meta(returned: int, cap: int = _EXPORT_CAP) -> dict[str, int] | None:
    """None when the export is complete; {'returned', 'cap'} when the cap bit
    (decision-export-truncation.md §3).

    The query runs `LIMIT cap`, so `returned >= cap` is the only available signal — a layer
    with EXACTLY cap rows over-warns. That false positive is deliberate: the alternative is a
    second full COUNT(*) scan on every download, and over-warning is the safe direction when
    the failure mode being prevented is a silently incomplete file.
    """
    return {"returned": returned, "cap": cap} if returned >= cap else None


def _export_headers(filename_stem: str, ext: str, meta: dict[str, int] | None) -> dict[str, str]:
    """Content-Disposition + the truncation headers. When truncated, the MARKER GOES IN THE
    FILENAME: a curl/QGIS/ArcGIS user who never reads a header still sees it the moment the
    file lands, and it survives being moved off the machine."""
    name = f"{filename_stem}-TRUNCATED-at-{meta['cap']}.{ext}" if meta else f"{filename_stem}.{ext}"
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    if meta:
        headers["X-Talus-Export-Truncated"] = "true"
        headers["X-Talus-Export-Returned"] = str(meta["returned"])
        headers["X-Talus-Export-Cap"] = str(meta["cap"])
    return headers


def _csv_cell(value: Any) -> str:
    """One CSV value: NULL -> '', dict/list (jsonb) -> compact JSON, everything else (uuid,
    datetime, date, number, bool) -> its str()."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


# --- bounded stats (decision-dashboards.md F1b — the dashboards arc's ONE new data path) -----
#
# A whitelisted aggregation over a filtered layer, for dashboard indicators and charts. The
# grammar is deliberately a CLOSED set — agg from a frozenset, field/groupBy validated against
# the layer's real columns or its owner-defined props fields, filters via the untouched
# _parse_filters — never an expression language (the standing bounded-grammar fence; the
# QGIS-Plugins indicator_expr.py anti-lesson). Rides EXACTLY the /features gate, in the same
# file, so the two can never drift apart.

_STATS_AGGS = frozenset({"count", "sum", "avg", "min", "max"})
_STATS_NUMERIC_ONLY = frozenset({"sum", "avg"})  # min/max also order dates/text usefully
_NUMERIC_COL_TYPES = frozenset(
    {"smallint", "integer", "bigint", "numeric", "real", "double precision"}
)
_STATS_MAX_GROUPS = 50  # a chart axis, not a data dump; deterministic order + truncated flag


def _stats_value(v: Any) -> Any:
    """JSON-safe aggregate value: ints/floats pass through, Decimal -> float, date/datetime ->
    isoformat, NULL -> None, anything else -> str (min/max over text)."""
    if v is None or isinstance(v, (int, float)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


@router.get(
    "/layers/{layer_id}/stats",
    summary="Bounded aggregation over a filtered layer (count/sum/avg/min/max, optional groupBy) — view-gated",
)
async def layer_stats(
    layer_id: str,
    agg: str = Query(..., description="count | sum | avg | min | max — a whitelist, never an expression"),
    field: str | None = Query(
        None, description="Column to aggregate (required except for count, which counts rows; "
        "count with a field counts its non-null values)"
    ),
    group_by: str | None = Query(
        None, alias="groupBy",
        description=f"Optional column to group by (top {_STATS_MAX_GROUPS} groups by value; "
        "`truncated` flags an overflow)",
    ),
    bbox: str | None = Query(
        None, description="EPSG:4326 bounds 'lon_min,lat_min,lon_max,lat_max' (optional)"
    ),
    filters: list[str] = Query(
        default=[], alias="filter", description="Repeatable 'field:op:value' — the /features grammar"
    ),
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    """Aggregate values for dashboard indicators/charts. Optional-auth exactly like /features:
    authed -> view_allowed (403), anon -> public_shared (404, existence-hiding). Anonymous
    callers may not aggregate or group by the scrubbed editor-identity columns — an aggregate
    over created_by is the same leak /features' scrub exists to prevent."""
    await _require_registered(conn, layer_id)
    is_anon = await _authorize_read(conn, principal, layer_id)
    cols = await _columns(conn, layer_id)
    if not cols:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    valid = set(cols)
    props_fields = await _props_fields(conn, layer_id)

    if agg not in _STATS_AGGS:
        raise HTTPException(status_code=422, detail=f"agg must be one of {sorted(_STATS_AGGS)}")
    if is_anon:
        # The anon scrub, extended to aggregation: grouping or aggregating over editor-identity
        # columns would reconstruct exactly what /features hides from the public.
        for f in (field, group_by):
            if f in _ANON_SCRUB_COLUMNS:
                raise HTTPException(status_code=422, detail=f"unknown stats field '{f}'")

    params: dict[str, Any] = {}

    def _target(name: str, *, numeric_required: bool, pkey: str) -> sql.Composable:
        """A validated aggregation/grouping target: a real column becomes an Identifier, an
        owner-defined props field a bound-key extraction cast by its declared type — the two
        _parse_filters branches, reused. `geom` is never aggregatable."""
        if name in valid and name != "geom":
            if numeric_required and cols[name] not in _NUMERIC_COL_TYPES:
                raise HTTPException(
                    status_code=422, detail=f"agg '{agg}' requires a numeric field, not '{name}'"
                )
            # Postgres has no min(uuid)/max(uuid) — refuse honestly (422) instead of erroring
            # the query (the security review's papercut, 2026-09-03).
            if agg in ("min", "max") and cols[name] == "uuid":
                raise HTTPException(
                    status_code=422, detail=f"agg '{agg}' does not apply to '{name}'"
                )
            return _col(name)
        if name in props_fields:
            if numeric_required and props_fields[name][0] != "number":
                raise HTTPException(
                    status_code=422, detail=f"agg '{agg}' requires a numeric field, not '{name}'"
                )
            params[pkey] = name
            return sql.SQL("((t.props ->> {k}){cast})").format(
                k=sql.Placeholder(pkey), cast=_props_cast(props_fields[name][0])
            )
        raise HTTPException(status_code=422, detail=f"unknown stats field '{name}'")

    if field is None:
        if agg != "count":
            raise HTTPException(status_code=422, detail=f"agg '{agg}' requires a field")
        agg_sql: sql.Composable = sql.SQL("count(*)")
    else:
        # sql.SQL(agg) is safe here: agg was just proven a member of the _STATS_AGGS frozenset.
        agg_sql = sql.SQL("{a}({t})").format(
            a=sql.SQL(agg),
            t=_target(field, numeric_required=agg in _STATS_NUMERIC_ONLY, pkey="_aggpk"),
        )

    group_sql: sql.Composable | None = None
    if group_by is not None:
        group_sql = _target(group_by, numeric_required=False, pkey="_gbpk")

    where_sql, wparams = _where(
        filters, bbox, None, valid, props_fields,
        storage_srid=(await _geom_srid(conn, layer_id) or 4326) if bbox is not None else 4326,
    )
    params |= wparams
    tbl = _assets_table(layer_id)

    if group_sql is None:
        q = sql.SQL("SELECT {a} FROM {tbl} AS t WHERE {w}").format(a=agg_sql, tbl=tbl, w=where_sql)
        async with conn.cursor() as cur:
            await cur.execute(q, params)
            row = await cur.fetchone()
        rows = [{"group": None, "value": _stats_value(row[0] if row else None)}]
        truncated = False
    else:
        # Deterministic: biggest groups first, group value as tiebreak; one row past the cap
        # probes overflow. Grouping by the aggregation target is bounded by construction.
        q = sql.SQL(
            "SELECT {g} AS grp, {a} AS val FROM {tbl} AS t WHERE {w} "
            "GROUP BY 1 ORDER BY 2 DESC NULLS LAST, 1 ASC NULLS LAST LIMIT {lim}"
        ).format(g=group_sql, a=agg_sql, tbl=tbl, w=where_sql,
                 lim=sql.Literal(_STATS_MAX_GROUPS + 1))
        async with conn.cursor() as cur:
            await cur.execute(q, params)
            fetched = await cur.fetchall()
        truncated = len(fetched) > _STATS_MAX_GROUPS
        rows = [
            {"group": _stats_value(r[0]), "value": _stats_value(r[1])}
            for r in fetched[:_STATS_MAX_GROUPS]
        ]
    return {
        "layer_id": layer_id,
        "agg": agg,
        "field": field,
        "groupBy": group_by,
        "rows": rows,
        "truncated": truncated,
    }


@router.get(
    "/layers/{layer_id}/export",
    summary="Export filtered features as CSV / GeoJSON / Shapefile / GeoPackage, optionally reprojected (&srid) — public-capable",
)
async def export_features(
    layer_id: str,
    format: str = Query(..., pattern="^(csv|geojson|shapefile|gpkg)$", description="csv | geojson | shapefile | gpkg"),
    bbox: str | None = Query(None),
    filters: list[str] = Query(default=[], alias="filter"),
    sort: str | None = Query(None),
    feature_ids: list[str] = Query(
        default=[], alias="id", description="Repeatable id to export only selected rows"
    ),
    srid: int | None = Query(
        None, description="Output CRS (EPSG) to reproject geometry into; default = the layer's storage CRS. "
                          "GeoJSON is always WGS-84 (4326). decision-working-crs-reproject.md E1."
    ),
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> Response:
    """Export the **full filtered set** (no paging), honoring the same filters/sort as the
    table — plus an optional explicit id set (export-selected). CSV carries the raw columns
    (codes) + resolved editor names + WKT geometry; GeoJSON is the raw `FeatureCollection`
    (codes); Shapefile is a zipped .shp/.shx/.dbf/.prj (EPSG:4326, geometry layers only). Each
    downloads as an attachment.

    Optional-auth (Talus 33 — the first anon `/export` surface, opened narrowly for Open Data
    downloads). Authenticated → view_allowed (403). ANONYMOUS → public_shared only (404 — existence-
    hiding); the anon export SCRUBS editor identity + AI provenance in every format (reusing
    `_ANON_SCRUB_COLUMNS`, the same scrub as `/features`) and rides a tight per-IP **download** rate
    class (ratelimit.py). Both branches fail closed (503 on OpenFGA error); the 250k cap is unchanged."""
    await _require_registered(conn, layer_id)
    is_anon = await _authorize_read(conn, principal, layer_id)
    # Stale-content signal (throttled, best-effort) — BEFORE the export work so its commit never lands
    # mid-stream. The export queries run in a fresh transaction on the same connection.
    await last_accessed.touch(conn, "layer", layer_id)

    cols = await _columns(conn, layer_id)
    if not cols:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    valid = set(cols)
    props_fields = await _props_fields(conn, layer_id)
    where_sql, params = _where(
        filters, bbox, feature_ids, valid, props_fields,
        storage_srid=(await _geom_srid(conn, layer_id) or 4326) if bbox is not None else 4326,
    )
    tbl = _assets_table(layer_id)
    order, order_params = _order_by(sort, valid, props_fields)
    params = {**params, **order_params}  # the export helpers execute where+order with one params dict
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")

    # Output-CRS reproject (E1): out_srid = the requested CRS (validated) or the layer's storage CRS. The
    # geometry is ST_Transform'd to out_srid at the edge — storage is untouched. GeoJSON is always WGS-84.
    if srid is not None:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM public.spatial_ref_sys WHERE srid = %s", (srid,))
            if await cur.fetchone() is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"srid {srid} is not a known coordinate system",
                )
        if format == "geojson" and srid != 4326:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="GeoJSON is always WGS-84 (4326); use gpkg or shapefile to reproject to another CRS",
            )
    out_srid = srid if srid is not None else await _geom_srid(conn, layer_id)

    if format == "geojson":
        return await _export_geojson(conn, layer_id, tbl, cols, where_sql, params, order, stamp, is_anon)
    if format == "shapefile":
        return await _export_shapefile(conn, layer_id, tbl, cols, valid, where_sql, params, order, stamp, is_anon, out_srid)
    if format == "gpkg":
        return await _export_geopackage(conn, layer_id, tbl, cols, valid, where_sql, params, order, stamp, is_anon, out_srid)
    return await _export_csv(conn, layer_id, tbl, cols, valid, where_sql, params, order, stamp, is_anon, out_srid)


async def _export_geojson(conn, layer_id, tbl, cols, where_sql, params, order, stamp, is_anon) -> Response:
    """GeoJSON FeatureCollection of the filtered set — same shape as the paged query, minus
    paging. Properties are raw columns (codes) + resolved editor names; geometry from geom.
    Anon scrub (Talus 33): the public surface omits editor identity + AI provenance — both the raw
    audit columns and the resolved-name joins — exactly as `/features` does."""
    valid = set(cols)
    select_items: list[sql.Composed] = []
    joins: list[sql.Composed] = []
    for name in cols:
        if name == "geom":
            continue
        if is_anon and name in _ANON_SCRUB_COLUMNS:
            continue
        if name == "id":
            select_items.append(sql.SQL("{c}::text AS {a}").format(c=_col("id"), a=sql.Identifier("id")))
        else:
            select_items.append(sql.SQL("{c}").format(c=_col(name)))
    if "geom" in valid:
        # GeoJSON export is WGS-84 by RFC 7946 — reproject native→4326 (no-op for canonical layers).
        # 15 decimal digits, matching the feature reads (§3.2) — an export shouldn't lose precision either.
        select_items.append(sql.SQL("ST_AsGeoJSON(ST_Transform(t.geom, 4326), 15)::jsonb AS geometry"))
    if not is_anon and "created_by" in valid:
        joins.append(sql.SQL("LEFT JOIN platform.users cu ON t.created_by = cu.id"))
        select_items.append(sql.SQL("COALESCE(cu.display_name, cu.username, cu.email) AS created_by_name"))
    if not is_anon and "updated_by" in valid:
        joins.append(sql.SQL("LEFT JOIN platform.users uu ON t.updated_by = uu.id"))
        select_items.append(sql.SQL("COALESCE(uu.display_name, uu.username, uu.email) AS updated_by_name"))

    q = sql.SQL("SELECT {sel} FROM {tbl} AS t {joins} WHERE {where} {order} LIMIT {lim}").format(
        sel=sql.SQL(", ").join(select_items),
        tbl=tbl,
        joins=sql.SQL(" ").join(joins) if joins else sql.SQL(""),
        where=where_sql,
        order=order,
        lim=sql.Placeholder("_cap"),
    )
    rows = await _fetch_export_rows(conn, q, {**params, "_cap": _EXPORT_CAP}, layer_id=layer_id, fmt="geojson")
    meta = _truncation_meta(len(rows))
    if meta:
        log.warning("export_capped", layer_id=layer_id, cap=_EXPORT_CAP, fmt="geojson")

    features = []
    for row in rows:
        fid = row.pop("id", None)
        geometry = row.pop("geometry", None)
        features.append({"type": "Feature", "id": fid, "geometry": geometry, "properties": row})
    payload: dict[str, Any] = {"type": "FeatureCollection", "features": features}
    if meta:
        # RFC 7946 foreign members — permitted, ignored by parsers that don't know them, and
        # they keep the truth attached to the FILE once it leaves the download context.
        payload["talus:truncated"] = True
        payload["talus:returned"] = meta["returned"]
        payload["talus:cap"] = meta["cap"]
    return JSONResponse(
        content=jsonable_encoder(payload),
        media_type="application/geo+json",
        headers=_export_headers(f"{layer_id}-{stamp}", "geojson", meta),
    )


async def _export_csv(conn, layer_id, tbl, cols, valid, where_sql, params, order, stamp, is_anon, out_srid=None) -> Response:
    """CSV of the filtered set: the raw columns (coded fields as codes); geometry is WKT;
    created_by/updated_by add resolved name columns. Anon scrub (Talus 33): the public
    surface omits editor identity + AI provenance (raw audit columns AND resolved names)."""
    select_items: list[sql.Composed] = []
    joins: list[sql.Composed] = []
    out_cols: list[str] = []  # output column order == CSV header order

    if "id" in valid:
        select_items.append(sql.SQL("{c}::text AS {a}").format(c=_col("id"), a=sql.Identifier("id")))
        out_cols.append("id")
    di = 0
    for name in cols:
        if name in ("geom", "id"):
            continue
        if is_anon and name in _ANON_SCRUB_COLUMNS:
            continue
        select_items.append(sql.SQL("{c}").format(c=_col(name)))
        out_cols.append(name)
    if "geom" in valid:
        # WKT in the OUTPUT CRS (out_srid = requested CRS or storage CRS; no-op ST_Transform when equal). E1.
        select_items.append(sql.SQL("ST_AsText(ST_Transform(t.geom, %(_out_srid)s)) AS geometry_wkt"))
        out_cols.append("geometry_wkt")
    if not is_anon and "created_by" in valid:
        joins.append(sql.SQL("LEFT JOIN platform.users cu ON t.created_by = cu.id"))
        select_items.append(sql.SQL("COALESCE(cu.display_name, cu.username, cu.email) AS created_by_name"))
        out_cols.append("created_by_name")
    if not is_anon and "updated_by" in valid:
        joins.append(sql.SQL("LEFT JOIN platform.users uu ON t.updated_by = uu.id"))
        select_items.append(sql.SQL("COALESCE(uu.display_name, uu.username, uu.email) AS updated_by_name"))
        out_cols.append("updated_by_name")

    q = sql.SQL("SELECT {sel} FROM {tbl} AS t {joins} WHERE {where} {order} LIMIT {lim}").format(
        sel=sql.SQL(", ").join(select_items),
        tbl=tbl,
        joins=sql.SQL(" ").join(joins) if joins else sql.SQL(""),
        where=where_sql,
        order=order,
        lim=sql.Placeholder("_cap"),
    )
    rows = await _fetch_export_rows(conn, q, {**params, "_cap": _EXPORT_CAP, "_out_srid": out_srid},
                                    layer_id=layer_id, fmt="csv")
    meta = _truncation_meta(len(rows))
    if meta:
        log.warning("export_capped", layer_id=layer_id, cap=_EXPORT_CAP, fmt="csv")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(out_cols)
    for row in rows:
        writer.writerow([_csv_cell(row.get(c)) for c in out_cols])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers=_export_headers(f"{layer_id}-{stamp}", "csv", meta),
    )


# --- Shapefile export (Talus 33) ------------------------------------------------
#
# A zipped ESRI Shapefile (.shp/.shx/.dbf/.prj/.cpg) of the filtered set, geometry from `ST_AsGeoJSON`
# in the OUTPUT CRS (the requested &srid or the storage CRS — the .prj follows; see _prj_for_srid).
# Pure-Python via `pyshp` (no GDAL). Geometry-only by format —
# a geometry-less hosted table can't be a shapefile (422). Attributes are the raw columns (codes),
# anon-scrubbed; all written as DBF character fields (the universal, lossless-as-text choice that
# sidesteps numeric-precision/overflow traps). Same 250k cap as CSV/GeoJSON.

# EPSG:4326 as the ESRI WKT a .prj expects (GIS clients read this to place the data).
_PRJ_WGS84 = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["Degree",0.017453292519943295]]'
)
_DBF_MAX_FIELD = 10   # ESRI .dbf field names are capped at 10 chars
_DBF_MAX_TEXT = 254   # .dbf character field max width


async def _geom_srid(conn, layer_id: str) -> int | None:
    """The layer's STORAGE geometry srid from geometry_columns (NOT working_srid: a default layer may carry a
    non-4326 working CRS while still storing 4326). None if the layer has no geometry column. Used to default
    the export OUTPUT srid (E1) and as the no-op ST_Transform target when no reproject is requested."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT srid FROM geometry_columns "
            "WHERE f_table_schema='assets' AND f_table_name=%s AND f_geometry_column='geom'",
            (layer_id,),
        )
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] else None


async def _prj_for_srid(conn, srid: int) -> str:
    """The ESRI .prj WKT for an OUTPUT srid (the shapefile's CRS). 4326 uses the curated ESRI GCS form; any
    other srid pulls spatial_ref_sys.srtext. The output srid = the layer's storage CRS by default, or the
    export's requested CRS (E1, decision-working-crs-reproject.md). Falls back to WGS-84."""
    if int(srid) == 4326:
        return _PRJ_WGS84
    async with conn.cursor() as cur:
        await cur.execute("SELECT srtext FROM public.spatial_ref_sys WHERE srid=%s", (int(srid),))
        prow = await cur.fetchone()
    return prow[0] if prow and prow[0] else _PRJ_WGS84


def _dbf_field_names(names: list[str]) -> dict[str, str]:
    """Map each output column to a unique <=10-char DBF field name (suffix on collision), per the
    shapefile spec — e.g. 'source_create_date'/'source_creator' both truncate to 'source_cre' and
    must be disambiguated ('source_cre' / 'source_cr1')."""
    used: set[str] = set()
    out: dict[str, str] = {}
    for name in names:
        base = name[:_DBF_MAX_FIELD]
        candidate = base
        i = 1
        while candidate in used:
            suffix = str(i)
            candidate = base[: _DBF_MAX_FIELD - len(suffix)] + suffix
            i += 1
        used.add(candidate)
        out[name] = candidate
    return out


def _shp_type_for(geojson_type: str | None):
    """The single shapefile geometry type for a GeoJSON type (a shapefile holds one). Returns None
    for an unknown/missing type (the writer then defaults to POINT)."""
    import shapefile  # lazy: pyshp is a Tier-2 dep, only the shapefile format needs it

    return {
        "Point": shapefile.POINT,
        "MultiPoint": shapefile.MULTIPOINT,
        "LineString": shapefile.POLYLINE,
        "MultiLineString": shapefile.POLYLINE,
        "Polygon": shapefile.POLYGON,
        "MultiPolygon": shapefile.POLYGON,
    }.get(geojson_type or "")


def _write_shape(w, geom: dict | None) -> None:
    """Write one feature's geometry to the pyshp writer from its GeoJSON. A null geometry becomes a
    null shape (valid in any shapefile). MultiPolygon flattens to all rings (shapefiles have no
    multipolygon — every ring rides one record)."""
    if not geom:
        w.null()
        return
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point":
        w.point(coords[0], coords[1])
    elif gtype == "MultiPoint":
        w.multipoint(coords)
    elif gtype == "LineString":
        w.line([coords])
    elif gtype == "MultiLineString":
        w.line(coords)
    elif gtype == "Polygon":
        w.poly(coords)
    elif gtype == "MultiPolygon":
        w.poly([ring for poly in coords for ring in poly])
    else:
        w.null()


async def _export_shapefile(conn, layer_id, tbl, cols, valid, where_sql, params, order, stamp, is_anon, out_srid) -> Response:
    """Zipped Shapefile of the filtered set (geometry layers only). Geometry ST_Transform'd to out_srid (the
    requested output CRS, or the layer's storage CRS — a no-op when equal); attributes are the raw columns
    (anon-scrubbed) as DBF text fields; the .prj carries out_srid (decision-working-crs-reproject.md E1)."""
    if "geom" not in valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="layer has no geometry — Shapefile unavailable (use CSV)",
        )
    import shapefile  # lazy: only this path needs pyshp (keeps the module importable without it)

    # SELECT: id + the property columns (raw codes, anon-scrubbed) + geometry as GeoJSON.
    select_items: list[sql.Composed] = []
    out_cols: list[str] = []
    if "id" in valid:
        select_items.append(sql.SQL("{c}::text AS {a}").format(c=_col("id"), a=sql.Identifier("id")))
        out_cols.append("id")
    for name in cols:
        if name in ("geom", "id"):
            continue
        if is_anon and name in _ANON_SCRUB_COLUMNS:
            continue
        select_items.append(sql.SQL("{c}").format(c=_col(name)))
        out_cols.append(name)
    select_items.append(sql.SQL("ST_AsGeoJSON(ST_Transform(t.geom, %(_out_srid)s))::jsonb AS geometry"))

    q = sql.SQL("SELECT {sel} FROM {tbl} AS t WHERE {where} {order} LIMIT {lim}").format(
        sel=sql.SQL(", ").join(select_items), tbl=tbl, where=where_sql, order=order,
        lim=sql.Placeholder("_cap"),
    )
    rows = await _fetch_export_rows(conn, q, {**params, "_cap": _EXPORT_CAP, "_out_srid": out_srid},
                                    layer_id=layer_id, fmt="shapefile")
    meta = _truncation_meta(len(rows))
    if meta:
        log.warning("export_capped", layer_id=layer_id, cap=_EXPORT_CAP, fmt="shapefile")

    # The geometry above (ST_AsGeoJSON, no transform) is in the layer's STORAGE CRS — native coords for a
    # native layer, 4326 for a default one — so the .prj must match that CRS (not a constant 4326).
    prj = await _prj_for_srid(conn, out_srid)

    # Shape type from the first non-null geometry (a shapefile holds one); default POINT if none.
    shape_type = next(
        (t for t in (_shp_type_for((r.get("geometry") or {}).get("type")) for r in rows) if t is not None),
        shapefile.POINT,
    )
    dbf_names = _dbf_field_names(out_cols)

    shp_io, shx_io, dbf_io = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp_io, shx=shx_io, dbf=dbf_io, shapeType=shape_type, autoBalance=1)
    for name in out_cols:
        w.field(dbf_names[name], "C", size=_DBF_MAX_TEXT)
    for row in rows:
        _write_shape(w, row.get("geometry"))
        w.record(*[_csv_cell(row.get(c))[:_DBF_MAX_TEXT] for c in out_cols])
    w.close()

    zip_io = io.BytesIO()
    with zipfile.ZipFile(zip_io, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{layer_id}.shp", shp_io.getvalue())
        zf.writestr(f"{layer_id}.shx", shx_io.getvalue())
        zf.writestr(f"{layer_id}.dbf", dbf_io.getvalue())
        zf.writestr(f"{layer_id}.prj", prj)
        zf.writestr(f"{layer_id}.cpg", "UTF-8")  # declare the .dbf text encoding
        if meta:
            # The zip is the one export format that already ships a container — put the
            # warning INSIDE it, so it survives the file being moved off the machine.
            zf.writestr(
                "TRUNCATED-README.txt",
                f"INCOMPLETE EXPORT\n\n"
                f"This shapefile contains {meta['returned']} features — the server's per-export\n"
                f"ceiling of {meta['cap']}. The source layer has MORE rows than this file.\n\n"
                f"To get the rest: narrow the export with a filter or bounding box and export\n"
                f"again, or ask your Talus administrator about a bulk export.\n",
            )
    return Response(
        content=zip_io.getvalue(),
        media_type="application/zip",
        headers=_export_headers(f"{layer_id}-{stamp}", "zip", meta),
    )


# --- GeoPackage feature export (decision-gpkg-feature-export.md) -----------------------------------
# The exact, modern vector deliverable: a valid OGC GeoPackage (1.3) of the filtered set, geometry in the
# layer's STORAGE CRS (native for a native-storage layer, 4326 for default — ST_AsBinary, no transform). The
# OGC scaffolding mirrors the empty-schema gpkg export (layer_fields._build_schema_gpkg); the new bit is the
# GeoPackage Binary (GPB) geometry blob. Dependency-free: hand-rolled via stdlib sqlite3 (no GDAL/OGR).
_GPKG_APPLICATION_ID = 0x47504B47  # 'GPKG'
_GPKG_USER_VERSION = 10300         # GeoPackage 1.3


def _gpb(srid: int, wkb: bytes) -> bytes:
    """Wrap little-endian WKB in a GeoPackage Binary blob: 'GP' + version(0) + flags(0x01: LE header,
    no envelope, non-empty, standard) + srs_id(int32 LE) + WKB. Matches ST_AsBinary(geom,'NDR')."""
    return b"GP" + bytes([0x00, 0x01]) + struct.pack("<i", int(srid)) + bytes(wkb)


async def _geom_meta_for(conn, layer_id: str, out_srid: int) -> tuple[int, str, str]:
    """(out_srid, geometry_type_name, srs_definition) for the gpkg core tables. The geometry TYPE comes from
    the layer's storage geometry_columns (reprojection preserves type — a point stays a point); the srs
    definition is for the OUTPUT srid (spatial_ref_sys.srtext). E1 (decision-working-crs-reproject.md)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT type FROM geometry_columns "
            "WHERE f_table_schema='assets' AND f_table_name=%s AND f_geometry_column='geom'",
            (layer_id,),
        )
        row = await cur.fetchone()
        geom_type = (row[0] if row and row[0] else "POINT") or "POINT"
        await cur.execute("SELECT srtext FROM public.spatial_ref_sys WHERE srid=%s", (int(out_srid),))
        srow = await cur.fetchone()
    srs_def = srow[0] if srow and srow[0] else _PRJ_WGS84
    return int(out_srid), geom_type, srs_def


async def _export_geopackage(conn, layer_id, tbl, cols, valid, where_sql, params, order, stamp, is_anon, out_srid) -> Response:
    """Valid single-feature-table OGC GeoPackage of the filtered set, geometry ST_Transform'd to out_srid (the
    requested output CRS or the storage CRS — a no-op when equal). Attributes are the raw columns
    (anon-scrubbed) as TEXT with FULL field names (the gpkg win over the Shapefile's 10-char DBF)."""
    if "geom" not in valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="layer has no geometry — GeoPackage unavailable (use CSV)",
        )
    srid, geom_type, srs_def = await _geom_meta_for(conn, layer_id, out_srid)

    # SELECT: id + property columns (raw, anon-scrubbed) as text + the geometry as little-endian WKB.
    select_items: list[sql.Composed] = []
    out_cols: list[str] = []
    if "id" in valid:
        select_items.append(sql.SQL("{c}::text AS {a}").format(c=_col("id"), a=sql.Identifier("id")))
        out_cols.append("id")
    for name in cols:
        if name in ("geom", "id"):
            continue
        if is_anon and name in _ANON_SCRUB_COLUMNS:
            continue
        select_items.append(sql.SQL("{c}").format(c=_col(name)))
        out_cols.append(name)
    select_items.append(sql.SQL("ST_AsBinary(ST_Transform(t.geom, %(_out_srid)s), 'NDR') AS _wkb"))

    q = sql.SQL("SELECT {sel} FROM {tbl} AS t WHERE {where} {order} LIMIT {lim}").format(
        sel=sql.SQL(", ").join(select_items), tbl=tbl, where=where_sql, order=order,
        lim=sql.Placeholder("_cap"),
    )
    rows = await _fetch_export_rows(conn, q, {**params, "_cap": _EXPORT_CAP, "_out_srid": out_srid},
                                    layer_id=layer_id, fmt="gpkg")
    async with conn.cursor(row_factory=dict_row) as cur:
        # Extent (in the OUTPUT CRS) for gpkg_contents — one aggregate over the same filtered set.
        await cur.execute(
            sql.SQL("SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) "
                    "FROM (SELECT ST_Extent(ST_Transform(t.geom, %(_out_srid)s)) AS e FROM {tbl} AS t WHERE {where}) s").format(
                tbl=tbl, where=where_sql),
            {**params, "_out_srid": out_srid},
        )
        ext = await cur.fetchone()
    meta = _truncation_meta(len(rows))
    if meta:
        log.warning("export_capped", layer_id=layer_id, cap=_EXPORT_CAP, fmt="gpkg")
    min_x, min_y, max_x, max_y = (ext["st_xmin"], ext["st_ymin"], ext["st_xmax"], ext["st_ymax"]) if ext else (None, None, None, None)

    data = _build_feature_gpkg(layer_id, out_cols, rows, geom_type, srid, srs_def, (min_x, min_y, max_x, max_y))
    return Response(
        content=data,
        media_type="application/geopackage+sqlite3",
        headers=_export_headers(f"{layer_id}-{stamp}", "gpkg", meta),
    )


def _build_feature_gpkg(table, out_cols, rows, geom_type, srid, srs_def, extent) -> bytes:
    """Assemble the GeoPackage bytes in memory (sqlite3 :memory: -> serialize()). One feature table:
    fid PK + geom (GPB) + one TEXT column per attribute (full names). The required core tables + the storage
    CRS srs row make it a valid OGC GeoPackage that QGIS/GDAL read."""
    con = sqlite3.connect(":memory:")
    try:
        con.execute(f"PRAGMA application_id = {_GPKG_APPLICATION_ID}")
        con.execute(f"PRAGMA user_version = {_GPKG_USER_VERSION}")
        con.execute(
            "CREATE TABLE gpkg_spatial_ref_sys ("
            " srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY, organization TEXT NOT NULL,"
            " organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT)"
        )
        srs_rows = [
            ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", None),
            ("Undefined geographic SRS", 0, "NONE", 0, "undefined", None),
        ]
        # The storage CRS row (the feature table references it). EPSG org/code = the srid for real EPSG codes.
        srs_rows.append((f"srs_{srid}", srid, "EPSG", srid, srs_def, None))
        con.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", srs_rows)

        con.execute(
            "CREATE TABLE gpkg_contents ("
            " table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE,"
            " description TEXT DEFAULT '', last_change DATETIME NOT NULL"
            " DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),"
            " min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER)"
        )
        con.execute(
            "INSERT INTO gpkg_contents (table_name, data_type, identifier, last_change, min_x, min_y, max_x, max_y, srs_id)"
            " VALUES (?, 'features', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?, ?, ?, ?)",
            (table, table, extent[0], extent[1], extent[2], extent[3], srid),
        )
        con.execute(
            "CREATE TABLE gpkg_geometry_columns ("
            " table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,"
            " srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL,"
            " CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name))"
        )
        con.execute(
            "INSERT INTO gpkg_geometry_columns VALUES (?, 'geom', ?, ?, 0, 0)", (table, geom_type, srid)
        )

        # Feature table: fid PK + geom + one TEXT column per attribute (full field names — the gpkg win).
        col_defs = ['"fid" INTEGER PRIMARY KEY AUTOINCREMENT', f'"geom" {geom_type}']
        for name in out_cols:
            col_defs.append(f'{_q_ident(name)} TEXT')
        con.execute(f'CREATE TABLE {_q_ident(table)} (' + ", ".join(col_defs) + ")")

        ins = (f'INSERT INTO {_q_ident(table)} ("geom"'
               + "".join(f", {_q_ident(c)}" for c in out_cols)
               + ") VALUES (?" + ", ?" * len(out_cols) + ")")
        for row in rows:
            wkb = row.get("_wkb")
            geom_blob = _gpb(srid, wkb) if wkb is not None else None
            con.execute(ins, [geom_blob] + [_csv_cell(row.get(c)) for c in out_cols])

        con.commit()
        return con.serialize()
    finally:
        con.close()


def _q_ident(name: str) -> str:
    """Double-quote a SQLite identifier (defensive; column/table names are validated tokens)."""
    return '"' + str(name).replace('"', '""') + '"'


# --- layer extent (decision-add-from-file.md — the post-create zoom) --------------------------------
@router.get(
    "/layers/{layer_id}/extent",
    summary="The layer's feature extent as a WGS-84 bbox (view-gated; null when empty or non-spatial)",
)
async def layer_extent(
    layer_id: str,
    feature_ids: list[str] = Query(
        default=[], alias="id", description="Repeatable id to bound only selected rows (zoom-to-selected)"
    ),
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    """Zoom-to for a layer whose features aren't loaded client-side — the add-from-file flow zooms to a
    freshly-ingested layer before any of its tiles have rendered. Same optional-auth read gate as
    /features (authenticated → view_allowed 403; anonymous → public_shared 404; fail-closed). bbox is
    [minx, miny, maxx, maxy] in WGS-84, or null for an empty or non-spatial layer.

    Repeatable ``id`` (decision-zoom-to-selected.md F1) bounds the extent to just those rows — the
    same contract and ``_where`` ids branch as export-selected, so ids are bound params and a layer
    without an ``id`` column 422s. All ids missing → ``bbox: null`` (the client no-ops)."""
    await _require_registered(conn, layer_id)
    await _authorize_read(conn, principal, layer_id)
    cols = await _columns(conn, layer_id)
    if not cols:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    if "geom" not in cols:
        return {"layerId": layer_id, "bbox": None}  # a non-spatial table — nothing to zoom to
    where_sql, params = _where([], None, feature_ids, set(cols), await _props_fields(conn, layer_id))
    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL(
                "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) "
                "FROM (SELECT ST_Extent(ST_Transform(t.geom, 4326)) AS e "
                "FROM {tbl} AS t WHERE {where}) s"
            ).format(tbl=_assets_table(layer_id), where=where_sql),
            params,
        )
        row = await cur.fetchone()
    bbox = list(row) if row is not None and row[0] is not None else None
    return {"layerId": layer_id, "bbox": bbox}


# --- per-feature edit history (decision-edit-history-ui.md) ----------------------
#
# Surfaces the audit shadow (V003/V012 generic trigger) as a read-only, per-feature timeline —
# the product-spec Phase-2 promise. Every audited layer is a hosted `tal_*` slot (fork A's
# flat-column family retired with the pilot built-ins), so attribute diffs read the `props`
# jsonb. WHO (fork B): any authenticated principal with `can_view` — history
# is metadata of data you can already see. Anonymous is denied at the door (get_current_user) and
# this route is NEVER on a public router, so history's editor identity is never anon-reachable
# (stricter than the T23 scrub, which merely hides identity from anon on the data itself).
#
# The timeline MERGES two shadows: the feature's own `audit.<layer>_audit`
# (attribute/geometry/notes changes) AND `audit.attachments_audit` filtered to this feature
# (add/remove attachment events — recorded in a separate table since V068). Because the two have
# independent `audit_id` spaces, paging is a keyset over `changed_at` (a distinct-transaction
# timestamp per event, so ties across the merged set don't occur in practice).

_HISTORY_DEFAULT_LIMIT = 20
_HISTORY_MAX_LIMIT = 100  # enumeration ceiling


def _geom_crs(g: Any) -> int | None:
    """The EPSG code a row_to_json geometry snapshot declares in its GeoJSON `crs` block, or 4326
    when absent (PostGIS emits a 4326 geom without a named crs — the default storage CRS)."""
    if not isinstance(g, dict):
        return None
    name = (((g.get("crs") or {}).get("properties") or {}).get("name")) if g.get("crs") else None
    if not name:
        return 4326
    m = re.search(r"(\d+)$", str(name))
    return int(m.group(1)) if m else None


def _point_move_m(gb: Any, ga: Any) -> float | None:
    """Great-circle metres between two Point snapshots — a cheap 'moved ~N m' hint for the drawer.
    ONLY for Point→Point in EPSG:4326 (degrees); native-CRS or line/polygon layers omit the number
    (the change still renders 'location changed'). Units correctness > completeness here."""
    if not (isinstance(gb, dict) and isinstance(ga, dict)):
        return None
    if gb.get("type") != "Point" or ga.get("type") != "Point":
        return None
    if _geom_crs(gb) != 4326 or _geom_crs(ga) != 4326:
        return None
    cb, ca = gb.get("coordinates"), ga.get("coordinates")
    if not (isinstance(cb, list) and isinstance(ca, list) and len(cb) >= 2 and len(ca) >= 2):
        return None
    try:
        lon1, lat1, lon2, lat2 = float(cb[0]), float(cb[1]), float(ca[0]), float(ca[1])
    except (TypeError, ValueError):
        return None
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def _diff_changes(before: Any, after: Any) -> list[dict[str, Any]]:
    """The changed-field delta between two row snapshots (UPDATE only). Server sends RAW keys +
    old/new values (fork D); the client resolves field labels + coded-value labels from the
    columns/domains maps the drawer already holds. Every layer with a shadow is a hosted slot
    (views have none — decision-retire-builtins.md phase 2 removed the flat-column built-ins),
    so attributes diff INSIDE `props`, plus `notes` (the one real editable column) and a
    geometry change as one synthetic `__geometry__` entry."""
    before = before or {}
    after = after or {}
    changes: list[dict[str, Any]] = []

    b_attrs = before.get("props") or {}
    a_attrs = after.get("props") or {}
    for k in sorted(set(b_attrs) | set(a_attrs)):
        if b_attrs.get(k) != a_attrs.get(k):
            changes.append({"field": k, "old": b_attrs.get(k), "new": a_attrs.get(k)})
    if before.get("notes") != after.get("notes"):
        changes.append({"field": "notes", "old": before.get("notes"), "new": after.get("notes")})

    if before.get("geom") != after.get("geom"):
        gch: dict[str, Any] = {"field": "__geometry__", "kind": "geometry"}
        moved = _point_move_m(before.get("geom"), after.get("geom"))
        if moved is not None:
            gch["moved_m"] = moved
        changes.append(gch)
    return changes


async def _has_audit_shadow(conn: AsyncConnection, layer_id: str) -> bool:
    """Whether audit.<layer_id>_audit exists as a base table. A layer VIEW (V038) is a registered
    platform.layers row but its assets object is a CREATE VIEW with NO shadow — it has no edit
    history of its own (edits happen on the source layer), so the route returns an empty timeline
    rather than 500ing on a missing table."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'audit' AND table_name = %s AND table_type = 'BASE TABLE'",
            (f"{layer_id}_audit",),
        )
        return await cur.fetchone() is not None


@router.get(
    "/layers/{layer_id}/features/{fid}/history",
    summary="A feature's edit history — who/when/what changed (view-gated; anonymous denied)",
)
async def feature_history(
    layer_id: str,
    fid: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = Query(_HISTORY_DEFAULT_LIMIT, ge=1, le=_HISTORY_MAX_LIMIT),
    cursor: str | None = Query(None, description="Return entries older than this ISO changed_at (keyset paging)"),
) -> dict[str, Any]:
    """Newest-first, keyset-paged history for one feature. Auth is MANDATORY (anon is denied here,
    even for a public layer — history is identity-bearing), then `view_allowed` (403 on no-view;
    503 fail-closed on OpenFGA down). Both shadows are scoped to THIS feature by bound `fid` (and
    the attachments side additionally by `layer_id`), so another layer's feature resolves to nothing
    (no cross-feature leak). Each entry: when, who (display name), actor_type, `via` (client), a
    `kind` ('feature' | 'attachment'), and either a changed-field diff (feature UPDATE) or an
    `attachment` filename (add/remove). INSERT/DELETE feature rows carry an empty `changes`."""
    await _require_registered(conn, layer_id)
    if not await view_allowed(user, layer_id):
        log.info(
            "authz_rejected", capability=f"{layer_id}:view",
            reason="no_view_access", user_id=str(user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"not authorized to view {layer_id}"
        )
    if not await _has_audit_shadow(conn, layer_id):
        return {"entries": [], "next_cursor": None}

    audit_tbl = sql.Identifier("audit", f"{layer_id}_audit")  # safe: layer_id proven a registry row
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            sql.SQL(
                "WITH ev AS ("
                "  SELECT a.changed_at, a.audit_id, 'feature' AS kind, a.operation, a.changed_by, "
                "         a.actor_type, a.client_info, a.before_row, a.after_row, NULL::text AS filename "
                "  FROM {tbl} AS a WHERE a.asset_id = %(fid)s::uuid "
                "  UNION ALL "
                "  SELECT t.changed_at, t.audit_id, 'attachment' AS kind, t.operation, t.changed_by, "
                "         t.actor_type, t.client_info, NULL::jsonb, NULL::jsonb, "
                "         COALESCE(t.after_row->>'filename', t.before_row->>'filename') AS filename "
                "  FROM audit.attachments_audit AS t "
                "  WHERE COALESCE(t.after_row->>'feature_id', t.before_row->>'feature_id') = %(fid_text)s "
                "    AND COALESCE(t.after_row->>'layer_id', t.before_row->>'layer_id') = %(layer_id)s "
                ") "
                "SELECT ev.changed_at, ev.audit_id, ev.kind, ev.operation, ev.changed_by, "
                "       ev.actor_type, ev.client_info, ev.before_row, ev.after_row, ev.filename, "
                "       COALESCE(u.display_name, u.username, u.email) AS actor_name "
                "FROM ev LEFT JOIN platform.users u ON u.id = ev.changed_by "
                "WHERE (%(cursor)s::timestamptz IS NULL OR ev.changed_at < %(cursor)s::timestamptz) "
                "ORDER BY ev.changed_at DESC, ev.audit_id DESC LIMIT %(limit)s"
            ).format(tbl=audit_tbl),
            {"fid": str(fid), "fid_text": str(fid), "layer_id": layer_id, "cursor": cursor, "limit": limit},
        )
        rows = await cur.fetchall()

    entries: list[dict[str, Any]] = []
    for r in rows:
        op = r["operation"]
        kind = r["kind"]
        ci = r["client_info"] or {}
        entry: dict[str, Any] = {
            "audit_id": r["audit_id"],
            "kind": kind,
            "at": r["changed_at"].isoformat() if r["changed_at"] else None,
            "operation": op,
            "actor": {
                "id": str(r["changed_by"]) if r["changed_by"] else None,
                "name": r["actor_name"],
            },
            "actor_type": r["actor_type"],
            "via": ci.get("client"),
            "changes": (
                _diff_changes(r["before_row"], r["after_row"])
                if kind == "feature" and op == "UPDATE"
                else []
            ),
        }
        if kind == "attachment":
            entry["attachment"] = {"filename": r["filename"]}
        entries.append(entry)
    # A full page MAY have more; a short page is the end. The cursor is the oldest entry's changed_at.
    next_cursor = (
        rows[-1]["changed_at"].isoformat() if len(rows) == limit and rows[-1]["changed_at"] else None
    )
    return {"entries": entries, "next_cursor": next_cursor}


# ---- Talus Field f4 (decision-talus-field-f4.md §4.5): the changes feed — the pull half of sync -----
#
# A pack is a frozen snapshot; this door lets a phone ask "what changed in this layer since my
# watermark?" and patch its packed features without a rebuild. The watermark is `audit_id` in the
# layer's own audit shadow (V059: a monotonic identity, INSERT/UPDATE/DELETE, one row per change), so a
# delete is a real tombstone and a refresh is bounded and resumable (`hasMore` + `nextWatermark`).
# Rows come back through `feature_page` — the SAME read the feature door serves, view-gated the same
# way (a PAT rides the optional-auth seam as on every read), so the feed can never show a caller a row
# the feature door would not. Nothing here is Field-specific: any client with a watermark may use it.

_CHANGES_DEFAULT = 2000
_CHANGES_MAX = 5000


@router.get(
    "/layers/{layer_id}/changes",
    summary="Rows changed since an audit watermark (upserts + delete tombstones) — view-gated",
)
async def layer_changes(
    layer_id: str,
    since: int = Query(0, ge=0, description="The audit_id watermark; 0 = everything the shadow holds"),
    bbox: str | None = Query(None, description="EPSG:4326 bounds — rows now outside it are reported in `outside`"),
    limit: int = Query(_CHANGES_DEFAULT, ge=1, le=_CHANGES_MAX),
    count_only: bool = Query(False, alias="count", description="true = only how many rows changed since the watermark"),
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    await _require_registered(conn, layer_id)
    is_anon = await _authorize_read(conn, principal, layer_id)
    empty = {"since": since, "nextWatermark": since, "changes": 0, "upserts": [], "deletes": [], "outside": [], "hasMore": False}
    if not await _has_audit_shadow(conn, layer_id):
        return empty  # a view layer has no shadow of its own — nothing to report
    audit_tbl = sql.Identifier("audit", f"{layer_id}_audit")  # safe: layer_id proven a registry row
    if count_only:
        async with conn.cursor() as cur:
            await cur.execute(
                sql.SQL("SELECT count(DISTINCT asset_id) FROM {t} WHERE audit_id > %s").format(t=audit_tbl), (since,)
            )
            n = int((await cur.fetchone())[0])
        return {**empty, "changes": n}
    # The LATEST operation per row since the watermark, oldest-latest first, one page + 1 for hasMore.
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            sql.SQL(
                "SELECT asset_id::text AS id, operation, audit_id FROM ("
                "  SELECT DISTINCT ON (asset_id) asset_id, operation, audit_id FROM {t} "
                "  WHERE audit_id > %s ORDER BY asset_id, audit_id DESC"
                ") latest ORDER BY audit_id LIMIT %s"
            ).format(t=audit_tbl),
            (since, limit + 1),
        )
        rows = list(await cur.fetchall())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_wm = int(rows[-1]["audit_id"]) if rows else since
    deletes = [r["id"] for r in rows if r["operation"] == "DELETE"]
    upsert_ids = [r["id"] for r in rows if r["operation"] != "DELETE"]
    upserts: list[dict[str, Any]] = []
    if upsert_ids:
        _total, upserts = await feature_page(
            conn, layer_id, is_anon, filters=[], bbox=bbox, feature_ids=upsert_ids,
            sort="objectid:asc", limit=max(1, min(len(upsert_ids), _MAX_LIMIT)), offset=0,
        )
    returned = {str(f.get("id")) for f in upserts}
    outside = [i for i in upsert_ids if i not in returned]  # changed, but no longer inside the bbox (or gone)
    return {
        "since": since, "nextWatermark": next_wm, "changes": len(rows),
        "upserts": upserts, "deletes": deletes, "outside": outside, "hasMore": has_more,
    }
