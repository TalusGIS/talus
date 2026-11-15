"""GeoServices ("Esri REST") FeatureServer façade — interop I-3 (decision-interop-i3.md).

The desktop EDITING surface and the AGO-style per-layer copy-link: every hosted layer is its own
one-layer FeatureServer service (`…/rest/services/{layer_id}/FeatureServer`, layer index always
0) over the exact machinery the portal already uses — no publish step, no service registry, no
second permission system. Reads delegate to layer_query.feature_page (the one paged builder, same
as OGC), so filters/scrub/reprojection cannot drift from /features.

★ TWO DOORS, same rationale and factory pattern as ogc.py: `/api/v1/esri` (public — never
challenges; anon sees public layers behind the F6 switch; the AGO/harvest leg) and
`/api/v1/esri-auth` (sign-in — 401 + Basic challenge; ArcGIS Pro's path). "Web-tier" Basic is a
documented Esri topology: /rest/info reports isTokenBasedSecurity:false and clients fall back to
HTTP auth — no generateToken shim (fork 2, recon-resolved: QGIS's AFS provider has NO token code
at all and sends Basic preemptively via authcfg).

Client contracts this file exists to satisfy (source-verified recon, decision doc §1.1):
  - QGIS 3.44 AFS: service-root `capabilities` must contain `Query` (CASE-SENSITIVE there);
    harvests ALL objectids in ONE returnIdsOnly call (serve uncapped — fork 10;
    truncation = silent feature loss) then fetches by objectIds batches; never pages with
    resultOffset, always f=json (esriJSON); OBJECTIDs must fit INT32 (`objectId.toInt()`).
  - Pro 3.7 / JS API: page with resultOffset/resultRecordCount when
    advancedQueryCapabilities.supportsPagination is true; honest exceededTransferLimit.
  - Dates are EPOCH MILLISECONDS UTC in esriJSON attributes.
  - /rest/info must exist (clients derive it by splitting the URL at /rest/) and must OMIT
    owningSystemUrl — presence makes clients chase a phantom Portal.
  - The AFS response path has NO JSON preprocessor (unlike OAPIF) — serve plain compact JSON;
    no backslash neutralizers needed here.
Tables (geom_type='none') are FeatureServer TABLES (fork 5): same doc shapes minus geometry keys.
View layers (vw_*) are excluded in v1 — a PG view carries no objectid identity column.
"""
import base64
import binascii
import json
import math
import re
import time
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, quote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from psycopg import AsyncConnection, sql
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from .. import attachments as att
from .. import keycloak_admin
from ..audit_context import set_audit_context
from ..auth import BASIC_CHALLENGE, BASIC_CHALLENGE_NO_VIEW, _optional_claims, get_optional_user
from ..authz import EDIT_OP_DENIED, edit_allowed, is_shared, public_object_allowed
from ..config import settings
from ..db import get_db
from ..display_field import derive_display_field
from ..layer_props import PropsError, active_field_defs, map_props_check_violation, validate_props
from ..media import MediaObjectMissing, MediaStoreUnavailable, read_object, remove_object
from ..models.user import User
from .attachments import _safe_disposition
from .layer_features import (
    _SLOT_RE,
    _geom_expr_and_params,
    _layer_storage,
    _validate_slot,
)
from .layer_query import (
    _geom_srid,
    _authorize_read,
    _columns,
    _props_fields,
    _require_registered,
    _where,
    feature_page,
)
from .ogc import _extent_bbox

log = structlog.get_logger(__name__)

PUBLIC_MOUNT = "/esri"
AUTH_MOUNT = "/esri-auth"

_CURRENT_VERSION = 11.2
_FULL_VERSION = "11.2.0"
_MAX_RECORD_COUNT = 1000  # the OGC page ceiling, kept identical across dialects
_IDS_WARN_THRESHOLD = 500_000  # fork 10: uncapped id harvests, but log when one gets huge
_WGS84_SR = {"wkid": 4326, "latestWkid": 4326}
_MERC_SR = {"wkid": 102100, "latestWkid": 3857}
_R = 6378137.0  # spherical-mercator radius (EPSG:3857 closed form)

# Real columns that never surface as esri fields: identity/geometry ride dedicated slots
# (OBJECTID / GlobalID / geometry), the rest are internal plumbing (row_version is fork 3's
# recorded option B — not exposed in v1) or identity uuids the desktop can't resolve.
_EXCLUDED_COLUMNS = frozenset(
    {"id", "geom", "objectid", "props", "row_version", "ai_provenance", "created_by", "updated_by"}
)
_READONLY_COLUMNS = frozenset({"created_at", "updated_at"})

# information_schema data_type -> esri field type. Booleans are String "true"/"false" (fork 8b —
# esri has no boolean type). bigint maps to Integer: no surviving bigint column is exposed
# (row_version is excluded), so int32 overflow is structurally unreachable.
_PG_TO_ESRI = {
    "text": "esriFieldTypeString",
    "character varying": "esriFieldTypeString",
    "smallint": "esriFieldTypeSmallInteger",
    "integer": "esriFieldTypeInteger",
    "bigint": "esriFieldTypeInteger",
    "numeric": "esriFieldTypeDouble",
    "double precision": "esriFieldTypeDouble",
    "real": "esriFieldTypeSingle",
    "date": "esriFieldTypeDate",
    "timestamp with time zone": "esriFieldTypeDate",
    "timestamp without time zone": "esriFieldTypeDate",
    "boolean": "esriFieldTypeString",
    "uuid": "esriFieldTypeGUID",
}
# layer_fields (data_type, numeric_subtype) -> esri field type (V044/V051 registry).
_PROPS_TO_ESRI = {
    ("number", "short_integer"): "esriFieldTypeSmallInteger",
    ("number", "long_integer"): "esriFieldTypeInteger",
    ("number", "float"): "esriFieldTypeSingle",
    ("number", "double"): "esriFieldTypeDouble",
    ("number", None): "esriFieldTypeDouble",
    ("date", None): "esriFieldTypeDate",
    ("boolean", None): "esriFieldTypeString",
}
# The QGIS write-chute's synthetic field (decision §2.3 + §9 rig note; A-0-resolved name). It
# exists ONLY in façade metadata — no storage column — so exports/OAPIF never see it.
_CHUTE_FIELD = "_talus_attach"

_ESRI_GEOM = {"point": "esriGeometryPoint", "line": "esriGeometryPolyline", "polygon": "esriGeometryPolygon"}
_EDIT_TOOL = {"point": "esriFeatureEditToolPoint", "line": "esriFeatureEditToolLine",
              "polygon": "esriFeatureEditToolPolygon", "none": "esriFeatureEditToolNone"}


# --- plumbing -------------------------------------------------------------------


def _wants_pretty(params: dict[str, str]) -> bool:
    return params.get("f", "json").lower() == "pjson"


def _json_esri(payload: Any, *, pretty: bool = False, status_code: int = 200,
               cors_any: bool = False) -> Response:
    """Plain JSON (the AFS path has no preprocessor — no escape neutralizers), compact unless
    pjson. no-store for the same Qt-disk-cache + credentialed-payload reasons as OGC. cors_any
    stamps `Access-Control-Allow-Origin: *` — the PUBLIC door only (fork 9a: the AGO Map Viewer
    fetches cross-origin from *.arcgis.com; public-door content is anon-visible by definition, so
    the wildcard discloses nothing)."""
    headers = {"Cache-Control": "no-store"}
    if cors_any:
        headers["Access-Control-Allow-Origin"] = "*"
    return Response(
        content=json.dumps(jsonable_encoder(payload),
                           separators=(",", ": ") if pretty else (",", ":"),
                           indent=2 if pretty else None),
        media_type="application/json",
        status_code=status_code,
        headers=headers,
    )


class _EsriBadRequest(Exception):
    """A client error reported the Esri way: HTTP 400 with {"error":{code,message}} — real HTTP
    status (honest, web-tier posture) AND the body shape Esri clients know how to display."""
    def __init__(self, message: str):
        self.message = message


def _esri_error_doc(code: int, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": []}}


# A query-type operation's POST body carries parameters only (where, objectIds, a filter geometry,
# outFields…). 4 MiB is far above the largest real one — ArcGIS Pro's densest "select by polygon" —
# and it is read as a stream so an over-size body is refused BEFORE it is held in memory: these doors
# buffer the body before the principal is known, and several are anonymous
# (decision-once-over-security-fixes.md A2). The edit operations carry feature payloads and are
# bounded by the api-wide request-body ceiling instead (A8).
_QUERY_FORM_MAX_BYTES = 4 * 1024 * 1024


async def _read_capped_body(request: Request, limit: int) -> bytes:
    """The request body, or 413 once it exceeds `limit` — by its declared length when there is one,
    otherwise by counting as it arrives. Never buffers more than `limit` bytes."""
    too_large = HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail="request body too large for this operation",
    )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


async def _request_params(request: Request, *, edit: bool = False) -> dict[str, str]:
    """Query params overlaid with form params (Esri operations accept GET and POST
    form-encoded interchangeably; POST wins on key collision, matching ArcGIS Server).
    `edit=True` (the applyEdits family) skips the query-body cap; see _QUERY_FORM_MAX_BYTES."""
    params: dict[str, str] = dict(request.query_params)
    if request.method == "POST":
        # Parsed by hand (urllib.parse_qsl), not request.form(): starlette 0.48 demands
        # python-multipart for ANY form parsing (urlencoded included) — an entire dependency for
        # one splitter — and hand-parsing also keeps the leniency OURS: QGIS embeds pretty-printed
        # JSON (raw newlines/quotes) in the `features` value with only %-critical bytes escaped
        # (recon §1.1), which parse_qsl handles exactly.
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype in ("application/x-www-form-urlencoded", ""):
            if edit:
                raw = await request.body()
            else:
                raw = await _read_capped_body(request, _QUERY_FORM_MAX_BYTES)  # 413 past the cap
            try:
                body = raw.decode("utf-8", errors="replace")
                params.update(dict(parse_qsl(body, keep_blank_values=True)))
            except Exception as exc:  # noqa: BLE001 — undecodable body: fall through to the query
                log.warning("esri_form_parse_failed", detail=str(exc), error=exc.__class__.__name__)
    return params


def _epoch_ms(value: Any) -> int | None:
    """esriFieldTypeDate wire value: epoch milliseconds UTC. Accepts datetimes (real columns via
    psycopg), ISO strings (props jsonb date values, 'YYYY-MM-DD' per V047)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp() * 1000)
    if isinstance(value, str):
        try:
            return _epoch_ms(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


# --- spatial reference handling (4326 native; 3857/102100 by closed form) ------------------------


def _norm_wkid(raw: str | None) -> int | None:
    """Normalize an SR request param (bare wkid or a JSON SR object) to 4326 or 3857.
    None input = server default (4326). Unsupported → _EsriBadRequest (honest 400 — v1 serves
    WGS-84 plus Web Mercator's exact closed form only; the Pro Step-0 capture tells us if more
    is ever demanded)."""
    if raw is None or raw.strip() == "":
        return None
    txt = raw.strip()
    wkid: Any = None
    if txt.startswith("{"):
        try:
            doc = json.loads(txt)
            wkid = doc.get("latestWkid") or doc.get("wkid")
        except ValueError:
            raise _EsriBadRequest(f"unparseable spatial reference: {txt[:80]}")
    else:
        try:
            wkid = int(float(txt))
        except ValueError:
            raise _EsriBadRequest(f"unparseable spatial reference: {txt[:80]}")
    if wkid in (4326,):
        return 4326
    if wkid in (3857, 102100):
        return 3857
    raise _EsriBadRequest(f"unsupported spatial reference wkid {wkid} — this server speaks 4326 and 3857/102100")


def _to_merc(lon: float, lat: float) -> tuple[float, float]:
    lat = max(min(lat, 89.9999), -89.9999)  # clamp the projection's poles
    return _R * math.radians(lon), _R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _from_merc(x: float, y: float) -> tuple[float, float]:
    return math.degrees(x / _R), math.degrees(2 * math.atan(math.exp(y / _R)) - math.pi / 2)


def _sr_doc(wkid: int | None) -> dict[str, int]:
    return _MERC_SR if wkid == 3857 else _WGS84_SR


# --- esriJSON geometry (out of GeoJSON, which is what feature_page emits) ------------------------


def _ring_cw(ring: list[list[float]]) -> bool:
    """Shoelace: negative signed area == clockwise (planar approximation is fine for winding)."""
    area = 0.0
    for i in range(len(ring) - 1):
        area += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return area < 0


def _oriented(ring: list[list[float]], clockwise: bool) -> list[list[float]]:
    return ring if _ring_cw(ring) == clockwise else ring[::-1]


def _xy(pt: list[float], wkid: int | None) -> list[float]:
    if wkid == 3857:
        x, y = _to_merc(pt[0], pt[1])
        return [x, y]
    return [pt[0], pt[1]]


def _geojson_to_esri(geom: dict[str, Any] | None, wkid: int | None) -> dict[str, Any] | None:
    """GeoJSON (WGS-84, from feature_page) → esriJSON, optionally projected to Web Mercator.
    Esri winding: exterior rings CW, holes CCW (GeoJSON's is the opposite; PostGIS output isn't
    guaranteed either way — orient computationally). Per-geometry spatialReference is omitted:
    the response's top-level spatialReference governs, which is also what QGIS reads."""
    if geom is None:
        return None
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point":
        x, y = _xy(coords, wkid)
        return {"x": x, "y": y}
    if gtype == "MultiPoint":
        return {"points": [_xy(c, wkid) for c in coords]}
    if gtype == "LineString":
        return {"paths": [[_xy(c, wkid) for c in coords]]}
    if gtype == "MultiLineString":
        return {"paths": [[_xy(c, wkid) for c in part] for part in coords]}
    if gtype == "Polygon":
        coords = [coords]  # treat as a one-polygon MultiPolygon
        gtype = "MultiPolygon"
    if gtype == "MultiPolygon":
        rings: list[list[list[float]]] = []
        for poly in coords:
            for i, ring in enumerate(poly):
                pts = [_xy(c, wkid) for c in ring]
                rings.append(_oriented(pts, clockwise=(i == 0)))
        return {"rings": rings}
    return None  # geometry family the registry can't produce


# --- field metadata (the /0 mapper) ---------------------------------------------------------------


async def _layer_row(conn: AsyncConnection, layer_id: str) -> dict[str, Any]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT display_name, geom_type, editable, editing_enabled, attachments_enabled, "
            "display_field, edit_add, edit_attributes, edit_geometry, edit_delete "
            "FROM platform.layers WHERE layer_id = %s AND status = 'ready'",
            (layer_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return {"display_name": row[0], "geom_type": row[1], "editable": row[2],
            "editing_enabled": row[3], "attachments_enabled": row[4], "display_field": row[5],
            # V109 (decision-layer-edit-capabilities.md): the owner's four capability flags — they shape
            # `capabilities` / allowGeometryUpdates and refuse per edit in applyEdits.
            "edit_add": bool(row[6]), "edit_attributes": bool(row[7]),
            "edit_geometry": bool(row[8]), "edit_delete": bool(row[9])}


async def _feature_uuid(conn: AsyncConnection, layer_id: str, objectid: int) -> str | None:
    """OBJECTID → the feature's uuid in assets.<layer_id> (the V064 identity the chute URLs are
    addressed by). Call ONLY after the layer is proven registered (sql.Identifier then names one
    of a fixed known-safe set). A layer with no objectid column (a PG view layer) reads as
    not-found — honest, and the edit gate already excludes those from writes."""
    q = sql.SQL("SELECT id::text FROM {tbl} WHERE objectid = %s").format(
        tbl=sql.Identifier("assets", layer_id))
    try:
        async with conn.cursor() as cur:
            await cur.execute(q, (objectid,))
            row = await cur.fetchone()
    except pg_errors.UndefinedColumn:
        await conn.rollback()  # the failed SELECT aborted the tx; clear it before the 404 path
        return None
    return row[0] if row is not None else None


async def _advertised_base(conn: AsyncConnection, request: Request) -> str:
    """The org's advertised base URL (Admin → Settings — the LAN-reachable base every desktop
    recipe is minted with), falling back to this request's own base when unset. No trailing
    slash. Used to mint the chute echo URLs, so they resolve from the same machine QGIS is on."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT advertised_base_url FROM platform.org_settings WHERE id = true")
        row = await cur.fetchone()
    base = (row[0] or "").strip() if row is not None else ""
    if not base:
        base = str(request.base_url)
    return base.rstrip("/")


def _capabilities(layer_id: str, meta: dict[str, Any]) -> str:
    """`Query` always (CASE-SENSITIVE in QGIS's service-root check); edit tokens describe the
    LAYER's edit surface — the hard editable flag AND the owner's editing_enabled switch, AND
    (v1) the layer must be a generic-write-path hosted layer (tal_*): a `vw_*` view layer is
    read-only by design (edits happen on its source slot), so advertising editing on it would
    be a lie the first save exposes. The per-CALLER gates (sharing, PAT scope, role) stay
    enforcement-side: capabilities are honest about the layer, authorization decides about
    the user.

    V109 (decision-layer-edit-capabilities.md §3.4): the edit tokens come from the owner's four flags —
    `Create` ⇔ add, `Update` ⇔ attributes OR geometry (Esri has no finer token; allowGeometryUpdates
    carries the geometry half), `Delete` ⇔ delete, `Editing` whenever any of them is on. Pro and QGIS
    shape their editing tools from this string, so a crew on an attributes-only layer never sees a
    Delete they cannot use."""
    if not (_SLOT_RE.fullmatch(layer_id) and meta["editable"] and meta["editing_enabled"]):
        return "Query"
    tokens = ["Query"]
    if meta["edit_add"]:
        tokens.append("Create")
    if meta["edit_attributes"] or meta["edit_geometry"]:
        tokens.append("Update")
    if meta["edit_delete"]:
        tokens.append("Delete")
    if len(tokens) > 1:
        tokens.append("Editing")
    return ",".join(tokens)


def _op_deny(meta: dict[str, Any], op: str) -> str | None:
    """The policy sentence when the layer's flag for `op` is off, else None (V109). The façade's
    per-edit refusal — reported per RESULT (never a top-level error: the recon trap), for everyone,
    admins included (F1)."""
    return None if meta[f"edit_{op}"] else EDIT_OP_DENIED[op]


async def _custom_field_rows(conn: AsyncConnection, layer_id: str) -> list[dict[str, Any]]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT field_key, label, data_type, required, constraints, numeric_subtype, domain_id::text "
            "FROM platform.layer_fields WHERE layer_id = %s AND active ORDER BY sort_order, field_key",
            (layer_id,),
        )
        rows = await cur.fetchall()
    return [
        {"key": r[0], "label": r[1], "dtype": r[2], "required": r[3],
         "constraints": r[4] or {}, "subtype": r[5], "domain_id": r[6]}
        for r in rows
    ]


async def _custom_domain_doc(conn: AsyncConnection, domain_id: str) -> dict[str, Any] | None:
    """A platform.domains (V046) domain as an esri codedValue domain — inlined per layer (esri
    domains are embedded copies; org-wide reuse flattens cleanly). Retired values INCLUDED: the
    validation trigger accepts them on legacy rows, so the desktop must be able to render them."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT name FROM platform.domains WHERE id = %s::uuid", (domain_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        name = row[0]
        await cur.execute(
            "SELECT code, label FROM platform.domain_values WHERE domain_id = %s::uuid "
            "ORDER BY sort_order, code",
            (domain_id,),
        )
        values = await cur.fetchall()
    return {"type": "codedValue", "name": name,
            "codedValues": [{"name": lbl, "code": code} for code, lbl in values]}


def _chute_ok(layer_id: str, meta: dict[str, Any]) -> bool:
    """Should this layer advertise the QGIS write-chute field? Attachments on AND the layer is a
    generic-write-path hosted layer that is `editable` — both STRUCTURAL properties, fixed for the
    life of the layer, so the advertised field surface is stable.

    ★ Deliberately does NOT gate on `editing_enabled` (decision-editing-toggle.md §9). It used to,
    on the reasoning that the field shouldn't appear where a chute PUT "would 403 anyway" — but that
    argues for a runtime denial, which we already do, not for a vanishing column. Because this feeds
    the layer document (:846), `query` (:1052) AND applyEdits (:1654), gating on the toggle meant an
    admin flipping Editing off in Manage → Settings silently changed the layer's SCHEMA for every
    already-connected client. Measured on 2026-07-25: 6 fields → 5 → 6, with the toggle as the only
    variable. Connected desktop clients then threw while serializing against a field that had
    ceased to exist — *"The index passed was not within the valid range"* and *"The value type is
    incompatible with the field type [_talus_attach]"*, naming a field the user has never heard of,
    from an action they did not take. It also disqualified the editing fence as a deny vector and
    produced two outcome-C nulls before the leg-3 measurement finally landed on a read-only PAT.

    The line: `_capabilities` depending on `editing_enabled` is CORRECT — that is a property of the
    layer, and a client can represent "this layer is now read-only". A client cannot represent "a
    field I cached for serialization has ceased to exist". Capabilities may move; the field list
    must not.

    Advertisement only — nothing here permits a write. `_talus_attach` is synthetic with no storage
    column (:503), writes to it dissolve in `_split_attrs` (:441-442), every edit still passes
    `_edit_deny_reason` (which checks `editing_enabled` independently), and attachment uploads stay
    hard-gated on `attachments_enabled` in routers/attachments.py.
    """
    return bool(meta.get("attachments_enabled")) and bool(_SLOT_RE.fullmatch(layer_id)) \
        and bool(meta.get("editable"))


def _alias_case_duplicates(
    layer_id: str, fields: list[dict[str, Any]], lookup: dict[str, dict[str, Any]]
) -> list[tuple[str, str]]:
    """Fold `fields[]` so no two advertised names are equal under `.lower()`, aliasing any LATE
    duplicate to `<name>_1` (`_2`, …) in place. Returns the (original, alias) pairs it made — empty
    on every healthy layer. Pure and in-place on purpose: the DB path that could feed it a duplicate
    is closed by V087, so a unit test is the only way left to prove the seatbelt still works.

    ── Backstop, NOT policy (decision-esri-field-name-clash.md §3.4 — Q5 RULED: ship, read path) ──
    After V087 this is unreachable by construction: `objectid`/`globalid` are reserved in both the API
    (`_RESERVED_KEYS`) and the DB (`layer_fields_key_reserved`), and a reserved SOURCE column is
    renamed to `*_src` at ingest before it can ever reach the registry. That ingest rename is the
    policy (§3.1); door-level aliasing was weighed as the policy and rejected. This is the seatbelt.

    It exists because the bug it guards had exactly one signature: silence. A FeatureServer must never
    advertise two names equal under `.lower()` — gdb/SQLite names are case-insensitive, so OGR refuses
    the duplicate field and `QgsOfflineEditing` aborts the feature copy WHILE REPORTING SUCCESS: an
    empty GeoPackage, a project still pointed at the live service, and a field day gone. So the late
    duplicate is never dropped — it is aliased (Esri's own `_1` convention) and still served — and the
    recurrence is made LOUD. If this ever logs, that is information worth having.

    The alias needs no reverse mapping in `_split_attrs`: it renames the ADVERTISED name and its
    lookup key, while `source` still names the true column / props key, so a write addressed to the
    alias resolves back to the right storage on its own.
    """
    aliased: list[tuple[str, str]] = []
    taken: set[str] = set()
    for f in fields:
        original = f["name"]
        if original.lower() not in taken:
            taken.add(original.lower())
            continue
        alias, n = original, 0
        while alias.lower() in taken:
            n += 1
            alias = f"{original}_{n}"
        log.warning("esri_field_name_clash_aliased", layer_id=layer_id, name=original, alias=alias)
        f["name"] = alias
        taken.add(alias.lower())
        # Re-key the lookup, but only RETIRE the old key when no earlier field still advertises that
        # exact name. Two entries with an exactly-equal (not merely case-equal) name would otherwise
        # leave the survivor without a lookup row, and `_attributes_for` indexes lookup by declared
        # name — a KeyError mid-response, after headers are sent. Unreachable today, but a seatbelt
        # that can throw is a poor seatbelt.
        spec = lookup.get(original)
        if spec is not None:
            lookup[alias] = spec
            if not any(g["name"] == original for g in fields if g is not f):
                lookup.pop(original, None)
        aliased.append((original, alias))
    return aliased


async def _esri_fields(
    conn: AsyncConnection, layer_id: str, *, is_anon: bool, chute: bool = False
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """The layer's esri `fields[]` + a lookup {name -> {source, esri_type, ...}} the query
    serializer uses to pull each declared attribute from a feature_page row. Field surface:
    OBJECTID · GlobalID (read-only — the uuid under its Esri contract name) · every real data
    column (system audit timestamps read-only; excluded plumbing per _EXCLUDED_COLUMNS) ·
    every active owner-defined field (V044 registry) · when
    attachments_enabled, the synthetic QGIS write-chute field (kind "chute" — no storage column;
    reads echo the newest attachment's chute URL, writes dissolve in _split_attrs)."""
    fields: list[dict[str, Any]] = [
        {"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID",
         "sqlType": "sqlTypeOther", "nullable": False, "editable": False, "domain": None,
         "defaultValue": None},
        # The feature uuid, surfaced under the Esri contract name. `esriFieldTypeGlobalID` (not
        # ...GUID) + `globalIdField` naming it is what Pro's Append-with-attachments /
        # preserve-Global-IDs gates on: without it Pro aborts CLIENT-side ("WARNING 000594:
        # Function not implemented") and never sends a feature (rig 2026-07-18). editable:False is
        # both Esri-faithful (GlobalID is a system field) and the fence that keeps Append from
        # mapping a source column onto it — preserve intent rides `useGlobalIds`, not this field.
        # length 38 = the braced-uppercase GUID form Esri uses on the wire ("{...}" = 36 + 2). Recon
        # 2026-07-20: the reference hosted service declares it and we did not, and Pro was seen
        # rewriting its own field map from `GlobalID … true 0` to `… true 38` on reload (rig) — i.e.
        # Pro was supplying the length we omitted, not re-deriving something harmless.
        {"name": "GlobalID", "type": "esriFieldTypeGlobalID", "alias": "GlobalID",
         "sqlType": "sqlTypeOther", "length": 38, "nullable": False, "editable": False,
         "domain": None, "defaultValue": None},
    ]
    lookup: dict[str, dict[str, Any]] = {
        "OBJECTID": {"source": "objectid", "kind": "real", "esri_type": "esriFieldTypeOID"},
        "GlobalID": {"source": "id", "kind": "fid", "esri_type": "esriFieldTypeGlobalID"},
    }

    cols = await _columns(conn, layer_id)
    from .layer_query import _ANON_SCRUB_COLUMNS  # keep the anon posture identical to /features
    for name, pg_type in cols.items():
        if name in _EXCLUDED_COLUMNS or (is_anon and name in _ANON_SCRUB_COLUMNS):
            continue
        esri_type = _PG_TO_ESRI.get(pg_type, "esriFieldTypeString")
        f: dict[str, Any] = {
            "name": name, "type": esri_type, "alias": name, "sqlType": "sqlTypeOther",
            "nullable": True, "editable": name not in _READONLY_COLUMNS,
            "domain": None, "defaultValue": None,
        }
        if esri_type == "esriFieldTypeString":
            f["length"] = 100000 if pg_type in ("text", "boolean") else 255
        fields.append(f)
        lookup[name] = {"source": name, "kind": "real", "esri_type": esri_type,
                        "pg_type": pg_type}

    for row in await _custom_field_rows(conn, layer_id):
        dtype, subtype = row["dtype"], row["subtype"]
        if dtype == "text" or dtype == "coded":
            esri_type = "esriFieldTypeString"
        else:
            esri_type = _PROPS_TO_ESRI.get((dtype, subtype)) or _PROPS_TO_ESRI.get((dtype, None), "esriFieldTypeString")
        domain = await _custom_domain_doc(conn, row["domain_id"]) if row["domain_id"] else None
        f = {
            "name": row["key"], "type": esri_type, "alias": row["label"] or row["key"],
            "sqlType": "sqlTypeOther", "nullable": not row["required"], "editable": True,
            "domain": domain, "defaultValue": None,
        }
        if esri_type == "esriFieldTypeString":
            f["length"] = int(row["constraints"].get("maxLength") or (5 if dtype == "boolean" else 255))
        fields.append(f)
        lookup[row["key"]] = {"source": row["key"], "kind": "props", "esri_type": esri_type,
                              "dtype": dtype}

    # ── Backstop, NOT policy (decision-esri-field-name-clash.md §3.4 — Q5 RULED: ship, read path) ──
    # After V087 this loop is unreachable by construction: `objectid`/`globalid` are reserved in both
    # the API (_RESERVED_KEYS) and the DB (layer_fields_key_reserved), and a reserved source column is
    # renamed to `*_src` at INGEST before it can reach the registry. That ingest rename is the policy
    # (§3.1); door-level aliasing was considered as the policy and rejected. This is the seatbelt.
    #
    # It exists because the bug it guards had exactly one signature: silence. A FeatureServer must
    # never advertise two names equal under .lower() — gdb/SQLite names are case-insensitive, so OGR
    # refuses the duplicate field and QgsOfflineEditing aborts the feature copy WHILE REPORTING
    # SUCCESS: an empty GeoPackage, a project still pointed at the live service, and a field day gone.
    # So: never drop the late duplicate — alias it (Esri's own `_1` convention) and keep serving it,
    # and make the recurrence LOUD. If this warning is ever emitted, that is information worth having.
    #
    # The alias needs no reverse mapping in `_split_attrs`: it renames the ADVERTISED name and its
    # lookup key, while `source` still names the true column / props key — so a write addressed to the
    # alias resolves back to the right storage on its own.
    # The §3.4 backstop — unreachable after V087, loud if it ever fires. See the helper's docstring.
    _alias_case_duplicates(layer_id, fields, lookup)

    if chute:
        # The QGIS write-chute field (decision §2.3): synthetic + computed — NO storage column, so
        # it structurally cannot reach the OAPIF door or any export. Reserved name collision-checked
        # against the real surface (fallback _talus_attach_1, _talus_attach_2, …). The alias is the
        # self-documenting "ignore me" for Pro users, who see the same metadata.
        taken = {f["name"].lower() for f in fields}
        chute_name = _CHUTE_FIELD
        n = 0
        while chute_name.lower() in taken:
            n += 1
            chute_name = f"{_CHUTE_FIELD}_{n}"
        fields.append({
            "name": chute_name, "type": "esriFieldTypeString",
            "alias": "QGIS attachment upload — ignore in Pro", "sqlType": "sqlTypeOther",
            "nullable": True, "editable": True, "domain": None, "defaultValue": None,
            "length": 1024,
        })
        lookup[chute_name] = {"source": None, "kind": "chute", "esri_type": "esriFieldTypeString"}
    return fields, lookup


def _attributes_for(feature: dict[str, Any], names: list[str],
                    lookup: dict[str, dict[str, Any]],
                    chute_url: str | None = None) -> dict[str, Any]:
    """One feature_page GeoJSON feature → the declared esri attributes (dates → epoch ms;
    booleans → "true"/"false" strings, fork 8b; GUID in esri's braced-uppercase form). `chute_url`
    is the SERVER-COMPUTED newest-attachment URL for this feature (or None) — the chute field's
    read value is always derived from media.attachments, never stored (no second source of truth)."""
    props = feature.get("properties") or {}
    nested = props.get("props") or {}
    out: dict[str, Any] = {}
    for name in names:
        spec = lookup[name]
        if spec["kind"] == "fid":
            out[name] = "{" + str(feature.get("id", "")).upper() + "}"
            continue
        if spec["kind"] == "chute":
            out[name] = chute_url
            continue
        raw = props.get(spec["source"]) if spec["kind"] == "real" else nested.get(spec["source"])
        if spec["esri_type"] == "esriFieldTypeDate":
            out[name] = _epoch_ms(raw)
        elif isinstance(raw, bool):
            out[name] = "true" if raw else "false"
        else:
            out[name] = raw
    return out


# --- the WHERE-subset compiler (fork/§2.4 grammar → the house filter strings) --------------------

_CLAUSE_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(=|<>|!=|>=|<=|>|<|\s+IN\s|\s+LIKE\s)\s*(.+?)\s*$",
    re.IGNORECASE,
)
_OP_MAP = {"=": "eq", "<>": "ne", "!=": "ne", ">=": "gte", "<=": "lte", ">": "gt", "<": "lt"}


def _literal(text: str) -> str:
    """One SQL literal → its bound-value string. Quoted strings ('' unescapes), bare numbers, and
    DATE/TIMESTAMP 'x' literals (the inner ISO text — psycopg binds strings untyped, so PG casts
    them against date/timestamp columns contextually). Anything else is refused loudly."""
    t = text.strip()
    m = re.match(r"^(?:DATE|TIMESTAMP)\s+('.*')$", t, re.IGNORECASE)
    if m:
        t = m.group(1)
    if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
        return t[1:-1].replace("''", "'")
    if re.match(r"^-?\d+(\.\d+)?$", t):
        return t
    raise _EsriBadRequest(f"unsupported literal in where clause: {text.strip()[:80]}")


def _scan_grammar(text: str):
    """Walk `text` yielding (index, char) for characters that are GRAMMAR — i.e. OUTSIDE any quoted
    literal. `''` inside a literal is an escaped quote, matching `_literal`'s own unescaping.

    Everything below keys off this. The check it replaces was
    `text.upper().replace("IN (", "").replace("IN(", "")`, a blind substring strip that made the
    gate's outcome depend on the caller's DATA: a literal containing `IN(` had its paren silently
    removed and slipped through, while a literal containing a bare `(` was refused. Parens inside a
    literal are data, not grammar."""
    in_quote = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_quote:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 1          # escaped quote — stay inside the literal
                else:
                    in_quote = False
        elif ch == "'":
            in_quote = True
        else:
            yield i, ch
        i += 1


def _strip_outer_parens(text: str) -> str:
    """Remove parentheses that redundantly wrap the ENTIRE expression, repeatedly (`((X))` -> `X`).

    This is the whole of the grouping support we offer (decision-where-parser-grouping.md, Option A).
    ArcGIS Pro wraps its WHERE as soon as there is more than one predicate — its post-edit refresh
    emits `(OBJECTID IN(…) AND OBJECTID NOT IN(<dirty>))` — and refusing that blanked the attribute
    table after every edit, recoverable only by re-adding the layer, which discards pending edits.

    Strips ONLY when the opening paren's match is the final character. `(A) AND (B)` and
    `(1=1) OR 1=1` are therefore left intact and still refused downstream — the parens there are
    internal grouping, which v1 does not do. Unbalanced input is left intact too and refused.

    ★ ONE PASS (decision-once-over-security-fixes.md A2). The first version rescanned the whole
    string for every wrapper it peeled — quadratic in nesting depth, and a 7.8 KB `where` of 4,000
    nested parentheses held the api's only event loop for a second (1 MB: hours). Now every grammar
    paren is matched to its partner once, and peeling is a walk inward over that table. The previous
    function is kept verbatim in tests/test_geoservices_where_limits.py as the oracle this one is
    compared against, on a table of cases and on random input."""
    if not text.startswith("("):
        return text
    partner: dict[int, int] = {}       # index of a grammar "(" -> index of the ")" that closes it
    open_at: list[int] = []
    for i, ch in _scan_grammar(text):
        if ch == "(":
            open_at.append(i)
        elif ch == ")" and open_at:
            partner[open_at.pop()] = i
    lo, hi = 0, len(text) - 1
    while lo <= hi and text[lo] == "(" and partner.get(lo) == hi:
        inner_lo, inner_hi = lo + 1, hi - 1
        while inner_lo <= inner_hi and text[inner_lo].isspace():
            inner_lo += 1
        while inner_hi >= inner_lo and text[inner_hi].isspace():
            inner_hi -= 1
        if inner_lo > inner_hi:
            break                    # `()` — leave it to be refused
        lo, hi = inner_lo, inner_hi
    return text[lo:hi + 1]


# --- the WHERE caps (decision-once-over-security-fixes.md A2) ------------------------------------
# The second lock behind the linear stripper. Sizing: the service advertises maxRecordCount 1,000,
# so ArcGIS Pro's `OBJECTID IN(…)` refresh is ~8 KB at its largest and wraps two or three deep; no
# client pads with whitespace. Several regexes below (`\s+AND\s+`, the clause pattern's `\s*…\s*$`)
# cost the SQUARE of a whitespace run's length, in or out of a literal — capping the run is what
# keeps every one of them linear in the size of the whole string, without having to find them all.
_WHERE_MAX_CHARS = 64_000
_WHERE_MAX_DEPTH = 16
_WHERE_MAX_WS_RUN = 64
_WS_RUN_OVER_RE = re.compile(r"\s{%d}" % (_WHERE_MAX_WS_RUN + 1))


def _check_where_limits(where: str) -> None:
    """Refuse an oversize, over-nested or whitespace-padded `where` BEFORE any parsing. Each check is
    one linear pass, and the length check runs first so the other two are bounded by it."""
    if len(where) > _WHERE_MAX_CHARS:
        raise _EsriBadRequest(
            f"where clause too long ({len(where)} characters; the limit is {_WHERE_MAX_CHARS})")
    if _WS_RUN_OVER_RE.search(where):
        raise _EsriBadRequest("unsupported where clause: too much consecutive whitespace")
    depth = 0
    for _i, ch in _scan_grammar(where):
        if ch == "(":
            depth += 1
            if depth > _WHERE_MAX_DEPTH:
                raise _EsriBadRequest("unsupported where clause: parentheses nested too deeply")
        elif ch == ")":
            depth -= 1


_MASK_FILL = "\x00"  # non-whitespace, non-word: see _grammar_mask


def _grammar_mask(text: str) -> str:
    """`text` with every quoted literal blanked out, INDEXES PRESERVED.

    The companion to `_scan_grammar` for checks that are easier to express as a regex than as a
    character walk. Because offsets are unchanged, a match on the mask slices the ORIGINAL string.

    Why it exists: `_scan_grammar` taught the PAREN check that a literal's contents are data, but
    the keyword checks in `_compile_where` predate it and kept scanning raw text — so `status =
    'not started'` was refused as though the user had typed a NOT operator, while the same value
    sailed through `status IN ('not started')` because the IN branch matches first. Same value,
    same layer, opposite answers, decided by the caller's DATA. Every one of those was an
    over-broad refusal (a false 400, never a way past the gate), but 'not started' is an
    unremarkable municipal status value and the error named a clause the user never wrote.

    ★ The fill is deliberately NOT a space. Blanking to whitespace makes the masked literal part of
    the `\\s+AND\\s+` separator, so `status = 'x' AND OBJECTID > 5` splits into `status =` and
    `OBJECTID > 5` — the literal is eaten by the split it was supposed to be hidden from. NUL is
    non-whitespace and non-word, so it neither joins a separator nor forms a keyword.
    """
    out = [_MASK_FILL] * len(text)
    for i, ch in _scan_grammar(text):
        out[i] = ch
    return "".join(out)


def _has_ungrouped_paren(text: str) -> bool:
    """True if a GRAMMAR paren remains that is not an `IN(...)` list opener — i.e. real grouping or a
    function call, neither of which v1 supports. Run AFTER `_strip_outer_parens`.

    An opener counts as an IN-list only when the token immediately before it is the bare word `IN`,
    so `upper(` and a field literally named `MAIN(` are still refused."""
    for i, ch in _scan_grammar(text):
        if ch != "(":
            continue
        j = i - 1
        while j >= 0 and text[j].isspace():
            j -= 1
        if j >= 1 and text[j - 1:j + 1].upper() == "IN" and (
            j - 1 == 0 or not (text[j - 2].isalnum() or text[j - 2] == "_")
        ):
            continue                 # `… IN(` / `… NOT IN (` — the one paren shape v1 accepts
        return True
    return False


def _compile_where(where: str | None, field_lookup: dict[str, dict[str, Any]]) -> list[str]:
    """The v1 SQL-92 WHERE subset → house `field:op:value` filter strings (decision doc §2.4):
    `1=1`, comparisons (= <> != > < >= <=), IN (...), NOT IN (...) — ArcGIS Pro excludes its
    in-session edits via `OBJECTID NOT IN(…)` on EVERY fetch while editing, so it's mandatory —
    LIKE 'pattern' (verbatim wildcards), AND-joined. OR / parentheses / IS NULL / functions →
    an honest esri 400, never a passthrough. Field names resolve case-insensitively against the
    DECLARED esri fields (so OBJECTID works), then map to their storage source; validation
    beyond that stays in _parse_filters."""
    if where is None:
        return []
    _check_where_limits(where)       # size, nesting, whitespace: refused before any parsing (A2)
    # Strip redundant whole-expression wrappers BEFORE anything else, so every check below — the
    # `1=1` short-circuit included — sees the same interior it would have seen unwrapped.
    text = _strip_outer_parens(where.strip())
    if text == "" or re.fullmatch(r"1\s*=\s*1", text):
        return []
    if _has_ungrouped_paren(text):
        # a paren that isn't an IN list = real grouping or a function call — not done in v1
        raise _EsriBadRequest("unsupported where clause: parentheses/grouping are not supported")
    # Keyword checks and the AND split run on GRAMMAR ONLY — a literal's contents are the caller's
    # data and must not steer the gate (see _grammar_mask). Slicing stays on `text`, since the mask
    # preserves offsets.
    mask = _grammar_mask(text)
    if re.search(r"\bOR\b|\bIS\s+NULL\b|\bIS\s+NOT\b", mask, re.IGNORECASE):
        raise _EsriBadRequest("unsupported where clause: only AND-joined comparisons, IN and LIKE are supported")
    by_lower = {k.lower(): k for k in field_lookup}
    specs: list[str] = []
    clauses, cut = [], 0
    for m_and in re.finditer(r"\s+AND\s+", mask, re.IGNORECASE):
        clauses.append(text[cut:m_and.start()])
        cut = m_and.end()
    clauses.append(text[cut:])
    for clause in clauses:
        m_in = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+(NOT\s+)?IN\s*\((.+)\)\s*$",
                        clause, re.IGNORECASE)
        if m_in:
            fname, body = m_in.group(1), m_in.group(3)
            values = [_literal(v) for v in _split_in_list(body)]
            if any("," in v for v in values):
                raise _EsriBadRequest("IN values containing commas are not supported")
            op, value = ("notin" if m_in.group(2) else "in"), ",".join(values)
        else:
            # Grammar only — a clause split on grammar whitespace has balanced quotes, so the
            # mask can be recomputed on the fragment.
            if re.search(r"\bNOT\b", _grammar_mask(clause), re.IGNORECASE):  # NOT outside NOT IN
                raise _EsriBadRequest(f"unsupported where clause: {clause.strip()[:80]}")
            m = _CLAUSE_RE.match(clause)
            if not m:
                raise _EsriBadRequest(f"unsupported where clause: {clause.strip()[:80]}")
            fname, rawop, rawval = m.group(1), m.group(2).strip().upper(), m.group(3)
            if rawop == "LIKE":
                op, value = "like", _literal(rawval)
            elif rawop == "IN":  # `IN` without parens — malformed
                raise _EsriBadRequest(f"unsupported where clause: {clause.strip()[:80]}")
            else:
                op, value = _OP_MAP[rawop], _literal(rawval)
        declared = by_lower.get(fname.lower())
        if declared is None:
            raise _EsriBadRequest(f"unknown field in where clause: {fname}")
        source = field_lookup[declared]["source"]
        if field_lookup[declared]["kind"] == "fid":
            # GlobalID filters on the uuid PK. Only identity ops make sense on a GUID — a range
            # comparison would be meaningless, so refuse it rather than emit a lexicographic
            # surprise. Values normalize to the bare lowercase uuid, which is what `id::text`
            # compares against (_parse_filters casts the column, so no SQL-side cast is needed).
            # (`rawop` is unbound on the IN branch — report `op`, which is always set.)
            if op not in ("eq", "ne", "in", "notin"):
                raise _EsriBadRequest(f"GlobalID supports = <> and IN only, not '{op}'")
            parts = [_parse_global_id(v) for v in value.split(",")]
            if any(p is None for p in parts):
                raise _EsriBadRequest("GlobalID filter values must be GUIDs (braced or bare)")
            specs.append(f"{source}:{op}:" + ",".join(p for p in parts if p))
            continue
        if field_lookup[declared]["kind"] == "chute":
            raise _EsriBadRequest(f"{declared} is a computed attachment field — not filterable")
        specs.append(f"{source}:{op}:{value}")
    return specs


def _split_in_list(body: str) -> list[str]:
    """Split an IN(...) body on commas OUTSIDE single quotes."""
    parts, buf, in_q = [], [], False
    for ch in body:
        if ch == "'":
            in_q = not in_q
            buf.append(ch)
        elif ch == "," and not in_q:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p for p in (s.strip() for s in parts) if p]


def _esri_filter_geojson(gtype: str, doc: dict[str, Any]) -> dict[str, Any]:
    """An Esri spatial-filter geometry (polygon / polyline / multipoint / point) as GeoJSON, for an
    exact ST_Intersects. Esri rings/paths map structurally onto GeoJSON coordinate arrays; winding
    is irrelevant to ST_Intersects (the ST_MakeValid downstream guards a malformed ring)."""
    if gtype == "esriGeometryPolygon":
        rings = doc.get("rings")
        if not isinstance(rings, list) or not rings:
            raise _EsriBadRequest("unparseable polygon geometry")
        return {"type": "Polygon", "coordinates": rings}
    if gtype == "esriGeometryPolyline":
        paths = doc.get("paths")
        if not isinstance(paths, list) or not paths:
            raise _EsriBadRequest("unparseable polyline geometry")
        return {"type": "MultiLineString", "coordinates": paths}
    if gtype == "esriGeometryMultipoint":
        pts = doc.get("points")
        if not isinstance(pts, list) or not pts:
            raise _EsriBadRequest("unparseable multipoint geometry")
        return {"type": "MultiPoint", "coordinates": pts}
    if gtype == "esriGeometryPoint":
        try:
            return {"type": "Point", "coordinates": [float(doc["x"]), float(doc["y"])]}
        except (KeyError, TypeError, ValueError):
            raise _EsriBadRequest("unparseable point geometry")
    raise _EsriBadRequest(f"unsupported spatial filter geometryType {gtype}")


def _parse_spatial_filter(params: dict[str, str]) -> tuple[str | None, tuple[str, int] | None]:
    """Parse the query's spatial filter → ``(bbox, geom_intersect)``, at most one non-None:
    - ENVELOPE → ``(bbox_string_wgs84, None)`` — the fast index `&&` path (the common case both
      clients send);
    - POLYGON / POLYLINE / MULTIPOINT / POINT → ``(None, (geojson, srid))`` — an EXACT ST_Intersects.
      ArcGIS Pro sends its map extent as a rectangular *polygon* during editing (and returned 400
      before this — the rig 2026-07-18 index-error root cause); QGIS sends envelopes.
    Unsupported spatialRel → honest 400."""
    raw = params.get("geometry")
    if not raw:
        return None, None
    gtype = params.get("geometryType", "esriGeometryEnvelope")
    srel = params.get("spatialRel", "esriSpatialRelIntersects")
    if srel not in ("esriSpatialRelIntersects", "esriSpatialRelEnvelopeIntersects"):
        raise _EsriBadRequest(f"unsupported spatialRel {srel}")
    in_wkid = _norm_wkid(params.get("inSR"))
    try:
        doc = json.loads(raw) if raw.strip().startswith("{") else None
    except ValueError:
        raise _EsriBadRequest("unparseable geometry")
    if isinstance(doc, dict) and in_wkid is None and isinstance(doc.get("spatialReference"), dict):
        in_wkid = _norm_wkid(json.dumps(doc["spatialReference"]))
    srid = 3857 if in_wkid == 3857 else 4326

    if gtype == "esriGeometryEnvelope":
        if doc is not None:
            try:
                xmin, ymin, xmax, ymax = (float(doc["xmin"]), float(doc["ymin"]),
                                          float(doc["xmax"]), float(doc["ymax"]))
            except (ValueError, KeyError, TypeError):
                raise _EsriBadRequest("unparseable envelope geometry")
        else:
            try:
                xmin, ymin, xmax, ymax = (float(p) for p in raw.split(","))
            except ValueError:
                raise _EsriBadRequest("unparseable envelope geometry")
        if srid == 3857:  # keep the fast path in 4326; convert client-side (as before)
            xmin, ymin = _from_merc(xmin, ymin)
            xmax, ymax = _from_merc(xmax, ymax)
        return f"{xmin},{ymin},{xmax},{ymax}", None

    if doc is None:
        raise _EsriBadRequest(f"unparseable {gtype} geometry")
    # Non-envelope: exact intersect. Coords stay in their own CRS (srid); SQL ST_Transforms to 4326.
    return None, (json.dumps(_esri_filter_geojson(gtype, doc)), srid)


# --- doc builders ---------------------------------------------------------------------------------


async def _extent_doc(conn: AsyncConnection, layer_id: str, wkid: int | None = None) -> dict[str, Any]:
    bbox = await _extent_bbox(conn, layer_id)
    if bbox is None:
        bbox = [-180.0, -90.0, 180.0, 90.0]  # empty layer: QGIS hard-requires a parseable extent
    if wkid == 3857:
        xmin, ymin = _to_merc(bbox[0], bbox[1])
        xmax, ymax = _to_merc(bbox[2], bbox[3])
        bbox = [xmin, ymin, xmax, ymax]
    return {"xmin": bbox[0], "ymin": bbox[1], "xmax": bbox[2], "ymax": bbox[3],
            "spatialReference": _sr_doc(wkid)}


def _renderer(geom_type: str) -> dict[str, Any]:
    """A minimal simple renderer per family (Talus DS blue) — required for the layer to draw with
    default symbology in Pro/AGO; QGIS ignores it."""
    blue = [37, 99, 235, 255]
    if geom_type == "point":
        symbol: dict[str, Any] = {"type": "esriSMS", "style": "esriSMSCircle", "color": blue, "size": 8,
                                  "outline": {"color": [255, 255, 255, 255], "width": 1}}
    elif geom_type == "line":
        symbol = {"type": "esriSLS", "style": "esriSLSSolid", "color": blue, "width": 1.5}
    else:
        symbol = {"type": "esriSFS", "style": "esriSFSSolid", "color": [37, 99, 235, 64],
                  "outline": {"type": "esriSLS", "style": "esriSLSSolid", "color": blue, "width": 1}}
    return {"renderer": {"type": "simple", "symbol": symbol}}


async def _layer_doc(conn: AsyncConnection, layer_id: str, meta: dict[str, Any],
                     *, is_anon: bool) -> dict[str, Any]:
    """The /0 layer (or table) metadata document — the recon-derived minimum both pinned clients
    load and edit against, no more (§2.6)."""
    geom_type = meta["geom_type"]
    is_table = geom_type == "none"
    fields, lookup = await _esri_fields(conn, layer_id, is_anon=is_anon,
                                        chute=_chute_ok(layer_id, meta))
    editable_fields = [f for f in fields if f["editable"]]
    # displayField (decision-layer-display-field.md): the configured display_field, else the SAME read-time
    # derivation web uses (app/display_field.py — so an unstarred layer names identically on both surfaces),
    # taken only when it names a field we actually emit (resolved identically on BOTH esri doors, which share
    # this doc). A stale / non-emitted name falls through to the historical heuristic (first string field, else
    # OBJECTID) so the wire contract always carries a real field.
    configured = meta.get("display_field") or await derive_display_field(conn, layer_id)
    display = next((f["name"] for f in fields if f["name"] == configured), None) if configured else None
    if display is None:
        display = next((f["name"] for f in fields if f["type"] == "esriFieldTypeString"
                        and lookup[f["name"]]["kind"] != "chute"), "OBJECTID")
    doc: dict[str, Any] = {
        "currentVersion": _CURRENT_VERSION,
        "id": 0,
        "name": meta["display_name"] or layer_id,
        "type": "Table" if is_table else "Feature Layer",
        "description": "",
        "copyrightText": "",
        "defaultVisibility": True,
        "displayField": display,
        "objectIdField": "OBJECTID",
        "globalIdField": "GlobalID",
        "typeIdField": "",
        "fields": fields,
        "types": [],
        # indexes (P3) — the DECLARATION Pro's Preserve GlobalIDs gates on. Without it Append aborts
        # client-side in ~1s with `ERROR 003340: The target dataset must have a GlobalID field with a
        # unique index…` and never sends a request, so the whole Phase A preserve-GlobalID write path
        # is unreachable from the GP tool. Same failure shape as the WARNING 000594 that `globalIdField`
        # fixed: a missing declaration, not a missing implementation.
        #
        # Both indexes are REAL, not decorative — every hosted table carries `<t>_pkey` UNIQUE on `id`
        # (the column behind GlobalID) and `<t>_objectid_key` UNIQUE on `objectid` (V064). Declaring
        # something we don't have would move the failure from Pro's client to ours.
        #
        # Built from the EMITTED field set, never from pg_indexes: `fields` is already anon-filtered
        # (_ANON_SCRUB_COLUMNS), so describing the catalog directly would name columns the anonymous
        # view deliberately omits. OBJECTID/GlobalID are unconditional today, so the guard is inert —
        # it is here so a future scrub of either name cannot silently leak it through this array.
        # `indexType: "Attribute"` is on EVERY index entry the reference hosted service emits
        # (recon 2026-07-20) and was absent from ours. Both of these are attribute indexes, so it is
        # truthful. Not the 003340 gate — that already passes without it — but it is a structural gap
        # in the exact object Pro was observed to decompose into typed properties (rig TEST 3).
        "indexes": [
            {"name": f"{_n}_Index", "fields": _n, "isAscending": True,
             "isUnique": True, "description": "", "indexType": "Attribute"}
            for _n in ("OBJECTID", "GlobalID") if _n in {f["name"] for f in fields}
        ],
        # The OBJECTID is server-maintained (V064 int32 identity) — a client must never try to supply
        # or preserve it. Reference declares this; we did not.
        "uniqueIdField": {"name": "OBJECTID", "isSystemMaintained": True},
        # The last unexplored candidate from the 2026-07-20 reference diff, and the only one left that
        # can be stated rather than built. Each key is answered for TALUS, not copied — the reference
        # also carries supportsAsyncApplyEdits / supportsApplyEditsbyUploadID /
        # supportedApplyEditsUploadIDFormats / supportsTranslation, and every one of those is FALSE or
        # absent here because we implement none of them: applyEdits is synchronous, there is no upload-id
        # ingest path, and there is no translation service. Declaring them would be the original mistake
        # with the sign flipped.
        #   supportsReturnEditResults — TRUE: applyEdits already returns per-feature
        #   addResults/updateResults/deleteResults (verify-featureserver.sh C1–C6b, G13/G14).
        #   supportedSqlFormatsInCalculate — ["standard"]: the only dialect our WHERE parser accepts
        #   (B6 rejects OR, B18 rejects non-identity ops on GlobalID); "none" would be a lie by omission
        #   since /query does take standard SQL, and advertising "native" would invite passthrough.
        "advancedEditingCapabilities": {
            "supportsReturnEditResults": True,
            "supportsAsyncApplyEdits": False,
            "supportsApplyEditsbyUploadID": False,
            "supportedSqlFormatsInCalculate": ["standard"],
        },
        # rollbackOnFailure is implemented and proven (verify-featureserver.sh C12/C13/C14: the default
        # rolls the whole batch back, =false lands the good edit alone). We simply never advertised it.
        "supportsRollbackOnFailureParameter": True,
        # One default editor template: Pro's Create-Features pane is template-driven (recon —
        # cheap to emit, QGIS ignores it).
        "templates": [{
            "name": meta["display_name"] or layer_id,
            "description": "",
            "drawingTool": _EDIT_TOOL[geom_type],
            "prototype": {"attributes": {f["name"]: None for f in editable_fields}},
        }],
        "capabilities": _capabilities(layer_id, meta),
        "maxRecordCount": _MAX_RECORD_COUNT,
        "supportedQueryFormats": "JSON",
        "supportsAdvancedQueries": True,
        # supportsPagination is THE flag Pro keys resultOffset paging on (recon §1.1).
        "advancedQueryCapabilities": {
            "supportsPagination": True,
            "supportsOrderBy": True,
            "supportsDistinct": False,
            "supportsStatistics": False,
            "supportsReturningQueryExtent": True,
            "supportsQueryWithDistance": False,
            "supportsSqlExpression": False,
            "supportsTrueCurve": False,
            "supportsHavingClause": False,
            "supportsCountDistinct": False,
        },
        # V109: the owner's Move-or-reshape flag — Pro and Field Maps refuse geometry edits client-side
        # from this property alone (decision-layer-edit-capabilities.md §3.4).
        "allowGeometryUpdates": (not is_table) and meta["edit_geometry"],
        # Honest only now that GlobalID-keyed adds AND updates AND deletes all work: Esri's
        # contract is all-or-nothing, and Pro reads this flag before it will start an Append.
        "supportsApplyEditsWithGlobalIds": True,
        # hasAttachments keys Pro's native Attachments tab (A-2). Gated on attachments_enabled
        # ALONE (decision §11.4) — it governs READS, so a non-editable layer still advertises its
        # downloadable attachments; writes stay per-caller (_edit_deny_reason + the hard switch).
        "hasAttachments": bool(meta["attachments_enabled"]),
        "supportsAttachmentsByUploadId": False,
        "supportsCoordinatesQuantization": False,
        "syncEnabled": False,
        "minScale": 0,
        "maxScale": 0,
    }
    if not is_table:
        doc["geometryType"] = _ESRI_GEOM[geom_type]
        doc["extent"] = await _extent_doc(conn, layer_id)
        doc["hasZ"] = False
        doc["hasM"] = False
        doc["drawingInfo"] = _renderer(geom_type)
    return doc


async def _service_doc(conn: AsyncConnection, layer_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    geom_type = meta["geom_type"]
    is_table = geom_type == "none"
    entry = {"id": 0, "name": meta["display_name"] or layer_id}
    if not is_table:
        entry["geometryType"] = _ESRI_GEOM[geom_type]
        entry["type"] = "Feature Layer"
    else:
        entry["type"] = "Table"
    doc: dict[str, Any] = {
        "currentVersion": _CURRENT_VERSION,
        "serviceDescription": meta["display_name"] or layer_id,
        "hasVersionedData": False,
        "hasStaticData": False,
        "supportsDisconnectedEditing": False,
        "supportedQueryFormats": "JSON",
        "maxRecordCount": _MAX_RECORD_COUNT,
        "capabilities": _capabilities(layer_id, meta),
        "description": "",
        "copyrightText": "",
        "spatialReference": _WGS84_SR,
        "units": "esriDecimalDegrees",
        "hasAttachments": bool(meta["attachments_enabled"]),  # §11.4 — same gate as _layer_doc
        "allowGeometryUpdates": (not is_table) and meta["edit_geometry"],  # V109, as on the layer
        "syncEnabled": False,
        # Declared at the SERVICE root as well as on the layer. Recon 2026-07-20 against a real hosted
        # FeatureServer (services3.arcgis.com, GlobalID-enabled, editable) found it in BOTH places; we
        # had it on the layer only. Pro's preserve-GlobalID Append aborts CLIENT-side with
        # `WARNING 000594: Function not implemented` and never sends a request, and a service-level
        # capability probe is the most direct explanation for a client deciding the function is absent.
        # Truthful either way: the layer flag has been true since Phase A, so this states the same fact
        # at the level a client may actually read it.
        "supportsApplyEditsWithGlobalIds": True,
        # applyEdits returns a per-feature result for deletes (deleteResults), same as adds/updates.
        "supportsReturnDeleteResults": True,
        "layers": [] if is_table else [entry],
        "tables": [entry] if is_table else [],
    }
    # NOT declared, deliberately, though the reference service carries them: `supportsAppend`
    # (+ supportedAppendCapabilities) advertises the server-side /append REST operation, which Talus
    # does NOT implement — declaring it would make Pro POST to /append and take a 404, trading a clean
    # client-side abort for a worse failure. Pro's Append GP tool already works without it, one feature
    # per applyEdits (verified in the access log, rig 2026-07-20). Likewise `syncEnabled`,
    # `Sync`/`ChangeTracking`/`Extract` in capabilities, and changeTrackingInfo: we implement none of
    # them, and the whole point of this slice is that a declaration must be TRUE.
    if not is_table:
        extent = await _extent_doc(conn, layer_id)
        doc["initialExtent"] = extent
        doc["fullExtent"] = extent
    return doc


async def _viewable_layers(conn: AsyncConnection, principal: User | None) -> list[tuple[str, str | None, str]]:
    """(layer_id, display_name, geom_type) — the caller's viewable hosted layers INCLUDING tables
    (fork 5), EXCLUDING view layers (vw_* — no objectid identity on a PG view, v1). Same fail-soft
    per-layer scoping as the OGC catalog."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT layer_id, display_name, geom_type FROM platform.layers "
            "WHERE status = 'ready' AND layer_id NOT LIKE 'vw\\_%' "
            "ORDER BY display_name NULLS LAST, layer_id"
        )
        rows = await cur.fetchall()
    out: list[tuple[str, str | None, str]] = []
    for layer_id, display_name, geom_type in rows:
        try:
            if principal is not None:
                if await is_shared(principal, layer_id) or principal.is_admin:
                    out.append((layer_id, display_name, geom_type))
            elif await public_object_allowed(conn, layer_id, object_type="layer"):
                out.append((layer_id, display_name, geom_type))
        except HTTPException:
            continue
    return out


# --- the query engine (shared by GET and POST) ----------------------------------------------------


async def _run_query(conn: AsyncConnection, layer_id: str, meta: dict[str, Any],
                     params: dict[str, str], is_anon: bool, *,
                     request: Request, mount: str) -> dict[str, Any]:
    geom_type = meta["geom_type"]
    is_table = geom_type == "none"
    fields, lookup = await _esri_fields(conn, layer_id, is_anon=is_anon,
                                        chute=_chute_ok(layer_id, meta))

    filters = _compile_where(params.get("where"), lookup)
    object_ids = params.get("objectIds", "").strip()
    if object_ids:
        cleaned = ",".join(p.strip() for p in object_ids.split(",") if p.strip())
        if not re.fullmatch(r"[0-9,]+", cleaned):
            raise _EsriBadRequest("objectIds must be a comma-separated integer list")
        filters.append(f"objectid:in:{cleaned}")
    # `globalIds=` — the GlobalID twin of objectIds, riding the same id-set path the export
    # already uses (_where's feature_ids → `t.id::text = ANY(...)`). Both may be sent; they AND.
    global_ids: list[str] | None = None
    raw_gids = params.get("globalIds", "").strip()
    if raw_gids:
        parsed = [_parse_global_id(p) for p in raw_gids.split(",") if p.strip()]
        if not parsed or any(p is None for p in parsed):
            raise _EsriBadRequest(
                "globalIds must be a comma-separated GUID list (braced or bare)")
        global_ids = [p for p in parsed if p]
    bbox, geom_int = (None, None) if is_table else _parse_spatial_filter(params)
    out_wkid = None if is_table else _norm_wkid(params.get("outSR"))

    def truthy(key: str) -> bool:
        return params.get(key, "false").strip().lower() == "true"

    cols = await _columns(conn, layer_id)
    props_fields = await _props_fields(conn, layer_id)
    tbl = sql.Identifier("assets", layer_id)
    # the layer's STORAGE srid: a State Plane (native) layer's envelope / filter geometry must be transformed
    # into it, else a spatial query matches nothing (layer_query._where, 2026-09-26)
    storage_srid = (await _geom_srid(conn, layer_id) or 4326) if (bbox is not None or geom_int is not None) else 4326

    # -- returnCountOnly / returnIdsOnly / returnExtentOnly: dedicated cheap queries -------------
    if truthy("returnCountOnly"):
        where_sql, wparams = _where(filters, bbox, global_ids, set(cols), props_fields, geom_int, storage_srid=storage_srid)
        async with conn.cursor() as cur:
            await cur.execute(sql.SQL("SELECT count(*) FROM {t} AS t WHERE {w}").format(
                t=tbl, w=where_sql), wparams)
            count = (await cur.fetchone())[0]
        return {"count": count}

    if truthy("returnIdsOnly"):
        # UNCAPPED by design (fork 10): QGIS harvests every id in ONE call and
        # cannot recover from truncation (silent feature loss). Warn-log the giants.
        where_sql, wparams = _where(filters, bbox, global_ids, set(cols), props_fields, geom_int, storage_srid=storage_srid)
        async with conn.cursor() as cur:
            await cur.execute(sql.SQL(
                "SELECT objectid FROM {t} AS t WHERE {w} ORDER BY objectid").format(
                t=tbl, w=where_sql), wparams)
            ids = [r[0] for r in await cur.fetchall()]
        if len(ids) > _IDS_WARN_THRESHOLD:
            log.warning("esri_id_harvest_large", layer=layer_id, count=len(ids))
        return {"objectIdFieldName": "OBJECTID", "objectIds": ids}

    if truthy("returnExtentOnly"):
        if is_table:
            raise _EsriBadRequest("a table has no extent")
        where_sql, wparams = _where(filters, bbox, global_ids, set(cols), props_fields, geom_int, storage_srid=storage_srid)
        async with conn.cursor() as cur:
            await cur.execute(sql.SQL(
                "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM "
                "(SELECT ST_Extent(ST_Transform(t.geom, 4326)) AS e FROM {t} AS t WHERE {w}) s "
                "WHERE e IS NOT NULL").format(t=tbl, w=where_sql), wparams)
            row = await cur.fetchone()
        if row is None:
            extent: dict[str, Any] | None = None
        else:
            b = list(row)
            if out_wkid == 3857:
                b[0], b[1] = _to_merc(b[0], b[1])
                b[2], b[3] = _to_merc(b[2], b[3])
            extent = {"xmin": b[0], "ymin": b[1], "xmax": b[2], "ymax": b[3],
                      "spatialReference": _sr_doc(out_wkid)}
        return {"extent": extent}

    # -- the feature page --------------------------------------------------------------------
    try:
        offset = int(params.get("resultOffset") or 0)
        page = int(params.get("resultRecordCount") or _MAX_RECORD_COUNT)
    except ValueError:
        raise _EsriBadRequest("resultOffset/resultRecordCount must be integers")
    page = max(1, min(page, _MAX_RECORD_COUNT))  # clamp, never reject (the OGC lesson)
    offset = max(0, offset)

    # Default order = OBJECTID (what a real FeatureServer returns and what makes resultOffset
    # paging stable & intuitive); orderByFields overrides.
    sort: str | None = "objectid:asc"
    order_by = (params.get("orderByFields") or "").strip()
    if order_by:
        first = order_by.split(",")[0].strip()  # v1: single-field ordering (house sort seam)
        parts = first.split()
        by_lower = {k.lower(): k for k in lookup}
        declared = by_lower.get(parts[0].lower())
        if declared is None or lookup[declared]["kind"] in ("fid", "chute"):
            raise _EsriBadRequest(f"unknown orderByFields field: {parts[0]}")
        direction = parts[1].lower() if len(parts) > 1 and parts[1].lower() in ("asc", "desc") else "asc"
        sort = f"{lookup[declared]['source']}:{direction}"
        if "," in order_by:
            log.debug("esri_orderby_extra_ignored", layer=layer_id, raw=order_by)

    return_geometry = (not is_table) and params.get("returnGeometry", "true").strip().lower() != "false"

    total, features = await feature_page(
        conn, layer_id, is_anon,
        filters=filters, bbox=bbox, feature_ids=global_ids, sort=sort, limit=page, offset=offset,
        geom_intersect=geom_int,
    )

    out_names = [f["name"] for f in fields]
    raw_outfields = (params.get("outFields") or "*").strip()
    if raw_outfields not in ("*", ""):
        asked = {p.strip().lower() for p in raw_outfields.split(",") if p.strip()}
        # OID rides along even when not asked for (clients omit it yet index results by it).
        out_names = [n for n in out_names if n.lower() in asked or n == "OBJECTID"]

    # The chute echo (decision §2.3.2): the newest attachment per feature, resolved for the whole
    # page in ONE media.attachments query, serialized as its feature-addressed chute URL. Server-
    # derived on every read — a fresh QGIS session previews the newest file with no stored value.
    chute_name = next((n for n in out_names if lookup[n]["kind"] == "chute"), None)
    chute_files: dict[str, str] = {}
    chute_base = ""
    if chute_name and features:
        chute_files = await att.latest_for_features(
            conn, layer_id, [str(f.get("id")) for f in features])
        if chute_files:
            chute_base = f"{await _advertised_base(conn, request)}/api/v1{mount}"

    out_features = []
    for feat in features:
        chute_url = None
        if chute_name:
            fname = chute_files.get(str(feat.get("id")))
            if fname is not None:
                oid = (feat.get("properties") or {}).get("objectid")
                chute_url = (f"{chute_base}/layers/{layer_id}/attachments/{oid}/"
                             f"{quote(fname, safe='')}")
        item: dict[str, Any] = {"attributes": _attributes_for(feat, out_names, lookup,
                                                              chute_url=chute_url)}
        if return_geometry:
            item["geometry"] = _geojson_to_esri(feat.get("geometry"), out_wkid)
        out_features.append(item)

    doc: dict[str, Any] = {
        "objectIdFieldName": "OBJECTID",
        # Names the GlobalID field IN THIS RESULT SET — Esri's contract is per-response, so the
        # conditional is correct rather than lazy: `""` is the right answer when GlobalID wasn't
        # requested. `out_names` carries DECLARED casing (built from fields at :1157, only filtered
        # by the caller's spelling at :1162), so the literal compare is safe however outFields was
        # spelled.
        #
        # ★ Completes PR #57 (decision-geoservices-globalid.md §10). That PR exists precisely
        # because Pro seeing no GlobalID aborts client-side with WARNING 000594 and blocks the main
        # ESRI→Talus migration path (decision-interop-i3.md:272) — but it scoped its fix to the
        # LAYER DOCUMENT, and this line was outside that scope and unchanged since the original I-3
        # build. So the layer doc declared `globalIdField: "GlobalID"`, we advertised
        # `supportsApplyEditsWithGlobalIds: True` twice, we served populated GUIDs — and every query
        # response still told the client the result set had no GlobalID column. On 2026-07-26 Pro
        # resolved that contradiction against us, out loud and unprompted, in its own error dialog:
        # "The table does not have a Global ID Field."
        "globalIdFieldName": "GlobalID" if "GlobalID" in set(out_names) else "",
        "fields": [f for f in fields if f["name"] in set(out_names)],
        "features": out_features,
        # true exactly when this page was truncated — the "fetch the next page" signal
        # pagination-aware clients (Pro) rely on.
        "exceededTransferLimit": offset + len(features) < total,
    }
    if not is_table:
        doc["geometryType"] = _ESRI_GEOM[geom_type]
        doc["spatialReference"] = _sr_doc(out_wkid)
    return doc


# --- editing (I-3 commit 3: applyEdits core + the QGIS wrapper trio) ------------------------------

_GEO_AUDIT_CLIENT = "geoservices-api"  # audit shadows distinguish desktop edits from portal-api

# The PAT write path's role key: a PAT carries no Keycloak claims, so the owner's CURRENT realm
# roles are fetched from the IdP and cached briefly (QGIS fires one POST per edit type per save —
# don't hammer Keycloak inside one save). 60s staleness on a role DOWNGRADE is the accepted
# window; revocation/expiry/disable all still bite instantly in the auth seam.
# Talus Field f4: the PAT-owner claims helper (live realm roles, 60 s cache) was lifted into
# app.auth so the native write doors share it; this name stays for the façade's callers.
from ..auth import pat_owner_claims as _pat_owner_claims  # noqa: E402


async def _is_field_project_token(conn: AsyncConnection, token_id: Any) -> bool:
    """Is this access token a generated field project's embedded credential? A listed token that is
    not one is a Talus Field device credential (once-over 2026-11-15 K6). Fail closed: no token id
    reads as "not a project token", which this door refuses; a database error raises."""
    if token_id is None:
        return False
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM platform.field_projects WHERE embedded_pat_id = %s LIMIT 1", (token_id,)
        )
        return (await cur.fetchone()) is not None


# Why an AUTHENTICATED caller may not edit — a user-facing sentence per edit_allowed reason.
_FRIENDLY_DENY = {
    "layer_not_editable": "This layer is read-only.",
    "editing_disabled": "Editing is turned off on this layer — its owner controls this in the "
                        "portal (Manage → Settings).",
    "missing_edit_role": "Your account doesn’t have permission to edit (no editor role).",
    "no_edit_access": "You have view-only access to this layer. Ask the owner to share it with "
                      "edit access.",
}


async def _edit_deny_reason(
    request: Request, conn: AsyncConnection, user: User,
    bearer_claims: dict[str, Any] | None, layer_id: str, meta: dict[str, Any],
) -> str | None:
    """Why this authenticated caller may NOT edit this layer — a user-facing sentence, or None if
    allowed. Returned as a per-EDIT error (not an HTTP 4xx) so the desktop client SHOWS it: QGIS
    and Pro ignore the body of an edit-POST HTTP error and surface only 'Forbidden'/'Bad Request',
    but they DO display addResults[].error.description. Authentication (no credential) is the ONE
    thing that stays HTTP-level (401 + challenge, in _handle_edit) — it's what makes the client
    prompt for a token. Everything past 'we know who you are' that's still a 'no' explains itself
    here. Runs the SAME three-key edit decision every write runs (editable × editing_enabled ×
    role × OpenFGA access); for a PAT it also requires read_write scope and resolves the owner's
    live Keycloak roles (the role key)."""
    if not _SLOT_RE.fullmatch(layer_id):
        return ("This layer can’t be edited from a desktop app — only hosted layers you created "
                "in Talus are editable this way. (View layers are view-only here — edit their "
                "source layer instead.)")
    if not meta["editable"]:
        return "This layer is read-only."
    if not meta["editing_enabled"]:
        return _FRIENDLY_DENY["editing_disabled"]
    if bearer_claims is None and settings.auth_mode != "stub":  # PAT path (the only claims-less auth)
        if getattr(request.state, "pat_scope", None) != "read_write":
            return ("This access token is read-only. Create a “Read & edit” token "
                    "(in the portal: Profile → Access tokens, or the “Use in… → Edit” wizard) and "
                    "reconnect with it.")
        # A Talus Field DEVICE credential (a token carrying a pack's layer list, V108) never edits
        # through this door — inside its pack or outside it. Its promise is "create and edit on the pack's layers, never delete";
        # the native doors keep that promise (auth.rw_pat_gate, and no delete), and this door has
        # deletes and knew nothing of the list, so a lost phone could change or delete on every layer
        # its owner may edit. The Field app does not call this door. Checked BEFORE the owner's
        # roles are fetched: the answer does not depend on them.
        listed = getattr(request.state, "pat_layer_ids", None)
        if listed is not None:
            # A listed token is either a Talus Field device credential (refused here, above) or a
            # generated field project's embedded token (once-over 2026-11-15 K6), which QField uses
            # through this door and which may write only to the project's own layers.
            if not await _is_field_project_token(conn, getattr(request.state, "pat_token_id", None)):
                return ("This is a Talus Field device credential. It cannot edit through this endpoint. "
                        "Create a “Read & edit” access token in the portal (Profile → Access tokens) "
                        "and reconnect with it.")
            if layer_id not in listed:
                return ("This field project's credential can edit only the layers in that project. "
                        "Add the layer to the project's map and regenerate it, or use your own "
                        "“Read & edit” access token.")
        eff_claims = await _pat_owner_claims(str(user.id))
    else:
        eff_claims = bearer_claims
    allowed, reason = await edit_allowed(conn, eff_claims, user, layer_id)
    if allowed:
        return None
    return _FRIENDLY_DENY.get(reason, "You’re not authorized to edit this layer.")


class _EditError(Exception):
    """A PER-EDIT failure. Never becomes an HTTP error: it becomes {success:false, error:{…}} in
    the result array — a top-level error body on an edit POST silently corrupts QGIS's id
    bookkeeping (the recon trap), so edit-payload problems ALWAYS report per-result. `code` is the
    Esri error code the result carries: 1000 for a payload problem, 403 for a policy refusal
    (V109 — the same code the request-level deny uses)."""
    def __init__(self, message: str, code: int = 1000):
        self.message = message
        self.code = code


_GUID_RE = re.compile(
    r"^\{?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}?$")


def _parse_global_id(raw: Any) -> str | None:
    """An Esri GlobalID wire value → the bare lowercase uuid, or None if it isn't one. Accepts
    braced or bare, any case: Pro sends the braced-uppercase registry form `{XXXXXXXX-…}`, but
    curl/QGIS-shaped callers send bare, and both name the same uuid. Strict by construction — a
    non-GUID never reaches SQL (it becomes an honest per-result error at the call site)."""
    if not isinstance(raw, str):
        return None
    m = _GUID_RE.match(raw.strip())
    return m.group(1).lower() if m else None


def _braced_guid(value: Any) -> str | None:
    """A uuid → Esri's braced-uppercase registry form, the shape `_attributes_for` already emits.
    Idempotent: an already-braced value is re-cased, not double-braced (the rollback `_mark` pass
    re-wraps results that already carry one)."""
    if not value:
        return None
    s = str(value).strip()
    return s.upper() if s.startswith("{") and s.endswith("}") else "{" + s.upper() + "}"


def _ok_result(oid: int, global_id: Any = None) -> dict[str, Any]:
    return {"objectId": oid, "globalId": _braced_guid(global_id), "success": True}


def _err_result(message: str, code: int = 1000, oid: int | None = None,
                global_id: Any = None) -> dict[str, Any]:
    # `oid`/`global_id` echo the ATTEMPTED identity of a failed edit so ArcGIS Pro can correlate the
    # result to the edit it sent. Pro maps results -> edits by BOTH; a null trips "The index passed
    # was not within the valid range" and hides the real error, which is why the editing-off /
    # read-only 403 descriptions never rendered in Pro (rig 2026-07-18, leg 3). The objectId echo
    # alone (442eddf) was an incomplete fix: Pro correlates by GlobalID once the layer has one.
    return {"objectId": oid, "globalId": _braced_guid(global_id), "success": False,
            "error": {"code": code, "description": message}}


def _attempted_edit_keys(feat: Any) -> tuple[int | None, str | None]:
    """The (objectId, globalId) an edit *claims* to target — mirrors ``_do_update``'s extraction,
    for echoing in a failed result. Best-effort: either may be None."""
    if not isinstance(feat, dict):
        return None, None
    attrs = feat.get("attributes") or {}
    raw_oid = next((v for k, v in attrs.items() if str(k).lower() == "objectid"), None)
    try:
        oid = int(raw_oid)
    except (TypeError, ValueError):
        oid = None
    raw_gid = next((v for k, v in attrs.items() if str(k).lower() == "globalid"), None)
    if raw_gid is None:
        raw_gid = feat.get("globalId")
    return oid, _parse_global_id(raw_gid)


def _parse_attachment_edits(params: dict[str, str]) -> dict[str, list] | None:
    """The applyEdits `attachments` sub-object (decision-geoservices-applyedits-attachments.md) —
    {"adds": […], "updates": […], "deletes": […]} with every key optional, or None when the request
    carries none (the response then carries no `attachments` key either — F1). Malformed JSON is a
    top-level Esri 400, protocol-param class, exactly like unparseable adds=."""
    raw = params.get("attachments")
    if raw is None or not str(raw).strip():
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        raise _EsriBadRequest("unparseable attachments JSON")
    if not isinstance(doc, dict):
        raise _EsriBadRequest("attachments must be a JSON object with adds/updates/deletes arrays")
    out: dict[str, list] = {}
    for key in ("adds", "updates", "deletes"):
        val = doc.get(key) or []
        if not isinstance(val, list):
            raise _EsriBadRequest(f"attachments.{key} must be a JSON array")
        out[key] = val
    return out


def _att_parent_keys(item: Any) -> tuple[int | None, str | None]:
    """The (parentObjectId, parentGlobalId-uuid) an attachment add claims to target — resolve input
    AND the failure echo (decision F5/F6: an uncorrelatable failed result hides its reason behind
    Pro's index exception, the leg-3 lesson). Best-effort: either may be None."""
    if not isinstance(item, dict):
        return None, None
    try:
        p_oid = int(item.get("parentObjectId"))
    except (TypeError, ValueError):
        p_oid = None
    return p_oid, _parse_global_id(item.get("parentGlobalId"))


def _att_add_result(esri_id: int | None, p_oid: int | None, p_gid: str | None,
                    error: tuple[int, str] | None = None) -> dict[str, Any]:
    """One attachments.addResults entry (decision F1): the documented {objectId, globalId, success}
    — objectId = the V069 esri_id (the same handle attachmentInfos serves), globalId always null
    (attachments carry no wire GlobalID in Talus; echoing an id nothing serves would be a claim,
    not evidence) — plus the parent echo (F5), an extra key Esri clients ignore."""
    r: dict[str, Any] = {"objectId": esri_id, "globalId": None,
                         "parentObjectId": p_oid, "success": error is None}
    if p_gid is not None:
        r["parentGlobalId"] = _braced_guid(p_gid)
    if error is not None:
        r["error"] = {"code": error[0], "description": error[1]}
    return r


def _att_refusal(kind: str, entry: Any) -> dict[str, Any]:
    """The F4 refusal for an attachments.updates/deletes entry — per-result, honest, pointed at
    the doors that work. Talus attachments have no wire GlobalID (attachmentInfos serves
    globalId:null), so no client can legitimately key one here; the entry's claimed globalId is
    echoed back for correlation only."""
    if kind == "update":
        gid, door = (entry.get("globalId") if isinstance(entry, dict) else None), "updateAttachment"
    else:
        gid, door = entry, "deleteAttachments"
    return {"objectId": None, "globalId": _braced_guid(_parse_global_id(gid)), "success": False,
            "error": {"code": 400, "description":
                      f"attachment {kind}s via applyEdits are not supported — use the "
                      f"per-feature {door} endpoint"}}


def _int_or_none(raw: str) -> int | None:
    """Strict int parse for wire ids. NOT lstrip('-').isdigit() — that admits '--5', which int()
    then raises on (an unhandled 500 where the contract promises an honest 400)."""
    try:
        return int(raw)
    except ValueError:
        return None


def _att_result_err(code: int, message: str) -> dict[str, Any]:
    """A failed attachment-POST result (decision §11.7): success:false with the honest code
    (403/404/409/413/415/502) + a user-facing description, at HTTP 200 — the shape Pro actually
    displays (it ignores an edit-POST's HTTP error body, the I-3 recon trap)."""
    return {"success": False, "error": {"code": code, "description": message}}


def _att_info_doc(row: dict[str, Any]) -> dict[str, Any]:
    """One attachmentInfos entry — the recon-derived minimum (§11 wire shapes). `id` is the V069
    esri_id (the stable EXTERNAL integer handle), never the internal uuid."""
    return {"id": row["esri_id"], "globalId": None, "name": row["filename"],
            "contentType": row["content_type"], "size": row["byte_size"]}


def _esri_to_geojson(geom: Any, geom_type: str) -> dict[str, Any]:
    """Inbound esriJSON → GeoJSON for the core geometry machinery. Coordinates arrive in the
    layer's ADVERTISED SR (4326 — QGIS sends no per-geometry spatialReference and uses the layer
    SR; recon §1.1). Esri winding (exterior CW, holes CCW — what QGIS emits) groups rings into
    polygons; unclosed rings are lenient-closed (real ArcGIS accepts them). True curves are
    refused honestly — we never advertise curve support."""
    if not isinstance(geom, dict):
        raise _EditError("edit requires a geometry object")
    if any(k in geom for k in ("curvePaths", "curveRings")):
        raise _EditError("true-curve geometries are not supported")
    if geom_type == "line":
        paths = geom.get("paths")
        if not isinstance(paths, list) or not paths:
            raise _EditError("a line edit requires esriJSON paths")
        return {"type": "MultiLineString", "coordinates": [[p[:2] for p in path] for path in paths]}
    if geom_type == "polygon":
        rings = geom.get("rings")
        if not isinstance(rings, list) or not rings:
            raise _EditError("a polygon edit requires esriJSON rings")
        polys: list[list[list[list[float]]]] = []
        for ring in rings:
            pts = [p[:2] for p in ring]
            if pts and pts[0] != pts[-1]:
                pts = pts + [pts[0]]
            if _ring_cw(pts) or not polys:
                polys.append([pts])  # CW = a new exterior (esri convention; first ring always starts one)
            else:
                polys[-1].append(pts)  # CCW = a hole of the current polygon
        return {"type": "MultiPolygon", "coordinates": polys}
    raise _EditError(f"unsupported geometry for a {geom_type} layer")


def _geom_sql(geom_type: str, native: bool, srid: int, esri_geom: Any) -> tuple[str, dict[str, Any]]:
    """The geom column expression for an add/update: esriJSON (4326) → the CORE's own expression
    builder (family guard + degenerate-geometry validation ride along), then ST_Transform for
    native-storage layers — the façade's coords are 4326 by contract, storage may not be."""
    if geom_type == "point":
        if not isinstance(esri_geom, dict) or not isinstance(esri_geom.get("x"), (int, float)) \
                or not isinstance(esri_geom.get("y"), (int, float)):
            raise _EditError("a point edit requires numeric esriJSON x/y")
        expr, params = _geom_expr_and_params(
            "point", False, 4326, lon=esri_geom["x"], lat=esri_geom["y"], x=None, y=None, geometry=None
        )
    else:
        gj = _esri_to_geojson(esri_geom, geom_type)
        try:
            expr, params = _geom_expr_and_params(
                geom_type, False, 4326, lon=None, lat=None, x=None, y=None, geometry=gj
            )
        except HTTPException as e:  # the core's 422s (family/degenerate) become per-result errors
            raise _EditError(str(e.detail))
    if native:
        expr = f"ST_Transform({expr}, %(_tgt_srid)s)"
        params["_tgt_srid"] = srid
    return expr, params


def _coerce_props_value(value: Any, dtype: str) -> Any:
    """Esri wire values → props storage values: Date epoch-ms → 'YYYY-MM-DD' (V047 strict),
    boolean strings → real booleans (fork 8b round-trip), numeric strings → numbers. Anything
    still wrong is the validation trigger's honest job."""
    if value is None:
        return None
    if dtype == "date":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).date().isoformat()
        return value
    if dtype == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        return value
    if dtype == "number" and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return value
    return value


def _split_attrs(attrs: dict[str, Any], lookup: dict[str, dict[str, Any]]
                 ) -> tuple[bool, Any, dict[str, Any]]:
    """Declared-field attribute mapping for an add/update: (notes_present, notes, props_patch).
    Read-only fields (OBJECTID/GlobalID/audit timestamps) and undeclared keys are IGNORED, not
    errors — clients echo the full declared set back (QGIS pre-reads then sends everything).
    The chute field is swallowed HERE (decision §2.3.3): the upload already happened via the PUT
    door, so the applyEdits carrying the widget's field change must dissolve — a chute-only update
    yields empty sets and takes _do_update's exists-check no-op (no UPDATE, no row_version bump,
    no audit row; sweep-guarded)."""
    by_lower = {k.lower(): k for k in lookup}
    notes_present, notes = False, None
    props: dict[str, Any] = {}
    for raw_key, value in (attrs or {}).items():
        declared = by_lower.get(str(raw_key).lower())
        if declared is None:
            continue
        spec = lookup[declared]
        if spec["kind"] == "chute":
            continue  # accept-and-discard: the value is computed on read, never stored
        if spec["kind"] == "props":
            props[spec["source"]] = _coerce_props_value(value, spec.get("dtype", "text"))
        elif spec["kind"] == "real" and spec["source"] == "notes":
            notes_present, notes = True, value
    return notes_present, notes, props


def _parse_edit_arrays(params: dict[str, str], mode: str
                       ) -> tuple[list, list, list[int], bool, bool]:
    """(adds, updates, delete_oids, rollbackOnFailure, useGlobalIds) from the request params, per
    endpoint mode ('applyEdits' reads adds/updates/deletes; the wrapper trio reads
    features=/objectIds=)."""
    use_global_ids = params.get("useGlobalIds", "false").strip().lower() == "true"
    if mode == "delete" and params.get("where"):
        raise _EsriBadRequest("where-based deletes are not supported — send objectIds")
    rollback = params.get("rollbackOnFailure", "true").strip().lower() != "false"

    def arr(raw: str | None, what: str) -> list:
        if not raw or not raw.strip():
            return []
        try:
            doc = json.loads(raw)
        except ValueError:
            raise _EsriBadRequest(f"unparseable {what} JSON")
        if not isinstance(doc, list):
            raise _EsriBadRequest(f"{what} must be a JSON array")
        return doc

    def oids(raw: str | None) -> list:
        """The delete key list: GlobalIDs under useGlobalIds, else OBJECTIDs. A mismatch is an
        honest 400 either way — never a silent reinterpretation of the caller's identifiers."""
        if not raw or not raw.strip():
            return []
        txt = raw.strip()
        parts = [p for p in (json.loads(txt) if txt.startswith("[") else txt.split(","))
                 if str(p).strip()]
        if use_global_ids:
            keys = []
            for p in parts:
                gid = _parse_global_id(str(p).strip())
                if gid is None:
                    raise _EsriBadRequest(
                        "useGlobalIds=true — deletes/objectIds must be GlobalIDs "
                        f"(braced or bare GUIDs); got {str(p)[:40]!r}")
                keys.append(gid)
            return keys
        try:
            return [int(str(p).strip()) for p in parts]
        except ValueError:
            raise _EsriBadRequest("deletes/objectIds must be integers")

    if mode == "applyEdits":
        adds, updates = arr(params.get("adds"), "adds"), arr(params.get("updates"), "updates")
        deletes = oids(params.get("deletes"))
    elif mode == "add":
        adds, updates, deletes = arr(params.get("features"), "features"), [], []
    elif mode == "update":
        adds, updates, deletes = [], arr(params.get("features"), "features"), []
    else:
        adds, updates, deletes = [], [], oids(params.get("objectIds"))

    return adds, updates, deletes, rollback, use_global_ids


async def _apply_edits_core(
    conn: AsyncConnection, request: Request, layer_id: str, meta: dict[str, Any],
    params: dict[str, str], user: User, claims: dict[str, Any] | None, mode: str,
) -> dict[str, Any]:
    """The one edit engine all four endpoints share. Parses the edits, runs the three-key edit
    decision (as a per-result DENY, not an HTTP error — so QGIS/Pro show the reason), then
    executes every allowed edit inside THIS request's transaction with savepoints:
    rollbackOnFailure=true (the default) makes the whole request atomic — any failure rolls
    everything back and every result reports it (index-matched, per the spec clients rely on);
    =false rolls back only the failing edit. Concurrency is last-write-wins (fork 3):
    no row_version predicate, but every write still bumps row_version via the
    trigger, so web-app If-Match stays fully sound."""
    adds, updates, delete_oids, rollback, use_global_ids = _parse_edit_arrays(params, mode)
    # The attachments sub-object rides ONLY the applyEdits dialect (Pro's Add-Attachments GP tool);
    # nothing sends it on the QGIS trio. Presence alone activates it — Pro sends parentObjectId
    # with NO useGlobalIds flag (rig 2026-07-22), the docs' "requires useGlobalIds" notwithstanding
    # (decision F6: live behavior outranks the doc page).
    atts = _parse_attachment_edits(params) if mode == "applyEdits" else None
    if not adds and not updates and not delete_oids and not (atts and any(atts.values())):
        raise _EsriBadRequest("no edits supplied")

    # Authorization as a PER-RESULT failure, not a bare HTTP 403: QGIS/Pro ignore an edit-POST's
    # error body and show only "Forbidden", so a denied editor gets no reason. Reporting every
    # submitted edit as success:false + a clear description is what the desktop actually
    # surfaces (and what real ArcGIS does for permission-denied edits). 200, nothing written.
    deny = await _edit_deny_reason(request, conn, user, claims, layer_id, meta)
    if deny is not None:
        # THE leg-3 case: this is the response Pro rendered as "index passed was not within the
        # valid range". Every result echoes whatever identity its edit carried so Pro can map the
        # result back and actually show the reason.
        upd_keys = [_attempted_edit_keys(u) for u in updates]
        denied = {
            "addResults": [_err_result(deny, code=403, global_id=_attempted_edit_keys(a)[1])
                           for a in adds],
            "updateResults": [_err_result(deny, code=403, oid=k[0], global_id=k[1])
                              for k in upd_keys],
            "deleteResults": [_err_result(deny, code=403,
                                          oid=None if use_global_ids else d,
                                          global_id=d if use_global_ids else None)
                              for d in delete_oids],
        }
        if atts is not None:  # the sub-object's entries report the SAME honest deny (F1 shape)
            denied["attachments"] = {
                "addResults": [_att_add_result(None, *_att_parent_keys(a), error=(403, deny))
                               for a in atts["adds"]],
                "updateResults": [_att_refusal("update", u) for u in atts["updates"]],
                "deleteResults": [_att_refusal("delete", d) for d in atts["deletes"]],
            }
        return denied
    await _validate_slot(conn, layer_id)  # status='ready' belt (id shape already proven)

    native, srid, geom_type = await _layer_storage(conn, layer_id)
    is_table = geom_type == "none"
    _fields, lookup = await _esri_fields(conn, layer_id, is_anon=False,
                                         chute=_chute_ok(layer_id, meta))
    field_defs = await active_field_defs(conn, layer_id)
    tbl = sql.Identifier("assets", layer_id)
    actor = str(user.id)

    add_results: list[dict[str, Any]] = []
    upd_results: list[dict[str, Any]] = []
    del_results: list[dict[str, Any]] = []
    # Object keys of attachments cascaded away by deletes in THIS batch. Accumulated in-transaction,
    # acted on only after the outer commit — and dropped entirely if the batch rolls back (below), so
    # rollbackOnFailure=true can never destroy bytes for an edit that did not land.
    del_object_keys: list[str] = []
    att_add_results: list[dict[str, Any]] = []
    # The mirror image, for the sub-object's ADDS (decision F3): att.store writes each blob before
    # the outer commit, so a batch rollback strands them — these keys are removed best-effort after
    # the ROLLBACK lands (their rows are gone; T39 GCs any hiccup). On commit they are LIVE rows'
    # blobs and are never touched.
    added_object_keys: list[str] = []
    any_failed = False

    async def _do_add(cur, feat: Any) -> dict[str, Any]:
        if not isinstance(feat, dict):
            raise _EditError("each add must be a {geometry, attributes} object")
        if (deny := _op_deny(meta, "add")) is not None:  # V109 — per result, before any SQL
            raise _EditError(deny, code=403)
        attrs = feat.get("attributes") or {}
        _np, notes, props = _split_attrs(attrs, lookup)
        if props:
            try:
                validate_props(field_defs, props)
            except PropsError as e:
                raise _EditError(f"invalid field values: {e.errors}")

        # PRESERVE-GLOBALID (decision §F6) — the one genuinely new write shape in this slice: a
        # client-supplied primary key. Honored ONLY under useGlobalIds, and that gate is
        # load-bearing, not ceremony. Clients echo the full declared field set back on ordinary
        # edits (QGIS pre-reads then sends everything — see _split_attrs), so a GlobalID attribute
        # arrives routinely from callers with no preserve intent; a QGIS copy-paste would carry the
        # SOURCE features' GlobalIDs into adds. Honoring it unconditionally would be a PK-hijack
        # surface. Absent the flag it stays discarded by _split_attrs, exactly as before.
        # Esri puts the value in the attributes; some clients also hang it off the feature — accept
        # either, reject anything that isn't a GUID (never reaches SQL).
        gid: str | None = None
        if use_global_ids:
            raw = next((v for k, v in attrs.items() if str(k).lower() == "globalid"), None)
            if raw is None:
                raw = feat.get("globalId")
            gid = _parse_global_id(raw)
            if gid is None:
                raise _EditError(
                    "useGlobalIds=true requires a GlobalID on every add "
                    "(braced or bare GUID); got " + (repr(raw) if raw is not None else "none"))

        cols = ["notes", "props", "created_by", "updated_by"]
        vals = ["%(notes)s", "%(props)s", "%(actor)s", "%(actor)s"]
        p: dict[str, Any] = {}
        if not is_table:
            if "geometry" not in feat:
                raise _EditError("an add on a spatial layer requires a geometry")
            expr, p = _geom_sql(geom_type, native, srid, feat.get("geometry"))
            cols.insert(0, "geom")
            vals.insert(0, expr)  # a trusted fragment from _geom_sql; its values ride in `p`
        if gid is not None:
            cols.append("id")
            vals.append("%(gid)s::uuid")
            p["gid"] = gid
        q = sql.SQL(
            "INSERT INTO {tbl} (" + ", ".join(cols) + ") "
            "VALUES (" + ", ".join(vals) + ") RETURNING objectid, id::text"
        ).format(tbl=tbl)
        await cur.execute(q, {**p, "notes": notes, "props": Jsonb(props), "actor": actor})
        row = await cur.fetchone()
        return _ok_result(row[0], global_id=row[1])

    # Under useGlobalIds the whole edit set is GlobalID-keyed — Esri's contract is all-or-nothing,
    # not adds-only (decision F3). The key rides straight into the predicate as `id = …::uuid`
    # rather than being pre-resolved to an OBJECTID: one query, and the uuid IS the PK.
    def _edit_key(feat: dict[str, Any], attrs: dict[str, Any], what: str) -> tuple[Any, str | None]:
        """(row key for the WHERE predicate, globalId to echo). GlobalID under the flag, else OID."""
        if use_global_ids:
            raw = next((v for k, v in attrs.items() if str(k).lower() == "globalid"), None)
            if raw is None:
                raw = feat.get("globalId")
            gid = _parse_global_id(raw)
            if gid is None:
                raise _EditError(f"useGlobalIds=true requires a GlobalID on every {what}")
            return gid, gid
        raw = next((v for k, v in attrs.items() if str(k).lower() == "objectid"), None)
        try:
            return int(raw), None
        except (TypeError, ValueError):
            raise _EditError(f"an {what} must carry the feature's OBJECTID attribute")

    def _key_pred() -> str:
        return "id = %(key)s::uuid" if use_global_ids else "objectid = %(key)s"

    def _not_found(key: Any) -> str:
        return ("feature not found (GlobalID %s)" % _braced_guid(key)) if use_global_ids \
            else f"feature not found (OBJECTID {key})"

    async def _do_update(cur, feat: Any) -> dict[str, Any]:
        if not isinstance(feat, dict):
            raise _EditError("each update must be a {geometry?, attributes} object")
        attrs = feat.get("attributes") or {}
        key, gid = _edit_key(feat, attrs, "update")
        notes_present, notes, props = _split_attrs(attrs, lookup)
        # V109 (§3.3): an update carrying geometry is a `geometry` op; notes/props an `attributes` op;
        # both when both ride along (Q1: strict — the known clients send geometry only on a move).
        if feat.get("geometry") is not None and (deny := _op_deny(meta, "geometry")) is not None:
            raise _EditError(deny, code=403)
        if (notes_present or props) and (deny := _op_deny(meta, "attributes")) is not None:
            raise _EditError(deny, code=403)
        sets: list[str] = []
        p: dict[str, Any] = {"key": key, "actor": actor}
        if feat.get("geometry") is not None:
            if is_table:
                raise _EditError("a table row has no geometry to edit")
            expr, gp = _geom_sql(geom_type, native, srid, feat["geometry"])
            sets.append(f"geom = {expr}")
            p.update(gp)
        if notes_present:
            sets.append("notes = %(notes)s")
            p["notes"] = notes
        if props:
            try:
                validate_props(field_defs, props)
            except PropsError as e:
                raise _EditError(f"invalid field values: {e.errors}")
            sets.append("props = props || %(props)s")
            p["props"] = Jsonb(props)
        if not sets:  # only the key came back — a harmless no-op, but the feature must exist
            await cur.execute(sql.SQL(
                "SELECT objectid, id::text FROM {tbl} WHERE " + _key_pred()).format(tbl=tbl), p)
            row = await cur.fetchone()
            if row is None:
                raise _EditError(_not_found(key))
            return _ok_result(row[0], global_id=row[1])
        sets += ["updated_by = %(actor)s", "updated_at = now()"]
        # Last-write-wins (fork 3): the identity predicate only — no version predicate. The BEFORE
        # UPDATE trigger still bumps row_version, keeping the portal's If-Match protection intact.
        # The PK itself is NEVER in `sets`: a GlobalID identifies the row, it never rewrites it.
        # RETURNING the uuid too, so EVERY successful result carries its globalId — including in
        # OBJECTID mode, at no extra query (the row is already being written).
        q = sql.SQL("UPDATE {tbl} SET " + ", ".join(sets) + " WHERE " + _key_pred() +
                    " RETURNING objectid, id::text").format(tbl=tbl)
        await cur.execute(q, p)
        row = await cur.fetchone()
        if row is None:
            raise _EditError(_not_found(key))
        return _ok_result(row[0], global_id=row[1])

    async def _do_delete(cur, key: Any) -> dict[str, Any]:
        if (deny := _op_deny(meta, "delete")) is not None:  # V109 — per result, before any SQL
            raise _EditError(deny, code=403)
        # FEATURE FIRST, then its attachments (decision-feature-delete-attachments.md §F6): the
        # not-found short-circuit below must fire before any attachment row is touched. The uuid the
        # attachments are keyed by comes back from THIS statement, so the cascade costs no extra
        # query and works identically in OBJECTID and GlobalID mode.
        await cur.execute(sql.SQL(
            "DELETE FROM {tbl} WHERE " + _key_pred() +
            " RETURNING objectid, id::text").format(tbl=tbl), {"key": key})
        row = await cur.fetchone()
        if row is None:
            raise _EditError(_not_found(key))
        # Same transaction (and same per-edit savepoint) as the feature delete, so a per-edit
        # rollback takes the attachment rows back with it. The blobs are removed after the OUTER
        # commit — never here.
        keys = await att.delete_for_feature(conn, layer_id, row[1], user, _GEO_AUDIT_CLIENT)
        result = _ok_result(row[0], global_id=row[1])
        # Recorded LAST, once this edit can no longer fail. Anything that threw after the extend
        # would roll the edit back to its savepoint (restoring the attachment rows) while leaving
        # their keys queued for post-commit removal — deleting the bytes out from under live rows.
        del_object_keys.extend(keys)
        return result

    async with conn.cursor() as cur:
        # GUCs BEFORE the outer savepoint: a partial rollback must not un-set actor attribution.
        await set_audit_context(cur, user, _GEO_AUDIT_CLIENT)
        await cur.execute("SAVEPOINT applyedits_all")

        async def run(coro_fn, item, oid: int | None = None,
                      gid: str | None = None) -> dict[str, Any]:
            # `oid`/`gid` = the attempted identity, echoed on failure so Pro can correlate the
            # failed result to its edit. An add has no pre-assigned objectId, but it DOES carry a
            # GlobalID under useGlobalIds — so a failed preserve-add is correlatable too.
            nonlocal any_failed
            await cur.execute("SAVEPOINT applyedits_one")
            try:
                return await coro_fn(cur, item)
            except _EditError as e:
                await cur.execute("ROLLBACK TO SAVEPOINT applyedits_one")
                any_failed = True
                return _err_result(e.message, code=e.code, oid=oid, global_id=gid)
            except pg_errors.CheckViolation as e:  # the validation trigger's honest rejection
                await cur.execute("ROLLBACK TO SAVEPOINT applyedits_one")
                any_failed = True
                return _err_result(str(map_props_check_violation(e).detail), oid=oid,
                                   global_id=gid)
            except pg_errors.UniqueViolation:  # decision §S1/§S2 — preserve-GlobalID collision
                # A supplied GlobalID that already exists in THIS layer. It fails honestly, per
                # result, at HTTP 200 — never a 500, and never an upsert: an Append must not mutate
                # a row it didn't create, and DO NOTHING would report success for a feature that
                # never landed (silent loss — the B.5 fence). Under the default
                # rollbackOnFailure=true nothing in the batch lands either.
                await cur.execute("ROLLBACK TO SAVEPOINT applyedits_one")
                any_failed = True
                return _err_result("a feature with this GlobalID already exists in this layer",
                                   code=1000, oid=oid, global_id=gid)
            except (pg_errors.InternalError_, pg_errors.DataError) as e:  # PostGIS rejected geometry
                await cur.execute("ROLLBACK TO SAVEPOINT applyedits_one")
                any_failed = True
                return _err_result(f"invalid geometry: {str(e).splitlines()[0][:200]}", oid=oid,
                                   global_id=gid)

        for feat in adds:
            add_results.append(await run(_do_add, feat, None, _attempted_edit_keys(feat)[1]))
        for feat in updates:
            u_oid, u_gid = _attempted_edit_keys(feat)
            upd_results.append(await run(_do_update, feat, u_oid, u_gid))
        for key in delete_oids:
            del_results.append(await run(_do_delete, key,
                                         None if use_global_ids else key,
                                         key if use_global_ids else None))

        # -- the attachments sub-object, AFTER the feature edits (decision F7: a parentGlobalId may
        # name a feature added THIS request; a parent deleted this batch is honestly not-found).
        # Adds only (F4); every post-auth "no" is a per-result error with the honest code at HTTP
        # 200 — the shape Pro displays. The bytes ride the SAME authority as every other door:
        # validate_upload (allowlist + extension + magic bytes) then att.store (count cap + audit).
        if atts is not None:
            # V109 F2: a photo is an attribute — the Edit-attributes flag governs the sub-object too.
            att_deny = _op_deny(meta, "attributes") or (
                None if meta["attachments_enabled"]
                else f"attachments are not enabled for {layer_id}")
            max_bytes = settings.attachment_max_bytes
            # base64 inflates 3 → 4: bound the STRING before decoding so an oversize upload is
            # rejected without ever allocating its decoded bytes (+8 slack for padding/newlines).
            max_b64 = (max_bytes + 2) // 3 * 4 + 8
            too_large = (413, f"attachment too large (max {max_bytes // (1024 * 1024)} MB)")

            async def _att_add(item: Any) -> dict[str, Any]:
                nonlocal any_failed
                p_oid, p_gid = _att_parent_keys(item)

                def err(code: int, msg: str) -> dict[str, Any]:
                    nonlocal any_failed
                    any_failed = True
                    return _att_add_result(None, p_oid, p_gid, error=(code, msg))

                if not isinstance(item, dict):
                    return err(400, "each attachment add must be a JSON object")
                if att_deny is not None:
                    return err(403, att_deny)
                name = item.get("name")
                if not isinstance(name, str) or not name or "\x00" in name:
                    return err(400, "an attachment add requires a name (the filename)")
                raw = item.get("data")
                if not isinstance(raw, str) or not raw:
                    if item.get("uploadId") is not None:
                        # F2 — no upload-session endpoint exists; the only byte source is inline.
                        return err(400, "upload sessions are not supported — send the file "
                                        "inline as base64 data")
                    return err(400, "an attachment add requires inline base64 data")
                if len(raw) > max_b64:
                    return err(*too_large)
                try:
                    data = base64.b64decode(raw, validate=True)
                except (binascii.Error, ValueError):
                    return err(400, "data is not valid base64")
                if len(data) > max_bytes:
                    return err(*too_large)
                content_type = item.get("contentType")
                if not isinstance(content_type, str) or not content_type:
                    content_type = att.content_type_for_filename(name)  # ext-derived; fence still runs
                try:
                    content_type = att.validate_upload(name, content_type, data)
                except HTTPException as e:  # 400 empty / 415 disallowed-or-spoofed
                    return err(e.status_code, e.detail)
                # Parent resolution — layer-scoped by construction (decision §5): both resolvers
                # query assets.<layer_id> for the SAME layer the gate authorized, so a key fished
                # from another layer resolves to nothing and no cross-layer landing exists.
                if item.get("parentGlobalId") is not None:
                    if p_gid is None:
                        return err(400, "parentGlobalId must be a GUID (braced or bare)")
                    feature_id = p_gid if await att.feature_exists(conn, layer_id, p_gid) else None
                elif p_oid is not None:
                    feature_id = await _feature_uuid(conn, layer_id, p_oid)
                else:
                    return err(400, "an attachment add requires parentObjectId or parentGlobalId")
                if feature_id is None:
                    return err(404, "feature not found")
                await cur.execute("SAVEPOINT applyedits_one")
                try:
                    row = await att.store(conn, layer_id=layer_id, feature_id=feature_id,
                                          filename=name, content_type=content_type, data=data,
                                          user=user, client=_GEO_AUDIT_CLIENT)
                except HTTPException as e:  # the count-cap 409
                    await cur.execute("ROLLBACK TO SAVEPOINT applyedits_one")
                    return err(e.status_code, e.detail)
                except MediaStoreUnavailable:
                    await cur.execute("ROLLBACK TO SAVEPOINT applyedits_one")
                    return err(502, "media store unreachable")
                # store()'s key shape (attachments.py): the row id IS the blob key's last segment.
                added_object_keys.append(f"attachments/{layer_id}/{feature_id}/{row['id']}")
                return _att_add_result(row["esri_id"], p_oid, p_gid)

            for item in atts["adds"]:
                att_add_results.append(await _att_add(item))
            if atts["updates"] or atts["deletes"]:  # F4 — refused honestly, and a refusal is a
                any_failed = True                   # failure (it participates in rollback)

        if rollback and any_failed:
            # Honest atomicity (the spec default): NOTHING lands; every result reports failure,
            # the genuinely-failing ones keep their specific error.
            await cur.execute("ROLLBACK TO SAVEPOINT applyedits_all")
            # The rollback restored every cascaded attachment row, so their blobs are LIVE again and
            # must not be touched. Dropping the list here is what makes "blobs after commit" safe for
            # the batch case — the rows and their bytes stay together, exactly as before the request.
            del_object_keys.clear()
            def _mark(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [r if not r["success"]
                        else _err_result("edit rolled back because another edit in the request "
                                         "failed (rollbackOnFailure=true)", code=1003,
                                         # keep BOTH ids the success carried, so a rolled-back
                                         # result stays correlatable (_braced_guid is idempotent)
                                         oid=r.get("objectId"), global_id=r.get("globalId"))
                        for r in results]
            add_results, upd_results, del_results = (
                _mark(add_results), _mark(upd_results), _mark(del_results))
            # Attachment adds are batch citizens too (decision F3): successes re-mark 1003 keeping
            # their echoes ({**r}, not _err_result — the parent correlation must survive), and
            # their rows are rolled back, so their already-written blobs are now STRANDED and get
            # removed after the transaction settles (below). Genuine failures keep their codes.
            att_add_results = [
                r if not r["success"]
                else {**r, "success": False,
                      "error": {"code": 1003,
                                "description": "edit rolled back because another edit in the "
                                               "request failed (rollbackOnFailure=true)"}}
                for r in att_add_results]
        else:
            added_object_keys.clear()  # committed rows own their blobs — never touch them

    await conn.commit()
    # Stranded add-blobs AFTER the rollback+commit settle, best-effort — their rows are gone, so
    # the keys stop looking live and T39 collects any hiccup (same posture as the cascade below).
    for object_key in added_object_keys:
        try:
            await run_in_threadpool(remove_object, object_key)
        except MediaStoreUnavailable:
            log.warning("esri_attachment_blob_orphaned", object_key=object_key, layer_id=layer_id)
    # Cascaded blobs AFTER the outer commit, best-effort — a store hiccup now leaves a blob whose row
    # is already gone, so its key stops looking live and T39 genuinely collects it (which was NOT true
    # before this slice: a surviving row made the key look permanently referenced).
    for object_key in del_object_keys:
        try:
            await run_in_threadpool(remove_object, object_key)
        except MediaStoreUnavailable:
            log.warning("esri_attachment_blob_orphaned", object_key=object_key, layer_id=layer_id)
    n_ok = sum(1 for r in add_results + upd_results + del_results + att_add_results
               if r["success"])
    log.info("geoservices_apply_edits", layer_id=layer_id, actor=actor, mode=mode,
             adds=len(add_results), updates=len(upd_results), deletes=len(del_results),
             attachment_adds=len(att_add_results),
             succeeded=n_ok, rolled_back=rollback and any_failed,
             attachments_cascaded=len(del_object_keys))
    doc = {"addResults": add_results, "updateResults": upd_results, "deleteResults": del_results}
    if atts is not None:
        # F1: the key exists exactly when the request carried the sub-object — a plain feature-edit
        # response stays byte-identical to before this slice.
        doc["attachments"] = {
            "addResults": att_add_results,
            "updateResults": [_att_refusal("update", u) for u in atts["updates"]],
            "deleteResults": [_att_refusal("delete", d) for d in atts["deletes"]],
        }
    return doc


# --- the router factory (two doors, ogc.py pattern) ------------------------------------------------


def _build_geoservices_router(mount: str, *, challenge: bool) -> APIRouter:
    router = APIRouter(tags=["geoservices"])
    in_schema = not challenge
    cors_any = not challenge  # fork 9a: the PUBLIC door carries the AGO CORS header

    def guard(principal: User | None) -> None:
        if challenge and principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required — this is the authenticated GeoServices endpoint",
                headers={"WWW-Authenticate": BASIC_CHALLENGE},
            )

    def reply(payload: Any, params: dict[str, str], status_code: int = 200) -> Response:
        return _json_esri(payload, pretty=_wants_pretty(params), status_code=status_code,
                          cors_any=cors_any)

    @router.api_route(f"{mount}/rest/info", methods=["GET", "HEAD", "POST"],
                      summary="GeoServices server info (auth discovery)", include_in_schema=in_schema)
    async def rest_info(request: Request,
                        principal: User | None = Depends(get_optional_user)) -> Response:
        # NO guard: /rest/info is the pre-auth discovery document — Esri clients fetch it before
        # deciding how to authenticate, so even the sign-in door answers it anonymously. It
        # discloses nothing but the auth topology. owningSystemUrl is deliberately ABSENT.
        params = await _request_params(request)
        return reply({
            "currentVersion": _CURRENT_VERSION,
            "fullVersion": _FULL_VERSION,
            "authInfo": {"isTokenBasedSecurity": False},
        }, params)

    # Trailing-slash aliases (below, on the three browsable roots): ArcGIS Pro — and ArcGIS REST
    # clients generally — authenticate at the services/FeatureServer roots WITH a trailing slash.
    # FastAPI's default 307 slash-redirect makes Pro's HTTP client DROP its Basic auth header on
    # the redirected request → 401, which blocked Add-Data-From-Path (rig, 2026-07-18). Registering
    # the slash variant as a real route serves it directly, so no redirect occurs and the credential
    # survives. `include_in_schema=False` keeps the alias out of OpenAPI (no duplicate operation id).
    @router.api_route(f"{mount}/rest/services/", methods=["GET", "HEAD", "POST"], include_in_schema=False)
    @router.api_route(f"{mount}/rest/services", methods=["GET", "HEAD", "POST"],
                      summary="GeoServices services directory — the caller's viewable layers",
                      include_in_schema=in_schema)
    async def services_directory(request: Request,
                                 conn: AsyncConnection = Depends(get_db),
                                 principal: User | None = Depends(get_optional_user)) -> Response:
        guard(principal)
        params = await _request_params(request)
        layers = await _viewable_layers(conn, principal)
        return reply({
            "currentVersion": _CURRENT_VERSION,
            "folders": [],
            "services": [{"name": lid, "type": "FeatureServer"} for lid, _n, _g in layers],
        }, params)

    async def _gate(request: Request, conn: AsyncConnection, principal: User | None,
                    layer_id: str) -> tuple[dict[str, Any], bool]:
        """The shared per-service gate: registered → readable → registry row. A PAT caller denied
        VIEW gets the 401-INVITE (re-prompt) instead of a dead-end 403 — the fix is "use another
        token," and only a challenge re-prompts Pro/QGIS. Bearer keeps its 403; anon keeps its 404
        (both unchanged). PAT-ness is `request.state.pat_scope` (set by auth._pat_user)."""
        await _require_registered(conn, layer_id)
        try:
            is_anon = await _authorize_read(conn, principal, layer_id)
        except HTTPException as e:
            if e.status_code == status.HTTP_403_FORBIDDEN and \
                    getattr(request.state, "pat_scope", None) is not None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="this access token's account can't see this layer — "
                    "sign in with a token whose account has access",
                    headers={"WWW-Authenticate": BASIC_CHALLENGE_NO_VIEW},
                ) from e
            raise
        meta = await _layer_row(conn, layer_id)
        return meta, is_anon

    @router.api_route(f"{mount}/rest/services/{{layer_id}}/FeatureServer/", methods=["GET", "HEAD", "POST"],
                      include_in_schema=False)  # trailing-slash alias (see services_directory note)
    @router.api_route(f"{mount}/rest/services/{{layer_id}}/FeatureServer", methods=["GET", "HEAD", "POST"],
                      summary="FeatureServer service root (one service per layer)",
                      include_in_schema=in_schema)
    async def service_root(layer_id: str, request: Request,
                           conn: AsyncConnection = Depends(get_db),
                           principal: User | None = Depends(get_optional_user)) -> Response:
        guard(principal)
        params = await _request_params(request)
        meta, _is_anon = await _gate(request, conn, principal, layer_id)
        return reply(await _service_doc(conn, layer_id, meta), params)

    @router.api_route(f"{mount}/rest/services/{{layer_id}}/FeatureServer/layers",
                      methods=["GET", "HEAD", "POST"], summary="All layers of the service (single-layer)",
                      include_in_schema=in_schema)
    async def service_layers(layer_id: str, request: Request,
                             conn: AsyncConnection = Depends(get_db),
                             principal: User | None = Depends(get_optional_user)) -> Response:
        guard(principal)
        params = await _request_params(request)
        meta, is_anon = await _gate(request, conn, principal, layer_id)
        doc = await _layer_doc(conn, layer_id, meta, is_anon=is_anon)
        is_table = meta["geom_type"] == "none"
        return reply({"layers": [] if is_table else [doc], "tables": [doc] if is_table else []},
                     params)

    @router.api_route(f"{mount}/rest/services/{{layer_id}}/FeatureServer/0/", methods=["GET", "HEAD", "POST"],
                      include_in_schema=False)  # trailing-slash alias (see services_directory note)
    @router.api_route(f"{mount}/rest/services/{{layer_id}}/FeatureServer/0", methods=["GET", "HEAD", "POST"],
                      summary="Layer metadata (fields/domains/capabilities)", include_in_schema=in_schema)
    async def layer_metadata(layer_id: str, request: Request,
                             conn: AsyncConnection = Depends(get_db),
                             principal: User | None = Depends(get_optional_user)) -> Response:
        guard(principal)
        params = await _request_params(request)
        meta, is_anon = await _gate(request, conn, principal, layer_id)
        return reply(await _layer_doc(conn, layer_id, meta, is_anon=is_anon), params)

    @router.api_route(f"{mount}/rest/services/{{layer_id}}/FeatureServer/0/query",
                      methods=["GET", "HEAD", "POST"], summary="Feature query (esriJSON)",
                      include_in_schema=in_schema)
    async def layer_query_op(layer_id: str, request: Request,
                             conn: AsyncConnection = Depends(get_db),
                             principal: User | None = Depends(get_optional_user)) -> Response:
        guard(principal)
        params = await _request_params(request)
        meta, is_anon = await _gate(request, conn, principal, layer_id)
        try:
            doc = await _run_query(conn, layer_id, meta, params, is_anon,
                                   request=request, mount=mount)
        except _EsriBadRequest as e:
            return reply(_esri_error_doc(400, e.message), params, status_code=400)
        return reply(doc, params)

    # -- editing endpoints: Pro's applyEdits + the trio QGIS actually calls (recon §1.1). The ONLY
    # HTTP-level failure is AUTHENTICATION (no credential → 401 + challenge, which makes the client
    # prompt); every other "no" (read-only token, no edit access, editing off) rides the result
    # arrays as a per-edit failure, because QGIS/Pro show addResults[].error.description but ignore
    # an edit-POST's HTTP error body (the recon trap — and what left a denied user staring at a
    # bare "Forbidden").
    async def _handle_edit(layer_id: str, request: Request, conn: AsyncConnection,
                           principal: User | None, claims: dict[str, Any] | None,
                           mode: str) -> Response:
        params = await _request_params(request, edit=True)
        if principal is None:  # authentication is the one thing that stays HTTP-level
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required to edit",
                headers={"WWW-Authenticate": BASIC_CHALLENGE},
            )
        try:
            await _require_registered(conn, layer_id)
            meta = await _layer_row(conn, layer_id)
            doc = await _apply_edits_core(conn, request, layer_id, meta, params, principal, claims, mode)
        except _EsriBadRequest as e:
            return reply(_esri_error_doc(400, e.message), params, status_code=400)
        if mode == "add":
            doc = {"addResults": doc["addResults"]}
        elif mode == "update":
            doc = {"updateResults": doc["updateResults"]}
        elif mode == "delete":
            doc = {"deleteResults": doc["deleteResults"]}
        return reply(doc, params)

    @router.post(f"{mount}/rest/services/{{layer_id}}/FeatureServer/0/applyEdits",
                 summary="Apply edits (adds/updates/deletes — atomic by default)",
                 include_in_schema=in_schema)
    async def apply_edits(layer_id: str, request: Request,
                          conn: AsyncConnection = Depends(get_db),
                          principal: User | None = Depends(get_optional_user),
                          claims: dict[str, Any] | None = Depends(_optional_claims)) -> Response:
        return await _handle_edit(layer_id, request, conn, principal, claims, "applyEdits")

    @router.post(f"{mount}/rest/services/{{layer_id}}/FeatureServer/applyEdits",
                 summary="Apply edits, SERVICE level (what ArcGIS Pro actually calls)",
                 include_in_schema=in_schema)
    async def service_apply_edits(layer_id: str, request: Request,
                                  conn: AsyncConnection = Depends(get_db),
                                  principal: User | None = Depends(get_optional_user),
                                  claims: dict[str, Any] | None = Depends(_optional_claims)) -> Response:
        # The SERVICE-level batch form: `edits` = [{id, adds, updates, deletes}, …], response =
        # an ARRAY of per-layer results. Pro saves through THIS endpoint, not the layer-level
        # one (found live on the Pro rig, 2026-07-08 — the docs' layer-level example misled the
        # recon). One layer per service here, so exactly one edits entry (id 0) is accepted;
        # everything else delegates to the same engine.
        params = await _request_params(request, edit=True)
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required to edit",
                headers={"WWW-Authenticate": BASIC_CHALLENGE},
            )
        try:
            try:
                edits = json.loads(params.get("edits") or "")
            except ValueError:
                raise _EsriBadRequest("unparseable edits JSON")
            if not isinstance(edits, list) or not edits:
                raise _EsriBadRequest("edits must be a non-empty JSON array")
            entry = next((e for e in edits if isinstance(e, dict) and str(e.get("id")) == "0"), None)
            if entry is None:
                raise _EsriBadRequest("this service has one layer — edits must target layer id 0")
            layer_params = dict(params)
            layer_params.pop("edits", None)
            layer_params["adds"] = json.dumps(entry.get("adds") or [])
            layer_params["updates"] = json.dumps(entry.get("updates") or [])
            deletes = entry.get("deletes")
            layer_params["deletes"] = deletes if isinstance(deletes, str) else json.dumps(deletes or [])
            # The attachments sub-object (decision-geoservices-applyedits-attachments.md) — the
            # payload Pro's Add-Attachments GP tool actually sends. Re-packed verbatim for the one
            # engine; dropping it here was the whole bug (rig thread 1, 2026-07-22).
            layer_params.pop("attachments", None)
            if entry.get("attachments") is not None:
                layer_params["attachments"] = json.dumps(entry["attachments"])
            await _require_registered(conn, layer_id)
            meta = await _layer_row(conn, layer_id)
            doc = await _apply_edits_core(conn, request, layer_id, meta, layer_params,
                                          principal, claims, "applyEdits")
        except _EsriBadRequest as e:
            return reply(_esri_error_doc(400, e.message), params, status_code=400)
        return reply([{"id": 0, **doc}], params)  # ARRAY, per the service-level contract

    @router.post(f"{mount}/rest/services/{{layer_id}}/FeatureServer/0/addFeatures",
                 summary="Add features (QGIS edit dialect)", include_in_schema=in_schema)
    async def add_features(layer_id: str, request: Request,
                           conn: AsyncConnection = Depends(get_db),
                           principal: User | None = Depends(get_optional_user),
                           claims: dict[str, Any] | None = Depends(_optional_claims)) -> Response:
        return await _handle_edit(layer_id, request, conn, principal, claims, "add")

    @router.post(f"{mount}/rest/services/{{layer_id}}/FeatureServer/0/updateFeatures",
                 summary="Update features (QGIS edit dialect)", include_in_schema=in_schema)
    async def update_features(layer_id: str, request: Request,
                              conn: AsyncConnection = Depends(get_db),
                              principal: User | None = Depends(get_optional_user),
                              claims: dict[str, Any] | None = Depends(_optional_claims)) -> Response:
        return await _handle_edit(layer_id, request, conn, principal, claims, "update")

    @router.post(f"{mount}/rest/services/{{layer_id}}/FeatureServer/0/deleteFeatures",
                 summary="Delete features (QGIS edit dialect)", include_in_schema=in_schema)
    async def delete_features(layer_id: str, request: Request,
                              conn: AsyncConnection = Depends(get_db),
                              principal: User | None = Depends(get_optional_user),
                              claims: dict[str, Any] | None = Depends(_optional_claims)) -> Response:
        return await _handle_edit(layer_id, request, conn, principal, claims, "delete")

    # -- the QGIS write-chute door (A-3, decision §2.3.1 + the §9 rig note). The Attachment
    # widget's External Storage does a RAW-BODY authenticated PUT to a per-feature URL (NOT
    # multipart, NO Content-Type header — rig-proven), and previews with a GET on the same URL.
    # Unlike the edit endpoints there is no per-result channel here, so denials are plain HTTP
    # (QGIS shows "server replied: Forbidden/…" in the message bar; the recipe documents the
    # fixes). Never redirect these routes — the rig proved the client dies on a 302.

    @router.put(f"{mount}/layers/{{layer_id}}/attachments/{{objectid}}/{{filename}}",
                status_code=status.HTTP_201_CREATED,
                summary="QGIS attachment write-chute — raw-body PUT, feature-addressed",
                include_in_schema=in_schema)
    async def chute_upload(layer_id: str, objectid: int, filename: str, request: Request,
                           conn: AsyncConnection = Depends(get_db),
                           principal: User | None = Depends(get_optional_user),
                           claims: dict[str, Any] | None = Depends(_optional_claims)
                           ) -> dict[str, Any]:
        if principal is None:  # writes are never anonymous, on either door
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required to upload attachments",
                headers={"WWW-Authenticate": BASIC_CHALLENGE},
            )
        await _require_registered(conn, layer_id)
        meta = await _layer_row(conn, layer_id)
        deny = await _edit_deny_reason(request, conn, principal, claims, layer_id, meta)
        if deny is None:
            deny = _op_deny(meta, "attributes")  # V109 F2: a photo is an attribute
        if deny is not None:  # the SAME gate applyEdits runs (PAT read_write × editing × sharing)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=deny)
        if not meta["attachments_enabled"]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"attachments are not enabled for {layer_id}")
        # Bounded raw-body read: never buffer more than cap+1 bytes (413 the moment it overflows).
        max_bytes = settings.attachment_max_bytes
        too_large = HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"attachment too large (max {max_bytes // (1024 * 1024)} MB)")
        declared_len = request.headers.get("content-length", "")
        if declared_len.isdigit() and int(declared_len) > max_bytes:
            raise too_large
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                raise too_large
            chunks.append(chunk)
        data = b"".join(chunks)
        # The URL's filename extension is the ONLY type declaration the client sends (rig-proven);
        # validate_upload then holds the magic-byte fence against it exactly as on every door.
        content_type = att.content_type_for_filename(filename)
        if content_type is None:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"file type of '{filename}' is not an allowed attachment type")
        content_type = att.validate_upload(filename, content_type, data)
        feature_id = await _feature_uuid(conn, layer_id, objectid)
        if feature_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feature not found")
        try:
            row = await att.store(
                conn, layer_id=layer_id, feature_id=feature_id, filename=filename,
                content_type=content_type, data=data, user=principal,
                client=_GEO_AUDIT_CLIENT,
            )
        except HTTPException:  # count-cap 409 — nothing to keep
            await conn.rollback()
            raise
        except MediaStoreUnavailable:
            await conn.rollback()
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                detail="media store unreachable")
        await conn.commit()
        log.info("chute_attachment_added", layer_id=layer_id, objectid=objectid,
                 attachment_id=row["id"], byte_size=row["byte_size"],
                 content_type=content_type, actor=str(principal.id))
        return {"id": row["id"], "filename": row["filename"], "byteSize": row["byte_size"]}

    @router.get(f"{mount}/layers/{{layer_id}}/attachments/{{objectid}}/{{filename}}",
                summary="QGIS attachment chute download — newest bytes for this filename",
                include_in_schema=in_schema)
    async def chute_download(layer_id: str, objectid: int, filename: str, request: Request,
                             conn: AsyncConnection = Depends(get_db),
                             principal: User | None = Depends(get_optional_user)) -> Response:
        guard(principal)
        await _gate(request, conn, principal, layer_id)  # view-gated; anon-when-public on /esri
        feature_id = await _feature_uuid(conn, layer_id, objectid)
        if feature_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feature not found")
        row = await att.get_by_filename(conn, layer_id, feature_id, filename)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="attachment not found")
        try:
            data = await run_in_threadpool(read_object, row["object_key"])
        except MediaObjectMissing:
            log.warning("chute_blob_missing", object_key=row["object_key"], layer_id=layer_id)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="attachment bytes not found")
        except MediaStoreUnavailable:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                detail="media store unreachable")
        # no-store: this URL is latest-by-filename (mutable on re-upload) — never cache-pin it.
        return Response(content=data, media_type=row["content_type"],
                        headers={"Cache-Control": "no-store"})

    # -- the GeoServices attachments subset (A-2, decision §2.2 + §11): the five OBJECTID-keyed
    # endpoints behind hasAttachments, all delegating into the ONE media.attachments authority
    # through the SAME gate/cap/audit chain as A-1/A-3. Reads are view-gated (anon-when-public on
    # the public door); writes run _edit_deny_reason + the attachments_enabled hard switch. On the
    # three POSTs every post-authentication "no" rides the RESULT shape at HTTP 200 (§11.7 — Pro
    # ignores an edit-POST's HTTP error body); malformed protocol params stay the top-level Esri
    # 400 like applyEdits. The wire attachmentId = media.attachments.esri_id (V069), always
    # resolved (layer, feature, esri_id)-scoped so an id never crosses features.

    async def _att_write_gate(request: Request, conn: AsyncConnection, principal: User | None,
                              claims: dict[str, Any] | None, layer_id: str) -> str | None:
        """The attachment-POST gate. Authentication stays HTTP-level (401 + challenge — what makes
        the client re-prompt); returns the deny sentence for the per-result error, or None."""
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required to edit attachments",
                headers={"WWW-Authenticate": BASIC_CHALLENGE},
            )
        await _require_registered(conn, layer_id)
        meta = await _layer_row(conn, layer_id)
        deny = await _edit_deny_reason(request, conn, principal, claims, layer_id, meta)
        if deny is None:
            deny = _op_deny(meta, "attributes")  # V109 F2: a photo is an attribute
        if deny is None and not meta["attachments_enabled"]:
            deny = f"attachments are not enabled for {layer_id}"
        return deny

    async def _att_form(request: Request) -> tuple[dict[str, str], Any]:
        """(params, file part) from an attachment multipart POST: query params overlaid with the
        form's STRING fields (f=json rides as a form part in Esri multipart), plus the upload —
        field name `attachment` (Esri's documented parameter) or `file` (defensive; §11.2).
        This is the ONE façade surface that true-multipart-parses (python-multipart, in since
        A-1) — never _request_params, which deliberately skips non-urlencoded bodies."""
        params: dict[str, str] = dict(request.query_params)
        form = await request.form()
        for key, value in form.multi_items():
            if isinstance(value, str):
                params[key] = value
        upload = form.get("attachment")
        if upload is None or isinstance(upload, str):  # absent, or a stray STRING field
            upload = form.get("file")
        return params, (None if isinstance(upload, str) else upload)

    _ATT_TOO_LARGE = (f"attachment too large "
                      f"(max {settings.attachment_max_bytes // (1024 * 1024)} MB)")

    class _AttReject(Exception):
        """A result-side upload rejection (400/413/415) raised by _att_read_upload — becomes
        {success:false, error:{code,description}} in the caller's result shape (§11.7)."""
        def __init__(self, code: int, message: str):
            self.code = code
            self.message = message

    async def _att_read_upload(request: Request) -> tuple[dict[str, str], str, str, bytes]:
        """The shared add/update upload plumbing → (params, filename, content_type, data).
        Content-length precheck (413 before spooling the body; 4 KiB margin for multipart
        framing), no-file-part 400, the bounded read (413 — the authoritative check, exactly
        A-1's idiom), the generic-type → filename-MIME fallback, and validate_upload's 400/415 —
        every rejection raised as _AttReject for the caller's result shape."""
        max_bytes = settings.attachment_max_bytes
        declared_len = request.headers.get("content-length", "")
        if declared_len.isdigit() and int(declared_len) > max_bytes + 4096:
            raise _AttReject(413, _ATT_TOO_LARGE)
        params, upload = await _att_form(request)
        if upload is None:
            raise _AttReject(
                400, "no file part — send multipart/form-data with an 'attachment' file part")
        data = await upload.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise _AttReject(413, _ATT_TOO_LARGE)
        filename = upload.filename or "attachment"
        content_type = upload.content_type
        if not content_type or content_type.lower() == "application/octet-stream":
            # a generic/absent part type: fall back to the filename's allowlisted MIME — the
            # magic-byte fence in validate_upload still holds either way
            content_type = att.content_type_for_filename(filename) or content_type
        try:
            content_type = att.validate_upload(filename, content_type, data)
        except HTTPException as e:  # 400 empty / 415 disallowed-spoofed → result-side (§11.7)
            raise _AttReject(e.status_code, e.detail) from e
        return params, filename, content_type, data

    @router.api_route(f"{mount}/rest/services/{{layer_id}}/FeatureServer/0/{{objectid}}/attachments",
                      methods=["GET", "HEAD", "POST"],
                      summary="A feature's attachmentInfos (Esri attachments protocol)",
                      include_in_schema=in_schema)
    async def attachment_infos(layer_id: str, objectid: int, request: Request,
                               conn: AsyncConnection = Depends(get_db),
                               principal: User | None = Depends(get_optional_user)) -> Response:
        guard(principal)
        params = await _request_params(request)
        await _gate(request, conn, principal, layer_id)  # view-gated; anon-when-public on /esri
        feature_id = await _feature_uuid(conn, layer_id, objectid)
        if feature_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feature not found")
        rows = await att.list_for_feature(conn, layer_id, feature_id)
        return reply({"attachmentInfos": [_att_info_doc(r) for r in rows]}, params)

    @router.get(f"{mount}/rest/services/{{layer_id}}/FeatureServer/0/{{objectid}}"
                "/attachments/{attachment_id}",
                summary="Attachment bytes by Esri id — view-gated, anon-when-public",
                include_in_schema=in_schema)
    async def attachment_download(layer_id: str, objectid: int, attachment_id: int,
                                  request: Request,
                                  conn: AsyncConnection = Depends(get_db),
                                  principal: User | None = Depends(get_optional_user)) -> Response:
        guard(principal)
        await _gate(request, conn, principal, layer_id)
        feature_id = await _feature_uuid(conn, layer_id, objectid)
        if feature_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feature not found")
        row = await att.get_by_esri_id(conn, layer_id, feature_id, attachment_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="attachment not found")
        try:
            data = await run_in_threadpool(read_object, row["object_key"])
        except MediaObjectMissing:
            log.warning("esri_attachment_blob_missing", object_key=row["object_key"],
                        layer_id=layer_id)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="attachment bytes not found")
        except MediaStoreUnavailable:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                detail="media store unreachable")
        # no-store: updateAttachment mutates bytes under a STABLE id — never cache-pin (§11.7).
        return Response(content=data, media_type=row["content_type"],
                        headers={"Cache-Control": "no-store",
                                 "Content-Disposition": _safe_disposition(row["filename"])})

    @router.post(f"{mount}/rest/services/{{layer_id}}/FeatureServer/0/{{objectid}}/addAttachment",
                 summary="Add an attachment (multipart) — Esri attachments protocol",
                 include_in_schema=in_schema)
    async def add_attachment(layer_id: str, objectid: int, request: Request,
                             conn: AsyncConnection = Depends(get_db),
                             principal: User | None = Depends(get_optional_user),
                             claims: dict[str, Any] | None = Depends(_optional_claims)) -> Response:
        deny = await _att_write_gate(request, conn, principal, claims, layer_id)
        params = dict(request.query_params)

        def result(payload: dict[str, Any]) -> Response:
            return reply({"addAttachmentResult": payload}, params)

        try:
            params, filename, content_type, data = await _att_read_upload(request)
        except _AttReject as e:  # a deny outranks an upload rejection (the honest reason first)
            return result(_att_result_err(403, deny) if deny is not None
                          else _att_result_err(e.code, e.message))
        if deny is not None:
            return result(_att_result_err(403, deny))
        feature_id = await _feature_uuid(conn, layer_id, objectid)
        if feature_id is None:
            return result(_att_result_err(404, "feature not found"))
        try:
            row = await att.store(conn, layer_id=layer_id, feature_id=feature_id,
                                  filename=filename, content_type=content_type, data=data,
                                  user=principal, client=_GEO_AUDIT_CLIENT)
        except HTTPException as e:  # count-cap 409 — nothing to keep
            await conn.rollback()
            return result(_att_result_err(e.status_code, e.detail))
        except MediaStoreUnavailable:
            await conn.rollback()
            return result(_att_result_err(502, "media store unreachable"))
        await conn.commit()
        log.info("esri_attachment_added", layer_id=layer_id, objectid=objectid,
                 attachment_id=row["id"], esri_id=row["esri_id"], byte_size=row["byte_size"],
                 content_type=content_type, actor=str(principal.id))
        return result({"objectId": row["esri_id"], "globalId": None, "success": True})

    @router.post(f"{mount}/rest/services/{{layer_id}}/FeatureServer/0/{{objectid}}"
                 "/updateAttachment",
                 summary="Replace an attachment's bytes, keeping its id (Esri attachments protocol)",
                 include_in_schema=in_schema)
    async def update_attachment(layer_id: str, objectid: int, request: Request,
                                conn: AsyncConnection = Depends(get_db),
                                principal: User | None = Depends(get_optional_user),
                                claims: dict[str, Any] | None = Depends(_optional_claims)
                                ) -> Response:
        deny = await _att_write_gate(request, conn, principal, claims, layer_id)
        params = dict(request.query_params)

        def result(payload: dict[str, Any]) -> Response:
            return reply({"updateAttachmentResult": payload}, params)

        try:
            params, filename, content_type, data = await _att_read_upload(request)
        except _AttReject as e:  # a deny outranks an upload rejection (the honest reason first)
            return result(_att_result_err(403, deny) if deny is not None
                          else _att_result_err(e.code, e.message))
        aid = _int_or_none((params.get("attachmentId") or "").strip())
        if aid is None:
            # malformed protocol param → top-level Esri 400, like applyEdits' unparseable edits
            return reply(_esri_error_doc(
                400, "updateAttachment requires an integer attachmentId"), params,
                status_code=400)
        if deny is not None:
            return result(_att_result_err(403, deny))
        feature_id = await _feature_uuid(conn, layer_id, objectid)
        if feature_id is None:
            return result(_att_result_err(404, "feature not found"))
        try:
            row = await att.update_attachment(conn, layer_id=layer_id, feature_id=feature_id,
                                              esri_id=aid, filename=filename,
                                              content_type=content_type, data=data,
                                              user=principal, client=_GEO_AUDIT_CLIENT)
        except MediaStoreUnavailable:
            await conn.rollback()
            return result(_att_result_err(502, "media store unreachable"))
        if row is None:
            await conn.rollback()
            return result(_att_result_err(404, "attachment not found"))
        await conn.commit()
        # old blob AFTER commit (a rolled-back update never orphans row from blob); T39 backstops.
        try:
            await run_in_threadpool(remove_object, row["old_object_key"])
        except MediaStoreUnavailable:
            log.warning("esri_attachment_blob_orphaned", object_key=row["old_object_key"],
                        layer_id=layer_id)
        log.info("esri_attachment_updated", layer_id=layer_id, objectid=objectid,
                 attachment_id=row["id"], esri_id=aid, byte_size=row["byte_size"],
                 content_type=content_type, actor=str(principal.id))
        return result({"objectId": aid, "globalId": None, "success": True})

    @router.post(f"{mount}/rest/services/{{layer_id}}/FeatureServer/0/{{objectid}}"
                 "/deleteAttachments",
                 summary="Delete attachments by Esri id (comma list) — per-id results",
                 include_in_schema=in_schema)
    async def delete_attachments(layer_id: str, objectid: int, request: Request,
                                 conn: AsyncConnection = Depends(get_db),
                                 principal: User | None = Depends(get_optional_user),
                                 claims: dict[str, Any] | None = Depends(_optional_claims)
                                 ) -> Response:
        params = await _request_params(request)
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if "attachmentIds" not in params and ctype == "multipart/form-data":
            # defensive: a client that multiparts this form — _request_params skips those bodies
            form = await request.form()
            for key, value in form.multi_items():
                if isinstance(value, str):
                    params.setdefault(key, value)
        deny = await _att_write_gate(request, conn, principal, claims, layer_id)
        ids = [p.strip() for p in (params.get("attachmentIds") or "").split(",") if p.strip()]
        if not ids:
            return reply(_esri_error_doc(
                400, "deleteAttachments requires attachmentIds (a comma-separated integer list)"),
                params, status_code=400)

        if deny is not None:  # every requested id reports the same honest per-result deny
            return reply({"deleteAttachmentResults": [
                {"objectId": _int_or_none(p), **_att_result_err(403, deny)} for p in ids]}, params)
        feature_id = await _feature_uuid(conn, layer_id, objectid)
        if feature_id is None:
            return reply({"deleteAttachmentResults": [
                {"objectId": _int_or_none(p), **_att_result_err(404, "feature not found")}
                for p in ids]}, params)
        results: list[dict[str, Any]] = []
        removed_keys: list[str] = []
        for p in ids:
            aid = _int_or_none(p)
            if aid is None:
                results.append({"objectId": None,
                                **_att_result_err(400, f"invalid attachmentId '{p}'")})
                continue
            object_key = await att.delete_by_esri_id(
                conn, layer_id, feature_id, aid, principal, _GEO_AUDIT_CLIENT)
            if object_key is None:
                results.append({"objectId": aid,
                                **_att_result_err(404, "attachment not found")})
                continue
            removed_keys.append(object_key)
            results.append({"objectId": aid, "globalId": None, "success": True})
        await conn.commit()  # per-id independence — successes land even beside failures
        for key in removed_keys:  # blobs AFTER commit; T39 backstops any hiccup
            try:
                await run_in_threadpool(remove_object, key)
            except MediaStoreUnavailable:
                log.warning("esri_attachment_blob_orphaned", object_key=key, layer_id=layer_id)
        log.info("esri_attachments_deleted", layer_id=layer_id, objectid=objectid,
                 deleted=[r["objectId"] for r in results if r.get("success")],
                 actor=str(principal.id))
        return reply({"deleteAttachmentResults": results}, params)

    return router


public_router = _build_geoservices_router(PUBLIC_MOUNT, challenge=False)
auth_router = _build_geoservices_router(AUTH_MOUNT, challenge=True)
