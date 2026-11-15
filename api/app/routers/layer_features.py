"""Generic feature-CRUD for publisher slot layers (assets.tal_<8hex>, V019).

A claimed slot has a lean, fixed schema — id/geom/edit-tracking/ai_provenance/notes,
NO other attribute columns — so one generic router gives every slot the same
add/fetch/edit/move/delete behaviour from a single code path.

Injection-surface discipline (the reason this file exists at all): every route takes
`layer_id` from the URL — user input that will NAME A TABLE. The codebase never derives a
SQL identifier from raw input (V019, publisher.py, tiles.py). Three independent guards run
BEFORE `layer_id` is ever interpolated, in order:

  1. REGEX     — layer_id must fullmatch ^tal_[0-9a-f]{8}$ (slots ONLY; a `vw_*` view layer
                 is read-only by design — its edits happen on the source slot — so it 404s
                 here, like everything else non-slot).
  2. REGISTRY  — layer_id must exist in platform.layers with status='ready'
                 (404 if absent/not a slot; 409 "layer not ready" if it exists but isn't ready).
  3. CAPABILITY— the three-key authz check, reusing app.authz.

require_capability() binds its layer at decoration time (a constant), so it cannot serve a
dynamic path param; instead each handler calls the SAME underlying view_allowed/edit_allowed
helpers require_capability uses — after the regex+registry guards. Only once layer_id has
passed regex+registry is it one of a fixed known-safe set; the table identifier is then built
with psycopg.sql.Identifier (never an f-string) as defense-in-depth, and fid is always a bound
param cast to uuid. Writes set the audit GUCs in-transaction so the slot's V019 trigger stamps
changed_by, exactly like publisher.py.
"""
import base64
import csv
import datetime
import io
import json
import math
import re
import sqlite3
import struct
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from psycopg import AsyncConnection, errors as pg_errors, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from .. import attachments as att
from .. import gpkg_guard
from ..audit_context import set_audit_context
from ..auth import _validated_claims, get_current_user, get_current_user_rw, rw_claims
from ..authz import edit_allowed, edit_deny_sentence, view_allowed
from ..concurrency import require_if_match, version_conflict_or_404
from ..db import get_db
from ..file_ingest import (
    FAMILY_BY_GEOJSON,
    INGEST_FEATURE_CAP,
    MAX_FILE_BYTES,
    csv_pair_srid,
    find_csv_coordinates,
    open_shapefile_zip,
)
from ..idempotency import lookup_applied_op, optional_idempotency_key, record_applied_op
from ..layer_props import PropsError, active_field_defs, map_props_check_violation, validate_props
from ..media import MediaStoreUnavailable, remove_object
from ..models.user import User
from .layer_fields import _sanitize_key             # the ONE source-column -> field_key derivation
from .layer_query import _columns as _layer_columns  # shared introspection + the injection-safe
from .layer_query import _geom_srid, _props_fields, _where  # scope grammar (decision-geometry-calc.md §3.1)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["layer-features"])

# Tags the audit row's client_info via the V003 trigger's app.current_client GUC.
_AUDIT_CLIENT = "portal-api"

# GUARD 1: the only layer_ids this generic router will ever touch. Slots ONLY — a `vw_*`
# view layer is read-only by design (edits happen on its source slot), so it 404s here.
_SLOT_RE = re.compile(r"^tal_[0-9a-f]{8}$")


# --- request bodies -------------------------------------------------------------


class SlotFeatureCreate(BaseModel):
    # POINT layers: DEFAULT (canonical-4326) take {lon, lat}; NATIVE-storage (decision-crs-native-slice1.md §6)
    # take {x, y} = easting/northing in the layer's CRS, stored exactly. LINE/POLYGON layers
    # (decision-line-polygon-layers.md): take a GeoJSON `geometry` (LineString/Polygon or their Multi*), whose
    # coords are already in the storage CRS (4326 default / working_srid native — no transform here). The router
    # picks by the layer's geom_type; the unused fields stay optional.
    lon: float | None = None
    lat: float | None = None
    x: float | None = None
    y: float | None = None
    geometry: dict[str, Any] | None = None  # GeoJSON geometry for line/polygon layers
    notes: str | None = None
    # Owner-defined field values (V044). Validated app-side (unknown-key/type) + the DB trigger
    # (defaults/required). `notes` stays its own special built-in field, NOT a props key.
    props: dict[str, Any] | None = None


class SlotFeatureUpdate(BaseModel):
    # All optional: a POINT move sends {lon, lat} (default) or {x, y} (native); a LINE/POLYGON geometry edit
    # sends a replacement GeoJSON `geometry`; an attribute edit sends {notes} and/or {props}.
    lon: float | None = None
    lat: float | None = None
    x: float | None = None
    y: float | None = None
    geometry: dict[str, Any] | None = None  # replacement GeoJSON geometry for line/polygon layers
    notes: str | None = None
    # A partial patch of owner-defined field values: merged onto the row's existing props.
    props: dict[str, Any] | None = None


# --- guards + shared helpers ----------------------------------------------------


async def _validate_slot(conn: AsyncConnection, layer_id: str) -> None:
    """GUARDS 1+2: prove `layer_id` is a slot token (regex) registered 'ready' in
    platform.layers, BEFORE it is ever used to name a table. Raises 404 (not a slot /
    unknown) or 409 ("layer not ready"). The registry query binds layer_id as a
    parameter — it is never interpolated.
    """
    if not _SLOT_RE.fullmatch(layer_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM platform.layers WHERE layer_id = %s", (layer_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    if row[0] != "ready":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="layer not ready")


async def _layer_storage(conn: AsyncConnection, layer_id: str) -> tuple[bool, int, str]:
    """(native_storage, working_srid, geom_type) for a layer. native/working_srid drive WHERE a write places
    geometry (decision-crs-native-slice1.md: native stores in working_srid exactly, default stores 4326);
    geom_type (V055) drives WHAT shape — point uses {lon,lat}/{x,y}+ST_MakePoint, line/polygon take a GeoJSON
    geometry wrapped Multi*. layer_id is bound, never interpolated. Call after _validate_slot proved the slot."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT native_storage, working_srid, geom_type FROM platform.layers WHERE layer_id = %s",
            (layer_id,),
        )
        row = await cur.fetchone()
    # _validate_slot already guaranteed the row exists; default to canonical point if somehow absent.
    return (bool(row[0]), int(row[1]), row[2] or "point") if row else (False, 4326, "point")


def _point_params(native: bool, srid: int, lon, lat, x, y) -> tuple[float, float, int]:
    """Resolve a point's (coord_x, coord_y, srid) for the INSERT/MOVE. A native layer takes {x,y} in its
    own CRS (stored exactly); a default layer takes {lon,lat} in 4326. Raises 422 if the layer's expected
    pair is missing. The srid is a bound integer in ST_SetSRID — no DDL, no interpolation."""
    if native:
        if x is None or y is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="native-storage layer requires x and y (easting/northing in the layer's CRS)",
            )
        return x, y, srid
    if lon is None or lat is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="lon and lat are required",
        )
    return lon, lat, 4326


# GeoJSON geometry families accepted per layer geom_type (decision-line-polygon-layers.md). Single-part input
# is accepted and wrapped Multi* on store; the layer never mixes families (the guard below is fail-closed).
_GEOJSON_TYPES = {
    "line": {"LineString", "MultiLineString"},
    "polygon": {"Polygon", "MultiPolygon"},
}

# A single feature's vertex ceiling — bounds the work PostGIS does per geometry so a pathologically huge
# coordinate array can't exhaust memory. 100k is generous for a real municipal line/polygon (a detailed
# parcel/main is hundreds–thousands of vertices); past this we 422 rather than ingest it.
_MAX_VERTICES = 100_000


def _validate_geojson_geometry(geom: dict) -> str | None:
    """Structural validation BEYOND PostGIS's lenient ST_GeomFromGeoJSON, which otherwise accepts an empty
    geometry, a single-point 'line', an unclosed polygon ring, and — worst — SILENTLY COERCES a non-numeric
    coordinate to 0 (a feature lands at lon 0 with no error). Returns a human error string, or None if usable.
    Checks: every position is a finite-number [x, y(,z)]; lines have ≥2 points, polygon rings ≥4 + closed;
    nothing empty; total vertices ≤ the cap. PostGIS still does the authoritative parse (+ ST_Multi/ST_Force2D)
    after this. Covers all families so it guards both single-feature create/edit AND bulk ingest (incl. point)."""
    t = geom.get("type")
    coords = geom.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) == 0:
        return "geometry has empty coordinates"
    vertices = 0

    def pos_ok(p: Any) -> bool:
        return (
            isinstance(p, (list, tuple)) and len(p) >= 2
            and all((not isinstance(v, bool)) and isinstance(v, (int, float)) and math.isfinite(v) for v in p[:2])
        )

    def check_seq(seq: Any, minimum: int, what: str) -> str | None:
        nonlocal vertices
        if not isinstance(seq, (list, tuple)) or len(seq) < minimum:
            return f"a {what} needs at least {minimum} points"
        if not all(pos_ok(p) for p in seq):
            return "coordinate values must be finite numbers"
        vertices += len(seq)
        return None

    def check_polygon(rings: Any) -> str | None:
        if not isinstance(rings, (list, tuple)) or len(rings) == 0:
            return "a polygon needs at least one ring"
        for ring in rings:
            err = check_seq(ring, 4, "polygon ring")
            if err:
                return err
            if list(ring[0][:2]) != list(ring[-1][:2]):
                return "a polygon ring must be closed (first point equals last)"
        return None

    err: str | None = None
    if t == "Point":
        err = None if pos_ok(coords) else "coordinate values must be finite numbers"
        vertices = 1
    elif t == "MultiPoint":
        err = check_seq(coords, 1, "multipoint")
    elif t == "LineString":
        err = check_seq(coords, 2, "line")
    elif t == "MultiLineString":
        for line in coords:
            err = check_seq(line, 2, "line")
            if err:
                break
    elif t == "Polygon":
        err = check_polygon(coords)
    elif t == "MultiPolygon":
        for poly in coords:
            err = check_polygon(poly)
            if err:
                break
    # other families are already rejected upstream (the family guard / adapter), so they never reach here

    if err:
        return err
    if vertices > _MAX_VERTICES:
        return f"geometry has too many vertices ({vertices} > {_MAX_VERTICES})"
    return None


def _geom_expr_and_params(
    geom_type: str, native: bool, srid: int, *, lon, lat, x, y, geometry
) -> tuple[str, dict]:
    """Build the geom column expression + its bound params for an INSERT/UPDATE, branching on the layer's
    geom_type. POINT: {lon,lat} (default) or {x,y} (native) → ST_MakePoint, byte-for-byte the prior path.
    LINE/POLYGON: a GeoJSON `geometry` whose family MUST match the layer (else 422, fail-closed), wrapped
    ST_Multi(ST_Force2D(...)) so single-part input + any Z are normalized. The client supplies coords already
    in the storage CRS (4326 default / working_srid native) — ST_SetSRID stamps, NO transform (bulk ingest is
    the reprojecting path, not this one). The returned expression carries %(...)s placeholders bound at execute."""
    if geom_type == "point":
        px, py, psrid = _point_params(native, srid, lon, lat, x, y)
        return ("ST_SetSRID(ST_MakePoint(%(px)s, %(py)s), %(psrid)s)",
                {"px": px, "py": py, "psrid": psrid})
    # line / polygon
    allowed = _GEOJSON_TYPES[geom_type]
    if (not isinstance(geometry, dict) or "type" not in geometry or "coordinates" not in geometry):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"this {geom_type} layer requires a GeoJSON geometry ({' or '.join(sorted(allowed))})",
        )
    if geometry["type"] not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"geometry type '{geometry['type']}' does not match a {geom_type} layer",
        )
    gerr = _validate_geojson_geometry(geometry)  # reject empty/degenerate/non-numeric before PostGIS coerces it
    if gerr:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"invalid geometry: {gerr}")
    gsrid = srid if native else 4326
    return ("ST_Multi(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%(geojson)s), %(gsrid)s)))",
            {"geojson": json.dumps(geometry), "gsrid": gsrid})


async def _require_view(user: User, layer_id: str) -> None:
    """GUARD 3 (read): the view key, reusing authz.view_allowed (same as me.py)."""
    if not await view_allowed(user, layer_id):
        log.info(
            "authz_rejected",
            capability=f"{layer_id}:view",
            reason="no_view_access",
            user_id=str(user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"not authorized to view {layer_id}",
        )


async def _require_edit(
    conn: AsyncConnection, claims, user: User, layer_id: str, *, op: str | tuple[str, ...]
) -> None:
    """GUARD 3 (write): the full three-key edit decision, reusing authz.edit_allowed —
    the exact logic require_capability(..., "write") runs, replicated here because the
    layer is a dynamic path param. `op` names what this door is about to do (V109,
    decision-layer-edit-capabilities.md §3.3) — required, so no write door can forget it; a
    refused op answers 403 with the policy sentence (the UI and the field drain show it)."""
    allowed, reason = await edit_allowed(conn, claims, user, layer_id, op)
    if not allowed:
        log.info(
            "authz_rejected",
            capability=f"{layer_id}:write",
            reason=reason,
            user_id=str(user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=edit_deny_sentence(reason) or f"not authorized to edit {layer_id}",
        )


def _update_ops(fields: dict[str, Any]) -> tuple[str, ...]:
    """The op(s) a PATCH performs, from the keys it SET (§3.3): any coordinate/geometry key → `geometry`;
    notes/props → `attributes`; both when both ride along. An empty PATCH is an attributes no-op."""
    ops: list[str] = []
    if any(k in fields for k in ("lon", "lat", "x", "y", "geometry")):
        ops.append("geometry")
    if any(k in fields for k in ("notes", "props")) or not ops:
        ops.append("attributes")
    return tuple(ops)


def _slot_table(layer_id: str) -> sql.Identifier:
    """assets.<layer_id> as a safe identifier. ONLY call after _validate_slot has passed
    — defense-in-depth on an already-proven token, never an f-string of raw input."""
    return sql.Identifier("assets", layer_id)


def _slot_by_id_sql(layer_id: str, has_geom: bool = True) -> sql.Composed:
    """The GET-one read for a slot feature. `row_version::text AS version` is the optimistic-
    concurrency token (Talus 34, V035) — present on the read and re-read on a 409. `objectid`
    (V064, every slot) rides along so a single-feature read names the record the way the list door
    and the record drawer do — the identify popup's Generate-sheet icon keys on it
    (decision-scheduled-reports.md F11). ONLY call after _validate_slot has passed."""
    # Geometry is emitted as GeoJSON (RFC 7946 → WGS-84), so ST_Transform to 4326. For a default layer geom
    # is already 4326 and ST_Transform is a no-op; for a native layer it reprojects native→4326 for display.
    # An ASPATIAL hosted table (geom_type 'none', decision-nonspatial-tables.md) has NO geom column —
    # emit NULL so the response shape stays identical (geometry: null, matching /features).
    # 15 decimal digits (not the 9-digit default): a float8 coordinate round-trips bit-exact through
    # the decimal text, so a snapped vertex lands ST_Equals-identical to its target and a reshape
    # auto-commit never nudges untouched vertices (decision-web-editor-snapping.md §3.2).
    geom_expr = (
        sql.SQL("ST_AsGeoJSON(ST_Transform(geom, 4326), 15)::jsonb") if has_geom else sql.SQL("NULL::jsonb")
    )
    return sql.SQL(
        "SELECT id::text AS id, objectid, {geom} AS geometry, notes, props, "
        "created_at, updated_at, ai_provenance, row_version::text AS version "
        "FROM {tbl} WHERE id = %(fid)s::uuid"
    ).format(geom=geom_expr, tbl=_slot_table(layer_id))


# Audit-context GUCs (user_id / client / actor_type) are set via the shared
# app.audit_context.set_audit_context helper, so a service-account write stamps
# actor_type='service_account' (Talus 19, Q6) — see that module's docstring.


# --- routes ---------------------------------------------------------------------


@router.post(
    "/layers/{layer_id}/features",
    status_code=status.HTTP_201_CREATED,
    summary="Add a feature (point lon/lat·x/y, or line/polygon GeoJSON) to a claimed slot layer (editor only)",
)
async def create_feature(
    layer_id: str,
    payload: SlotFeatureCreate,
    conn: AsyncConnection = Depends(get_db),
    # Talus Field f4 (decision-talus-field-f4.md §4.2): a Bearer as before, OR a read_write field-sync
    # PAT scoped to this layer (the phone's outbox drain). `rw_claims` must follow the user dependency.
    user: User = Depends(get_current_user_rw),
    claims: dict | None = Depends(rw_claims),
    op_key: str | None = Depends(optional_idempotency_key),
) -> dict[str, str]:
    await _validate_slot(conn, layer_id)
    await _require_edit(conn, claims, user, layer_id, op="add")
    actor = user.id

    # Storage + shape: native vs canonical CRS (decision-crs-native-slice1.md §6) AND point vs line/polygon
    # (decision-line-polygon-layers.md). The helper resolves the geom expression + params and 422s a geometry
    # whose family doesn't match the layer's geom_type. srid binds into ST_SetSRID/ST_GeomFromGeoJSON (no DDL).
    native, srid, geom_type = await _layer_storage(conn, layer_id)
    # Aspatial hosted table (decision-nonspatial-tables.md): no geom column — the INSERT omits geometry entirely.
    if geom_type == "none":
        geom_expr, geom_params = None, {}
    else:
        geom_expr, geom_params = _geom_expr_and_params(
            geom_type, native, srid,
            lon=payload.lon, lat=payload.lat, x=payload.x, y=payload.y, geometry=payload.geometry,
        )

    # Offline-replay idempotency (Talus 35): a replayed clientOpId returns the already-created
    # id rather than inserting a duplicate. The client ignores create's return value, so the id
    # alone suffices (no row re-read needed). See idempotency.py.
    prior_id = await lookup_applied_op(conn, op_key, "slot_feature")
    if prior_id is not None:
        await conn.rollback()
        return {"id": prior_id}

    # Owner-defined fields (V044): app-side friendly check (unknown-key/type); the BEFORE INSERT
    # trigger then applies defaults + enforces required even when props is empty (the belt).
    props = payload.props or {}
    if props:
        try:
            validate_props(await active_field_defs(conn, layer_id), props)
        except PropsError as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.errors)

    if geom_expr is None:  # aspatial hosted table — insert notes/props only, no geom column
        query = sql.SQL(
            "INSERT INTO {tbl} (notes, props, created_by, updated_by) "
            "VALUES (%(notes)s, %(props)s, %(actor)s, %(actor)s) RETURNING id::text"
        ).format(tbl=_slot_table(layer_id))
    else:
        query = sql.SQL(
            "INSERT INTO {tbl} (geom, notes, props, created_by, updated_by) "
            "VALUES ({geom}, %(notes)s, %(props)s, %(actor)s, %(actor)s) "
            "RETURNING id::text"
        ).format(tbl=_slot_table(layer_id), geom=sql.SQL(geom_expr))

    try:
        async with conn.cursor() as cur:
            await set_audit_context(cur, user, _AUDIT_CLIENT)
            await cur.execute(
                query,
                {**geom_params, "notes": payload.notes, "props": Jsonb(props), "actor": str(actor)},
            )
            new_id = (await cur.fetchone())[0]
        await record_applied_op(conn, op_key, "slot_feature", new_id)
        await conn.commit()
    except pg_errors.CheckViolation as e:  # the props trigger rejected (defaults/required/holistic)
        await conn.rollback()
        raise map_props_check_violation(e)
    except (pg_errors.InternalError_, pg_errors.DataError) as e:  # ST_GeomFromGeoJSON rejected a bad geometry
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"invalid geometry: {str(e).splitlines()[0][:200]}")
    log.info("slot_feature_mutation", action="create", layer_id=layer_id, entity_id=new_id, actor=str(actor))
    return {"id": new_id}


# --- bulk feature ingest (E4, decision-bulk-feature-ingest.md) ------------------
#
# Upload many point features at once: validate-first DRY RUN -> per-row verdicts, then COMMIT inserts the valid
# rows. The same validate_props the single-feature path uses (so bulk can't diverge), the same per-row audit
# trigger (a plain INSERT per row, never bypassed). A thin ADAPTER per format parses the upload into uniform
# (x, y, source_srid) points + raw props; the shared core gates point-only, auto-maps attributes by field name,
# validates, and on insert ST_Transform's the source point into the layer's STORAGE CRS (E4 import-reproject-in:
# 4326 for a default layer, the native srid for a native one). Formats: GeoJSON (source 4326, RFC 7946) and
# GeoPackage (source = the gpkg's srs_id, read in-memory via sqlite3). Point-only, insert-only, auto-match.

class FeatureImport(BaseModel):
    format: str = "geojson"               # geojson | geopackage | shapefile | csv
    geojson_text: str | None = None       # format=geojson
    gpkg_base64: str | None = None        # format=geopackage (binary SQLite, base64-in-JSON)
    shp_base64: str | None = None         # format=shapefile (a zipped .shp/.dbf/.shx/.prj, base64-in-JSON)
    csv_text: str | None = None           # format=csv (lon/lat or x/y columns + attributes)
    parquet_base64: str | None = None     # format=geoparquet (columnar binary, base64-in-JSON)
    # Optional source-CRS override (EPSG). Wins over what the file declares — used when a Shapefile's .prj has no
    # AUTHORITY code and the user must declare the CRS (decision-bulk-feature-ingest.md / the .prj→srid note).
    source_srid: int | None = None
    # Optional CSV coordinate-column override (decision-add-from-file.md §2.3) — the wizard's manual pick for
    # CSVs whose column names beat the common-name auto-detect ('POINT_X', 'east', …). Both or neither.
    csv_x_column: str | None = None
    csv_y_column: str | None = None
    commit: bool = False


def _storage_srid(native: bool, working_srid: int) -> int:
    """The layer's geom-column srid: the working CRS for a native layer, else canonical 4326 (a default layer
    stores 4326 even when it carries a non-4326 working CRS)."""
    return working_srid if native else 4326


def _ingest_geom_expr(kind: str, geom_type: str) -> str:
    """The geom column expression for a bulk-ingest INSERT (decision-line-polygon-layers.md c5). The source
    geometry comes from %(g)s — a GeoJSON text (kind='geojson') or raw WKB bytes (kind='wkb', GeoPackage) —
    stamped with the source CRS %(src)s, REPROJECTED to the storage CRS %(storage)s (the reprojecting path,
    unlike single-feature create), forced 2D, and wrapped Multi* for line/polygon to match the Multi* storage
    column. Point layers keep a single Point (no ST_Multi). Placeholders bind at executemany time."""
    src = ("ST_SetSRID(ST_GeomFromGeoJSON(%(g)s), %(src)s)" if kind == "geojson"
           else "ST_SetSRID(ST_GeomFromWKB(%(g)s), %(src)s)")
    reproj = f"ST_Force2D(ST_Transform({src}, %(storage)s))"
    return f"ST_Multi({reproj})" if geom_type in ("line", "polygon") else reproj


# A parsed incoming feature, format-agnostic (decision-line-polygon-layers.md c5): the geometry as `geom`
# (a GeoJSON geometry dict for kind='geojson' adapters, or raw OGC/ISO WKB bytes for kind='wkb'), its `family`
# (point/line/polygon, for the geom_type match guard; None ⇒ unusable → per-row error), and the raw attribute
# dict. The adapter declares ONE kind for the whole upload (format is fixed per request), so the commit INSERT
# picks ST_GeomFromGeoJSON vs ST_GeomFromWKB once. Replaces the former point-only {"point": (x,y)} shape.
def _err_feat(msg: str) -> dict[str, Any]:
    return {"family": None, "geom": None, "props": {}, "error": msg}


# Geometry family by WKB base-type code (1..6); the GeoJSON-type map is shared with the schema preview
# (file_ingest.FAMILY_BY_GEOJSON). Multi* folds into its single-part family; anything else → None.
_FAMILY_BY_WKB = {1: "point", 4: "point", 2: "line", 5: "line", 3: "polygon", 6: "polygon"}


def _geojson_to_features(text: str) -> tuple[int, str, list[dict[str, Any]]]:
    """GeoJSON adapter → (4326, 'geojson', raw_features). RFC 7946 fixes the source CRS at WGS-84. Carries the
    geometry of ANY supported family (Point/LineString/Polygon + Multi*) through as a GeoJSON dict — the core
    gates the family against the layer's geom_type and PostGIS (ST_GeomFromGeoJSON) does the real parse."""
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="not valid JSON")
    if isinstance(doc, dict) and doc.get("type") == "FeatureCollection":
        feats = doc.get("features") or []
    elif isinstance(doc, dict) and doc.get("type") == "Feature":
        feats = [doc]
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="expected a GeoJSON FeatureCollection or Feature",
        )
    if not isinstance(feats, list):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="features must be a list")
    out: list[dict[str, Any]] = []
    for f in feats:
        if not isinstance(f, dict):
            out.append(_err_feat("not a GeoJSON Feature"))
            continue
        geom = f.get("geometry")
        if not isinstance(geom, dict) or "coordinates" not in geom:
            out.append(_err_feat("missing or invalid geometry"))
            continue
        family = FAMILY_BY_GEOJSON.get(geom.get("type"))
        if family is None:
            out.append(_err_feat(f"unsupported geometry type '{geom.get('type')}'"))
            continue
        gerr = _validate_geojson_geometry(geom)  # same structural guard as single-feature create (per-row)
        if gerr:
            out.append(_err_feat(f"invalid geometry: {gerr}"))
            continue
        props = f.get("properties") if isinstance(f.get("properties"), dict) else {}
        out.append({"family": family, "geom": geom, "props": props, "error": None})
    return 4326, "geojson", out


# GeoPackage Binary envelope sizes by the flags' envelope-indicator (bits 1-3): 0=none, 1=XY, 2=XYZ, 3=XYM, 4=XYZM.
_GPB_ENVELOPE_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def _gpkg_geometry(blob: bytes) -> tuple[str | None, bytes | None]:
    """Strip a GeoPackage geometry blob's GPB header to the raw OGC/ISO WKB, returning (family, wkb_bytes) — or
    (None, None) if it isn't a usable GPB. GPB: 'GP' + version + flags + srs_id(+envelope) + WKB. We read past
    the header, then read ONLY the WKB base-type code to classify the family (point/line/polygon); the full
    parse (incl. Z/M, any structure) is left to PostGIS ST_GeomFromWKB at insert time. Replaces _gpkg_point —
    no longer point-only, no hand-rolled coordinate decode (decision-line-polygon-layers.md c5)."""
    if not blob or len(blob) < 8 or blob[0:2] != b"GP":
        return (None, None)
    flags = blob[3]
    env = (flags >> 1) & 0x07
    env_bytes = _GPB_ENVELOPE_BYTES.get(env)
    if env_bytes is None:
        return (None, None)
    wkb = blob[8 + env_bytes:]
    family = _wkb_family(wkb)
    return (family, wkb) if family is not None else (None, None)


def _wkb_family(wkb: bytes | None) -> str | None:
    """Classify raw OGC/ISO WKB by its base-type code alone (point/line/polygon, Multi* folded in;
    None ⇒ unusable). Shared by the GeoPackage and GeoParquet adapters — only the FAMILY is read
    here; the full geometry parse stays PostGIS's job (ST_GeomFromWKB) at insert time."""
    if not wkb or len(wkb) < 5:
        return None
    endian = "<" if wkb[0] == 1 else ">"
    raw_type = struct.unpack(endian + "I", wkb[1:5])[0]
    base = raw_type & 0xFF            # extended-WKB (PostGIS Z/M high-bit flags) + plain 2D land here
    if base not in _FAMILY_BY_WKB:
        base = raw_type % 1000        # ISO-WKB Z/M variants (e.g. 1002 = LineString Z) → base type
    return _FAMILY_BY_WKB.get(base)


def _geopackage_to_features(b64: str) -> tuple[int, str, list[dict[str, Any]]]:
    """GeoPackage adapter → (srs_id, 'wkb', raw_features). Reads the .gpkg fully in-memory (sqlite3.deserialize —
    no temp file, no GDAL), takes the first feature table, strips each GPB geometry to raw WKB + family (any
    geometry type now, not just points), and carries the attribute columns (skipping fid + geom) as raw props.
    Source CRS = the table's gpkg_geometry_columns.srs_id; PostGIS ST_GeomFromWKB does the real geometry parse."""
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="gpkg_base64 is not valid base64")
    con = sqlite3.connect(":memory:")
    try:
        try:
            con.deserialize(data)  # py3.11+: load the bytes as the in-memory DB
        except Exception:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="not a readable GeoPackage")
        gpkg_guard.guard(con, gpkg_guard.PREVIEW_BUDGET_OPS)  # read-only, untrusting, budgeted (K1)
        gpkg_guard.require_plain_table(con, "gpkg_geometry_columns")
        gc = con.execute(
            "SELECT table_name, column_name, srs_id FROM gpkg_geometry_columns ORDER BY table_name"
        ).fetchall()
        if not gc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no feature table in the GeoPackage")
        table, geomcol, srs_id = gc[0][0], gc[0][1], int(gc[0][2])
        gpkg_guard.require_plain_table(con, table)  # a view / virtual table / generated column is refused (K1)
        qt = gpkg_guard.quote_ident(table)  # the name comes from the file: quoted, embedded quotes doubled
        info = con.execute(f"PRAGMA table_info({qt})").fetchall()
        all_cols = [r[1] for r in info]
        attr_cols = [c for c in all_cols if c != geomcol and c.lower() != "fid"]
        sel = ", ".join('"' + c.replace('"', '""') + '"' for c in [geomcol] + attr_cols)
        out: list[dict[str, Any]] = []
        for row in con.execute(f"SELECT {sel} FROM {qt}"):
            attrs = {c: row[i + 1] for i, c in enumerate(attr_cols)}
            family, wkb = _gpkg_geometry(row[0]) if isinstance(row[0], (bytes, bytearray)) else (None, None)
            if family is None:
                out.append({"family": None, "geom": None, "props": attrs, "error": "unreadable or unsupported geometry"})
            else:
                out.append({"family": family, "geom": wkb, "props": attrs, "error": None})
        return srs_id, "wkb", out
    except gpkg_guard.UnsafeGeoPackage as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except sqlite3.OperationalError as exc:
        if gpkg_guard.is_budget_stop(exc):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=gpkg_guard.TOO_COSTLY)
        raise
    finally:
        con.close()


_PARQUET_DECOMPRESSED_CEILING = 256 * 1024 * 1024  # decision-geoparquet-ingest.md F5 cap (3)


def _pq_attr_value(v: Any) -> Any:
    """An arrow cell → a props-safe python value. Scalars pass through; datetimes/dates become ISO
    strings (props is jsonb — a raw datetime would fail json encoding); anything exotic (bytes,
    nested lists/structs) becomes its string form rather than an error — attributes are the
    payload here, not a contract."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    iso = getattr(v, "isoformat", None)
    return iso() if callable(iso) else str(v)


def _geoparquet_to_features(b64: str) -> tuple[int, str, list[dict[str, Any]]]:
    """GeoParquet adapter → (srid, 'wkb', raw_features) (decision-geoparquet-ingest.md). pyarrow
    reads the table fully in-memory (BytesIO, no temp file); PostGIS does ALL geometry work — each
    cell's raw WKB rides through untouched to ST_GeomFromWKB, exactly like the GeoPackage adapter.

    Bounds against hostile columnar files (F5), all before full materialization: the 50 MB byte cap
    ran in the endpoint; the row count comes from the parquet FOOTER (bounded read) and is checked
    against INGEST_FEATURE_CAP before any data page is touched; the batchwise read then enforces a
    decompressed-bytes ceiling so a single-cell zstd bomb dies at the ceiling, not at OOM. Every
    parser failure is an honest 422 — pyarrow internals are never echoed.

    Contract fences (F3/F4): geometry encoding must be WKB (geoarrow-native → 422 naming the fix);
    CRS absent → the spec default OGC:CRS84 ≈ 4326; PROJJSON with an EPSG authority id → that code;
    any other authority → 422; no `geo` metadata at all → 422 (plain Parquet has no geometry
    contract — tables are the CSV adapter's job)."""
    import pyarrow.parquet as pq  # lazy, like pyshp — only this format pays the import

    try:
        data = base64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="parquet_base64 is not valid base64")
    try:
        pf = pq.ParquetFile(io.BytesIO(data))
        num_rows = pf.metadata.num_rows
        kv = pf.schema_arrow.metadata or {}
    except Exception:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="not a readable Parquet file")
    if num_rows > INGEST_FEATURE_CAP:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"too many features ({num_rows}); the per-upload cap is {INGEST_FEATURE_CAP}")
    geo_raw = kv.get(b"geo")
    if geo_raw is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="not a GeoParquet file (no `geo` metadata) — plain Parquet has no geometry; "
                   "for attribute tables use CSV import")
    try:
        geo = json.loads(geo_raw)
        primary = geo["primary_column"]
        col_meta = geo["columns"][primary]
    except Exception:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="unreadable GeoParquet `geo` metadata")
    encoding = str(col_meta.get("encoding", "")).upper()
    if encoding != "WKB":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"geometry encoding '{str(col_meta.get('encoding'))[:60]}' is not supported — "
                   "re-export with WKB geometry encoding")
    crs = col_meta.get("crs")
    if crs is None:
        srid = 4326  # the spec default (OGC:CRS84) — axis order lon/lat, same as GeoJSON
    else:
        crs_id = crs.get("id") if isinstance(crs, dict) else None
        authority = str((crs_id or {}).get("authority", "")).upper()
        try:
            code = int((crs_id or {}).get("code"))
        except (TypeError, ValueError):
            code = None
        if authority == "OGC" and (crs_id or {}).get("code") == "CRS84":
            srid = 4326
        elif authority == "EPSG" and code is not None:
            srid = code  # validated against spatial_ref_sys by the endpoint, like every adapter
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="the file's CRS has no EPSG code — reproject to an EPSG-coded CRS "
                       "and re-export")
    try:
        total = 0
        batches = []
        for batch in pf.iter_batches(batch_size=8192):
            total += batch.nbytes
            if total > _PARQUET_DECOMPRESSED_CEILING:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="file decompresses beyond the ingest ceiling — split it and re-import")
            batches.append(batch)
    except HTTPException:
        raise
    except MemoryError:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="file decompresses beyond the ingest ceiling")
    except Exception:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="not a readable Parquet file")
    names = [f.name for f in pf.schema_arrow]
    if primary not in names:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="the declared geometry column is missing from the table")
    attr_names = [n for n in names if n != primary and n.lower() != "fid"]
    geoms: list[Any] = []
    attrs_by_col: dict[str, list[Any]] = {n: [] for n in attr_names}
    try:
        for batch in batches:
            cols = {n: batch.column(i).to_pylist() for i, n in enumerate(names)}
            geoms.extend(cols[primary])
            for n in attr_names:
                attrs_by_col[n].extend(cols[n])
    except Exception:  # a pathological arrow value at materialization → honest 422, never a 500
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="not a readable Parquet file")
    out: list[dict[str, Any]] = []
    for i, blob in enumerate(geoms):
        props = {n: _pq_attr_value(attrs_by_col[n][i]) for n in attr_names}
        wkb = bytes(blob) if isinstance(blob, (bytes, bytearray)) else None
        family = _wkb_family(wkb)
        if family is None:
            out.append({"family": None, "geom": None, "props": props,
                        "error": "unreadable or unsupported geometry"})
        else:
            out.append({"family": family, "geom": wkb, "props": props, "error": None})
    return srid, "wkb", out


def _dbf_value(v: Any) -> Any:
    """dbf attribute → JSON-safe prop. pyshp yields REAL date objects for 'D' fields (and bytes for
    memo fields); validate_props wants the ISO string and Jsonb can't serialize a date at all — so an
    unconverted county export ('createdate' on every row) skips 100% of rows (B5, 2026-08-10)."""
    if isinstance(v, datetime.datetime):
        return v.isoformat()
    if isinstance(v, datetime.date):
        return v.isoformat()  # 'D' fields: date-only → 'YYYY-MM-DD', exactly what a date field validates
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def _shapefile_to_features(b64: str) -> tuple[int | None, str, list[dict[str, Any]]]:
    """Shapefile adapter → (detected_srid_or_None, 'geojson', raw_features). Reads a zipped .shp/.dbf/.shx/.prj
    fully in-memory via pyshp (no GDAL; zip/member-pick + .prj→srid shared with the schema preview via
    file_ingest). Each shape → a GeoJSON geometry via pyshp's __geo_interface__ (handles point/line/polygon +
    multipart); .dbf records → raw props. The srid is None when the .prj can't be resolved — the endpoint then
    requires a source_srid override."""
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="shp_base64 is not valid base64")
    reader, detected = open_shapefile_zip(data)
    try:
        field_names = [f[0] for f in reader.fields[1:]]  # fields[0] is the pyshp DeletionFlag
        out: list[dict[str, Any]] = []
        for sr in reader.iterShapeRecords():
            shape = sr.shape
            props = {field_names[i]: _dbf_value(v) for i, v in enumerate(list(sr.record)) if i < len(field_names)}
            try:
                geom = shape.__geo_interface__  # GeoJSON geometry dict (Point/LineString/Polygon + Multi*)
            except Exception:
                geom = None
            family = FAMILY_BY_GEOJSON.get(geom.get("type")) if isinstance(geom, dict) else None
            if family is None or not geom.get("coordinates"):
                out.append({"family": None, "geom": None, "props": props, "error": "missing or unsupported geometry"})
                continue
            out.append({"family": family, "geom": geom, "props": props, "error": None})
    except Exception:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="could not read the Shapefile")
    return detected, "geojson", out


def _coerce_csv_value(data_type: str, v: Any) -> Any:
    """A CSV cell is a STRING; coerce it to the target field's type so validate_props checks the real value —
    without this, importing a CSV into a layer with typed fields rejects every row ('must be a number' on
    '42'), which add-from-file made the MAINLINE path (the wizard drafts typed fields from the same file it
    then ingests). Blank ⇒ None (the trigger enforces `required`); an uncoercible string passes through so
    validate_props reports the honest type error. Dates stay strings (validate_props checks ISO)."""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s == "":
        return None
    if data_type == "number":
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            return v
    if data_type == "boolean":
        low = s.lower()
        if low in ("true", "yes", "t", "y", "1"):
            return True
        if low in ("false", "no", "f", "n", "0"):
            return False
        return v
    return v


def _csv_to_features(text: str, xcol_override: str | None = None,
                     ycol_override: str | None = None) -> tuple[int | None, str, list[dict[str, Any]]]:
    """CSV adapter → (detected_srid_or_None, 'geojson', raw_features). Finds the coordinate columns by common
    names (lon/lat or x/y — detection shared with the schema preview via file_ingest), or takes the caller's
    explicit pair (the wizard's manual override); the rest are attributes; emits a Point geometry per row (CSV
    is point-only by nature — the endpoint rejects CSV for line/polygon layers). Geographic names ⇒ 4326;
    projected ⇒ declare."""
    if text.startswith("\ufeff"):
        text = text[1:]  # Excel/ArcGIS CSVs carry a UTF-8 BOM — without it stripped the first header is corrupt
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    if not headers:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="CSV has no header row")
    if xcol_override or ycol_override:
        if not xcol_override or not ycol_override:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="csv_x_column and csv_y_column must be provided together")
        missing = [c for c in (xcol_override, ycol_override) if c not in headers]
        if missing:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"coordinate column(s) not in the CSV header: {', '.join(missing)}")
        xcol, ycol = xcol_override, ycol_override
        detected = csv_pair_srid(xcol, ycol)  # the shared rule — a mixed lon/lat + x/y pick is refused here
    else:
        xcol, ycol, detected = find_csv_coordinates(headers)
        if not xcol or not ycol:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="no coordinate columns found (expected lon/lat or x/y)",
            )
    out: list[dict[str, Any]] = []
    for row in reader:
        sx, sy = (row.get(xcol) or "").strip(), (row.get(ycol) or "").strip()
        try:
            x, y = float(sx), float(sy)
        except ValueError:
            out.append(_err_feat(f"invalid coordinates ({xcol}={sx!r}, {ycol}={sy!r})"))
            continue
        props = {k: v for k, v in row.items() if k is not None and k not in (xcol, ycol)}
        out.append({"family": "point", "geom": {"type": "Point", "coordinates": [x, y]},
                    "props": props, "error": None})
    return detected, "geojson", out


def _sample_coords(raw: list[dict[str, Any]], limit: int = 500) -> list[tuple[float, float]]:
    """Up to `limit` (x, y) pairs out of parsed GeoJSON rows. Walks nested coordinate arrays, so it works for
    points, lines and polygons alike. GeoJSON-kind only — WKB rows (shapefile/gpkg) are left to their own CRS."""
    out: list[tuple[float, float]] = []
    for rf in raw:
        geom = rf.get("geom")
        if rf.get("error") is not None or not isinstance(geom, dict):
            continue
        stack: list[Any] = [geom.get("coordinates")]
        while stack:
            c = stack.pop()
            if not isinstance(c, (list, tuple)) or not c:
                continue
            if isinstance(c[0], (int, float)) and len(c) >= 2:
                out.append((float(c[0]), float(c[1])))
                if len(out) >= limit:
                    return out
            else:
                stack.extend(c)
    return out


def _assert_coords_match_crs(kind: str, raw: list[dict[str, Any]], source_srid: int, geographic: bool) -> None:
    """The declared CRS must be *possible* for the actual numbers — the srid merely EXISTING is not enough.
    Both directions are real, both seen live:
      * projected declared, degrees found — an ArcGIS CSV export carries WGS-84 degrees even when the service
        advertises 102100/3857; declaring 3857 stored 12,303 signs at null island (2026-08-13);
      * geographic declared, projected found — state-plane feet read as lon/lat, which silently skipped every
        row of two county imports (B5, 2026-08-10) and is why the wizard gates on an explicit pick.
    Raises 422 naming the fix. Sampled (not exhaustive) — this is a mislabel detector, not a validator."""
    if kind != "geojson":
        return
    coords = _sample_coords(raw)
    if not coords:
        return
    # Readable in BOTH ranges: %g renders a state-plane northing as 1.1205e+06, which reads as noise.
    def _num(v: float) -> str:
        return f"{v:,.6f}".rstrip("0").rstrip(".")

    in_degrees = [abs(x) <= 180 and abs(y) <= 90 for x, y in coords]
    if not geographic and all(in_degrees):
        x, y = coords[0]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"these coordinates look like longitude/latitude degrees (e.g. {_num(x)}, {_num(y)}), but EPSG:"
                f"{source_srid} is a projected system measured in linear units — re-import and choose "
                "WGS 84 (EPSG:4326). Note an ArcGIS CSV export is in degrees even when the service reports "
                "a projected spatial reference such as 102100 / 3857."
            ),
        )
    if geographic and not all(in_degrees):
        x, y = next(c for c, ok in zip(coords, in_degrees) if not ok)
        # Name a LIKELY candidate from the magnitude instead of one fixed guess: a Web-Mercator easting runs to
        # ±20,037,508 m, while a State Plane easting is hundreds of thousands. Suggesting state plane for what
        # were plainly Web-Mercator metres put 6,577 street lights in Mexico (2026-08-13) — a wrong hint is
        # worse than none, because it reads as authoritative.
        hint = (
            "values in the millions suggest Web Mercator metres — WGS 84 / Pseudo-Mercator (EPSG:3857), which is "
            "what an ArcGIS export from a web-service basemap usually carries"
            if abs(x) > 1_000_000
            else "values in the hundreds of thousands suggest a State Plane or other projected system — search "
            "for your area's zone by name"
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"these coordinates ({_num(x)}, {_num(y)}) are outside the longitude/latitude range, but EPSG:"
                f"{source_srid} is measured in degrees — choose the projected coordinate system the file is "
                f"actually in ({hint}). Check the source's own metadata if you can."
            ),
        )


@router.post(
    "/layers/{layer_id}/features/import",
    summary="Bulk-import features (point/line/polygon) from GeoJSON / GeoPackage / Shapefile / GeoParquet, or points from CSV (validate-first dry run, then commit; editor only)",
)
async def import_features(
    layer_id: str,
    payload: FeatureImport,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _validate_slot(conn, layer_id)
    await _require_edit(conn, claims, user, layer_id, op="add")

    # The layer's storage + geometry family (V055/c5): drives the per-row family gate, the CSV restriction, and
    # the commit INSERT. Fetched once here (cheap) and reused for the commit below.
    native, working_srid, geom_type = await _layer_storage(conn, layer_id)

    # A non-spatial TABLE (decision-nonspatial-tables.md) has no geometry — the geometry-file formats can
    # never apply. Reject early with a clear message instead of per-row family errors. (Attribute-only
    # row import for tables is a queued follow-on — see the backlog.)
    if geom_type == "none":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="this is a non-spatial table — feature import needs geometry; add rows via the grid or API",
        )

    # CSV carries no line/polygon geometry (point lon/lat by nature) — reject early for non-point layers.
    if payload.format == "csv" and geom_type != "point":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"CSV import supports point layers only; this is a {geom_type} layer",
        )

    # One byte cap across the flow (decision-add-from-file.md §2.5) — the schema preview's same 50 MB.
    # base64 payloads are checked at ~4/3 encoded length, BEFORE decoding buys another in-memory copy;
    # text formats by text length.
    too_big = HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=f"file too large (max {MAX_FILE_BYTES // (1024 * 1024)} MB)",
    )
    for b64_field in (payload.gpkg_base64, payload.shp_base64, payload.parquet_base64):
        if b64_field is not None and len(b64_field) > (MAX_FILE_BYTES * 4) // 3 + 8:
            raise too_big
    for text_field in (payload.geojson_text, payload.csv_text):
        if text_field is not None and len(text_field) > MAX_FILE_BYTES:
            raise too_big

    # Adapter per format → (detected_srid, kind, raw_features). kind ∈ {'geojson','wkb'} fixes how the commit
    # builds geometry; raw_features carry geom + family (or a per-row error) + props.
    if payload.format == "geojson":
        if payload.geojson_text is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="geojson_text is required")
        detected_srid, kind, raw = _geojson_to_features(payload.geojson_text)
    elif payload.format == "geopackage":
        if payload.gpkg_base64 is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="gpkg_base64 is required")
        detected_srid, kind, raw = await run_in_threadpool(_geopackage_to_features, payload.gpkg_base64)  # K1
    elif payload.format == "shapefile":
        if payload.shp_base64 is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="shp_base64 is required")
        detected_srid, kind, raw = await run_in_threadpool(_shapefile_to_features, payload.shp_base64)  # K5
    elif payload.format == "csv":
        if payload.csv_text is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="csv_text is required")
        detected_srid, kind, raw = _csv_to_features(payload.csv_text, payload.csv_x_column, payload.csv_y_column)
    elif payload.format == "geoparquet":
        if payload.parquet_base64 is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="parquet_base64 is required")
        detected_srid, kind, raw = _geoparquet_to_features(payload.parquet_base64)
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="format must be 'geojson', 'geopackage', 'shapefile', 'csv', or 'geoparquet'",
        )
    if len(raw) > INGEST_FEATURE_CAP:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"too many features ({len(raw)}); the per-upload cap is {INGEST_FEATURE_CAP}",
        )
    # The source CRS: the explicit override wins, else what the file declared. None ⇒ the user must declare it
    # (a Shapefile .prj with no AUTHORITY code is the common cause). decision-bulk-feature-ingest.md.
    source_srid = payload.source_srid if payload.source_srid is not None else detected_srid
    if source_srid is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="could not determine the file's coordinate system; choose the source CRS and re-import",
        )
    # The source CRS must be a real EPSG PostGIS can ST_Transform (a foreign file may carry an unknown srid).
    async with conn.cursor() as cur:
        await cur.execute("SELECT proj4text FROM public.spatial_ref_sys WHERE srid = %s", (source_srid,))
        srs_row = await cur.fetchone()
        if srs_row is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unsupported source CRS (srid {source_srid}); declare a known EPSG coordinate system",
            )
    _assert_coords_match_crs(kind, raw, source_srid, "+proj=longlat" in (srs_row[0] or ""))

    field_defs = await active_field_defs(conn, layer_id)
    # case-insensitive incoming-name -> the layer's field_key; 'notes' is the built-in column, not a custom field.
    by_lower = {k.lower(): k for k in field_defs.keys()}
    coerce = payload.format == "csv"  # CSV cells are strings — coerce to the target field's type (see below)

    valid: list[dict[str, Any]] = []          # {g, notes, props} — g is GeoJSON text or WKB bytes (per kind)
    errors: list[dict[str, Any]] = []         # {index, reason}
    unmatched: set[str] = set()
    for i, rf in enumerate(raw):
        if rf["error"] is not None:
            errors.append({"index": i, "reason": rf["error"]})
            continue
        # Geometry-family gate (fail-closed): a row whose family doesn't match the layer is skipped, not stored.
        if rf["family"] != geom_type:
            errors.append({"index": i, "reason": f"geometry is a {rf['family']}, but this is a {geom_type} layer"})
            continue
        notes: str | None = None
        props: dict[str, Any] = {}
        for k, v in (rf["props"] or {}).items():
            kl = str(k).lower()
            # Exact case-fold first (unchanged, and it still matches a pre-V087 layer that really does
            # carry an `objectid` key); then the SANITIZED form as a fallback, so this loader agrees
            # with the draft builder that named the fields in the first place — same file, same wizard
            # (decision-esri-field-name-clash.md §3.6 Q6). Without the fallback a source `OBJECTID`
            # column silently loses its values to `unmatched_columns` the moment §3.1 renames the key
            # to `objectid_src` — and a column named `Pole Type` (drafted `pole_type`, ingested
            # `pole type`) has been losing them all along. Two source names that sanitize alike
            # (`Pole Type` / `Pole-Type`) resolve last-wins; both were dropped outright before.
            fk = by_lower.get(kl) or by_lower.get(_sanitize_key(str(k)))
            if kl == "notes":
                notes = None if v is None else str(v)
            elif fk is not None:
                props[fk] = _coerce_csv_value(field_defs[fk].data_type, v) if coerce else v
            else:
                unmatched.add(str(k))
        try:
            validate_props(field_defs, props)
        except PropsError as e:
            errors.append({"index": i, "reason": "; ".join(str(x_) for x_ in e.errors)})
            continue
        # g is the geom param bound at insert: GeoJSON text for kind='geojson', raw WKB bytes for kind='wkb'.
        g = json.dumps(rf["geom"]) if kind == "geojson" else rf["geom"]
        valid.append({"g": g, "notes": notes, "props": props})

    result: dict[str, Any] = {
        "total": len(raw),
        "valid": len(valid),
        "error_count": len(errors),
        "errors": errors[:200],                 # bound the response
        "unmatched_columns": sorted(unmatched)[:50],
        "committed": 0,
    }
    if not payload.commit or not valid:
        return result

    storage = _storage_srid(native, working_srid)
    # Reproject the source geometry into the layer's storage CRS + normalize 2D/Multi* (no-op transform when
    # source == storage). The geom expression is fixed by (kind, geom_type) — see _ingest_geom_expr.
    query = sql.SQL(
        "INSERT INTO {tbl} (geom, notes, props, created_by, updated_by) "
        "VALUES ({geom}, %(notes)s, %(props)s, %(actor)s, %(actor)s)"
    ).format(tbl=_slot_table(layer_id), geom=sql.SQL(_ingest_geom_expr(kind, geom_type)))
    actor = str(user.id)
    params = [
        {"g": r["g"], "src": source_srid, "storage": storage,
         "notes": r["notes"], "props": Jsonb(r["props"]), "actor": actor}
        for r in valid
    ]
    try:
        async with conn.cursor() as cur:
            await set_audit_context(cur, user, _AUDIT_CLIENT)
            await cur.executemany(query, params)
        await conn.commit()
    except pg_errors.CheckViolation as e:  # the props trigger (belt) rejected a row the app-check missed
        await conn.rollback()
        raise map_props_check_violation(e)
    except (pg_errors.InternalError_, pg_errors.DataError) as e:  # a bad geometry slipped past the adapter
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"invalid geometry in upload: {str(e).splitlines()[0][:200]}")
    result["committed"] = len(valid)
    log.info("slot_features_imported", layer_id=layer_id, count=len(valid), fmt=payload.format, actor=actor)
    return result


# --- geometry calculation + bulk set field value (decision-geometry-calc.md) -----
#
# The Pro "Calculate Geometry Attributes" analog, fenced GEOMETRY-ONLY (the 2026-07-01 drift ruling —
# no expressions, no arithmetic, no Z) plus ONE editing op, `set` ("bulk set field value": one bound
# constant on the scoped rows). One bounded, set-based UPDATE writes props[target_field] for every row
# the scope matches; the scope is the grid's own predicates — the SAME injection-safe filter grammar as
# /features?filter= (validated field, whitelisted op, bound value/key) + optional id set + bbox, via the
# shared layer_query._where. The per-row V019 audit and V044/V048 props-validation triggers fire under a
# set-based UPDATE exactly as they do per-row (un-bypassable audit holds), and V035 bumps row_version per
# row so concurrent editors' stale single-row writes 409 unchanged (T34). Dry-run by default (mirrors
# bulk ingest); the apply is ATOMIC — any trigger rejection (e.g. a computed value violates the target's
# constraint) rolls back the whole batch to a clean 422, never a partial write.

_CALC_CAP = 50_000  # bounded synchronous batch — above this the answer is narrowing the scope

# op -> the geometry family it requires (None = any slot/table layer; geometry ops 422 on 'none').
_CALC_OPS: dict[str, str | None] = {
    "x": "point", "y": "point", "length": "line", "perimeter": "polygon", "area": "polygon",
    "set": None,
}

# Fixed unit lists (the §1 fence): meters-per-unit for lengths, square-meters-per-unit for areas.
# Measures are GEODESIC — geography, true ellipsoidal meters (decision-geometry-calc.md §3.5).
_LENGTH_M_PER_UNIT = {"m": 1.0, "ft": 0.3048, "km": 1000.0, "mi": 1609.344}
_AREA_SQM_PER_UNIT = {
    "sqm": 1.0, "sqft": 0.09290304, "hectares": 10_000.0, "acres": 4046.8564224, "sqkm": 1_000_000.0,
}

# The geometry-op SQL expressions. FIXED strings keyed by the validated op — user input never reaches
# the expression; srid/factor bind. X/Y read the point in the chosen output CRS (working-CRS parity
# with exports); length/perimeter/area measure on geography (transform is a no-op for 4326 storage).
_CALC_EXPRS = {
    # Params carry EXPLICIT casts: these expressions feed jsonb_build_object (variadic "any"), where
    # Postgres cannot infer an untyped parameter — IndeterminateDatatype at execute, which offline SQL
    # review can't catch (found by the first live sweep run). ST_Transform's srid also casts ::integer
    # so overload resolution never sees an unknown.
    "x": "ST_X(ST_Transform(t.geom, %(csrid)s::integer))::float8",
    "y": "ST_Y(ST_Transform(t.geom, %(csrid)s::integer))::float8",
    "length": "(ST_Length(ST_Transform(t.geom, 4326)::geography) / %(factor)s::float8)::float8",
    "perimeter": "(ST_Perimeter(ST_Transform(t.geom, 4326)::geography) / %(factor)s::float8)::float8",
    "area": "(ST_Area(ST_Transform(t.geom, 4326)::geography) / %(factor)s::float8)::float8",
}


class CalcScope(BaseModel):
    # The grid's current predicates, verbatim: repeatable 'field:op:value' filter strings (real +
    # owner-defined fields), an optional explicit id set (the selection), an optional 4326 bbox.
    filters: list[str] = []
    ids: list[str] = []
    bbox: str | None = None


class FeatureCalc(BaseModel):
    op: str
    target_field: str                  # an EXISTING active owner-defined field (the Fields UI owns schema)
    unit: str | None = None            # length/perimeter/area only
    srid: int | None = None            # x/y only; default = the layer's working CRS
    value: Any = None                  # set only; validated against the target field's type/constraints
    scope: CalcScope = CalcScope()
    commit: bool = False               # false = dry run (the default posture, mirrors bulk ingest)


def _calc_expr_params(payload: FeatureCalc, working_srid: int) -> tuple[str | None, dict[str, Any]]:
    """(fixed SQL expression | None for 'set', its bound params) — after op/target validation.
    422s a unit/srid/value that doesn't belong to the op, so a confused request never half-works."""
    op = payload.op
    if op == "set":
        if payload.unit is not None or payload.srid is not None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="'set' takes a value — unit/srid do not apply")
        return None, {}
    if payload.value is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"'{op}' computes from geometry — 'value' does not apply")
    if op in ("x", "y"):
        if payload.unit is not None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="x/y take a CRS (srid), not a unit")
        return _CALC_EXPRS[op], {"csrid": payload.srid if payload.srid is not None else working_srid}
    # length / perimeter / area
    if payload.srid is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"'{op}' takes a unit, not a CRS")
    units = _LENGTH_M_PER_UNIT if op in ("length", "perimeter") else _AREA_SQM_PER_UNIT
    if payload.unit not in units:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"'{op}' requires unit one of {sorted(units)}")
    return _CALC_EXPRS[op], {"factor": units[payload.unit]}


@router.post(
    "/layers/{layer_id}/features/calc",
    summary="Populate a field with geometry attributes (X/Y, length, perimeter, area) or a constant value, over the filtered/selected rows (dry-run first; editor only)",
)
async def calc_features(
    layer_id: str,
    payload: FeatureCalc,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _validate_slot(conn, layer_id)
    await _require_edit(conn, claims, user, layer_id, op="attributes")
    _, working_srid, geom_type = await _layer_storage(conn, layer_id)

    # Op ↔ geometry-family gate (fail-closed): geometry ops need the matching family; tables get 'set' only.
    if payload.op not in _CALC_OPS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"op must be one of {sorted(_CALC_OPS)}")
    need = _CALC_OPS[payload.op]
    if need is not None:
        if geom_type == "none":
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="this is a non-spatial table — only 'set' applies")
        if geom_type != need:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"op '{payload.op}' applies to {need} layers; this is a {geom_type} layer")

    # Target field: must ALREADY EXIST as an active owner-defined field (the grid never mutates schema);
    # geometry results are numbers, so those ops require a number field. 'set' validates the bound value
    # app-side (type + declarative constraints); required/domain-membership stay the trigger belt's job.
    field_defs = await active_field_defs(conn, layer_id)
    target = field_defs.get(payload.target_field)
    if target is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"unknown or inactive target field '{payload.target_field}'")
    if payload.op != "set" and target.data_type != "number":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"'{payload.op}' writes a number — target field '{payload.target_field}' is {target.data_type}")
    if payload.op == "set" and payload.value is not None:
        try:
            validate_props(field_defs, {payload.target_field: payload.value})
        except PropsError as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.errors)

    expr, expr_params = _calc_expr_params(payload, working_srid)
    if payload.op in ("x", "y"):  # the chosen output CRS must be a real EPSG (mirrors import/export)
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM public.spatial_ref_sys WHERE srid = %s", (expr_params["csrid"],))
            if await cur.fetchone() is None:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    detail=f"unsupported CRS (srid {expr_params['csrid']})")

    # Scope: the shared /features grammar (real + props fields validated, ops whitelisted, values/keys
    # bound). Empty scope = the whole layer. Raises 422 on any bad predicate before SQL sees it.
    valid = set(await _layer_columns(conn, layer_id))
    props_fields = await _props_fields(conn, layer_id)
    where_sql, params = _where(
        payload.scope.filters, payload.scope.bbox, payload.scope.ids or None, valid, props_fields,
        storage_srid=(await _geom_srid(conn, layer_id) or 4326) if payload.scope.bbox is not None else 4326,
    )
    tbl = _slot_table(layer_id)

    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL("SELECT count(*) FROM {tbl} AS t WHERE {where}").format(tbl=tbl, where=where_sql),
            params,
        )
        matched = (await cur.fetchone())[0]
    if matched > _CALC_CAP:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"scope matches {matched} rows; the per-calc cap is {_CALC_CAP} — narrow the filter",
        )
    if not payload.commit:
        await conn.rollback()
        return {"matched": matched, "committed": False}

    # Apply: ONE set-based UPDATE. jsonb_build_object binds the target key as text (never an
    # identifier) and the computed/bound value; updated_by/updated_at match every other write path;
    # row_version bumps per row via the V035 trigger. ATOMIC: a trigger rejection rolls it all back.
    if expr is None:  # set — the bound constant (JSON null clears, matching single-row merge semantics)
        value_sql = sql.SQL("%(cval)s::jsonb")
        expr_params = {"cval": Jsonb(payload.value)}
    else:
        value_sql = sql.SQL(expr)  # a fixed expression from _CALC_EXPRS — never user input
    query = sql.SQL(
        # %(ctarget)s::text — jsonb_build_object is variadic "any"; an uncast parameter there is
        # IndeterminateDatatype at execute time (the first live sweep's finding).
        "UPDATE {tbl} AS t SET props = t.props || jsonb_build_object(%(ctarget)s::text, {val}), "
        "updated_by = %(cactor)s, updated_at = now() WHERE {where}"
    ).format(tbl=tbl, val=value_sql, where=where_sql)
    run_params = {**params, **expr_params, "ctarget": payload.target_field, "cactor": str(user.id)}

    try:
        async with conn.cursor() as cur:
            await set_audit_context(cur, user, _AUDIT_CLIENT)
            await cur.execute(query, run_params)
            updated = cur.rowcount
        await conn.commit()
    except pg_errors.CheckViolation as e:  # the props trigger rejected a row -> the WHOLE batch rolls back
        await conn.rollback()
        raise map_props_check_violation(e)
    except (pg_errors.InternalError_, pg_errors.DataError) as e:  # e.g. an unrepresentable transform
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"calculation failed: {str(e).splitlines()[0][:200]}")
    log.info(
        "slot_features_calc", layer_id=layer_id, op=payload.op, target=payload.target_field,
        matched=matched, updated=updated, actor=str(user.id),
    )
    return {"matched": matched, "updated": updated, "committed": True}


@router.get(
    "/layers/{layer_id}/features/{fid}",
    summary="Fetch one slot feature by id",
)
async def get_feature(
    layer_id: str,
    fid: UUID,
    response: Response,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await _validate_slot(conn, layer_id)
    await _require_view(user, layer_id)
    _, _, geom_type = await _layer_storage(conn, layer_id)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_slot_by_id_sql(layer_id, has_geom=geom_type != "none"), {"fid": str(fid)})
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feature not found")
    response.headers["ETag"] = f'"{row["version"]}"'  # optimistic-concurrency token (Talus 34)
    return row


@router.patch(
    "/layers/{layer_id}/features/{fid}",
    summary="Update a slot feature's geometry and/or notes (editor only)",
)
async def update_feature(
    layer_id: str,
    fid: UUID,
    payload: SlotFeatureUpdate,
    conn: AsyncConnection = Depends(get_db),
    # Talus Field f4: Bearer, or a read_write field-sync PAT scoped to this layer (see create_feature).
    user: User = Depends(get_current_user_rw),
    claims: dict | None = Depends(rw_claims),
    expected_version: str = Depends(require_if_match),
) -> dict[str, str]:
    await _validate_slot(conn, layer_id)
    fields = payload.model_dump(exclude_unset=True)
    await _require_edit(conn, claims, user, layer_id, op=_update_ops(fields))
    actor = user.id

    native, srid, geom_type = await _layer_storage(conn, layer_id)

    # Column names are LITERAL (not user-derived); values are bound params.
    set_clauses: list[str] = []
    params: dict = {"fid": str(fid), "actor": str(actor), "expected_version": expected_version}

    if geom_type == "none":
        # Aspatial hosted table (decision-nonspatial-tables.md): no geometry to move/replace — reject any
        # coordinate/geometry keys; only notes/props edits fall through below.
        if any(k in fields for k in ("lon", "lat", "x", "y", "geometry")):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="this is a non-spatial table — it has no geometry to edit",
            )
    elif geom_type == "point":
        # A POINT move uses the layer's storage coordinate pair: native → {x,y} in its CRS, default → {lon,lat}.
        move_keys = ("x", "y") if native else ("lon", "lat")
        other_keys = ("lon", "lat") if native else ("x", "y")
        if any(k in fields for k in other_keys):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "this layer stores natively; send x and y (not lon/lat)" if native
                    else "this layer stores in WGS84; send lon and lat (not x/y)"
                ),
            )
        if (move_keys[0] in fields) != (move_keys[1] in fields):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{move_keys[0]} and {move_keys[1]} must be provided together",
            )
        if move_keys[0] in fields:  # both present (guarded above) -> MOVE; srid binds into ST_SetSRID
            set_clauses.append("geom = ST_SetSRID(ST_MakePoint(%(px)s, %(py)s), %(psrid)s)")
            params["px"] = fields.pop(move_keys[0])
            params["py"] = fields.pop(move_keys[1])
            params["psrid"] = srid if native else 4326
    else:
        # LINE/POLYGON: geometry is replaced wholesale by a new GeoJSON `geometry` (the c6 draw tool posts the
        # edited shape). lon/lat/x/y make no sense here — reject them. The helper 422s a mismatched family.
        if any(k in fields for k in ("lon", "lat", "x", "y")):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"this {geom_type} layer takes a GeoJSON 'geometry', not lon/lat/x/y",
            )
        if "geometry" in fields:
            geom_expr, geom_params = _geom_expr_and_params(
                geom_type, native, srid,
                lon=None, lat=None, x=None, y=None, geometry=fields.pop("geometry"),
            )
            set_clauses.append(f"geom = {geom_expr}")
            params.update(geom_params)
    # Drop any coordinate/geometry keys not consumed above so the leftover-field handling below stays clean.
    for _k in ("lon", "lat", "x", "y", "geometry"):
        fields.pop(_k, None)

    if "notes" in fields:  # attribute edit (may set NULL to clear)
        set_clauses.append("notes = %(notes)s")
        params["notes"] = fields.pop("notes")
    if "props" in fields:  # merge a partial patch of owner-defined field values onto existing props
        incoming = fields.pop("props") or {}
        if incoming:
            try:
                validate_props(await active_field_defs(conn, layer_id), incoming)
            except PropsError as e:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.errors)
        set_clauses.append("props = props || %(props)s")
        params["props"] = Jsonb(incoming)
    if not set_clauses:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="no fields to update",
        )
    set_clauses += ["updated_by = %(actor)s", "updated_at = now()"]

    # Optimistic-concurrency guard (Talus 34): the row_version predicate makes a stale
    # write affect 0 rows -> 409 with the current row. RETURNING the bumped row_version
    # (set by the BEFORE UPDATE trigger, V035) lets the client keep editing without a re-read.
    query = sql.SQL(
        "UPDATE {tbl} SET {sets} "
        "WHERE id = %(fid)s::uuid AND row_version = %(expected_version)s::bigint "
        "RETURNING id::text, row_version::text AS version"
    ).format(
        tbl=_slot_table(layer_id),
        sets=sql.SQL(", ").join(sql.SQL(c) for c in set_clauses),
    )

    try:
        async with conn.cursor() as cur:
            await set_audit_context(cur, user, _AUDIT_CLIENT)
            await cur.execute(query, params)
            updated = await cur.fetchone()
    except pg_errors.CheckViolation as e:  # the props trigger rejected the merged result
        await conn.rollback()
        raise map_props_check_violation(e)
    except (pg_errors.InternalError_, pg_errors.DataError) as e:  # ST_GeomFromGeoJSON rejected a bad geometry
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"invalid geometry: {str(e).splitlines()[0][:200]}")
    if updated is None:  # 0 rows: stale version (409 + current row) or gone (404)
        await version_conflict_or_404(
            conn, _slot_by_id_sql(layer_id, has_geom=geom_type != "none"), {"fid": str(fid)}, "feature"
        )
    await conn.commit()
    log.info("slot_feature_mutation", action="update", layer_id=layer_id, entity_id=updated[0], actor=str(actor))
    return {"id": updated[0], "version": updated[1]}


@router.delete(
    "/layers/{layer_id}/features/{fid}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a slot feature (editor only)",
)
async def delete_feature(
    layer_id: str,
    fid: UUID,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
    expected_version: str = Depends(require_if_match),
) -> Response:
    await _validate_slot(conn, layer_id)
    await _require_edit(conn, claims, user, layer_id, op="delete")
    actor = user.id
    _, _, geom_type = await _layer_storage(conn, layer_id)

    # Optimistic-concurrency guard (Talus 34): stale delete hits 0 rows -> 409 / 404.
    query = sql.SQL(
        "DELETE FROM {tbl} WHERE id = %(fid)s::uuid AND row_version = %(expected_version)s::bigint "
        "RETURNING id::text"
    ).format(tbl=_slot_table(layer_id))

    async with conn.cursor() as cur:
        await set_audit_context(cur, user, _AUDIT_CLIENT)
        await cur.execute(query, {"fid": str(fid), "expected_version": expected_version})
        deleted = await cur.fetchone()
    if deleted is None:  # 0 rows: stale version (409 + current row) or gone (404)
        await version_conflict_or_404(
            conn, _slot_by_id_sql(layer_id, has_geom=geom_type != "none"), {"fid": str(fid)}, "feature"
        )
    # Delete means delete (decision-feature-delete-attachments.md): the feature's attachments go with
    # it, in THIS transaction, so a rolled-back delete (the stale-version 409 above) leaves them whole.
    # Feature-first is deliberate (§F6) — the 409/404 short-circuit fires before any attachment moves.
    object_keys = await att.delete_for_feature(conn, layer_id, str(fid), user, _AUDIT_CLIENT)
    await conn.commit()
    # Blobs AFTER commit, best-effort — same shape as attachments.py delete_attachment. A store hiccup
    # now leaves a blob whose row is gone, so T39 finally CAN collect it (before this slice the
    # surviving row made the key look permanently live and the reaper skipped it by definition).
    for object_key in object_keys:
        try:
            await run_in_threadpool(remove_object, object_key)
        except MediaStoreUnavailable:
            log.warning("attachment_blob_orphaned", object_key=object_key, layer_id=layer_id)
    log.info("slot_feature_mutation", action="delete", layer_id=layer_id, entity_id=str(fid), actor=str(actor),
             attachments_cascaded=len(object_keys))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
