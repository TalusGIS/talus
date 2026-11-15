"""Layer 8 — publisher endpoints (create-layer, create-group).

create-layer claims one of the pre-minted slot tables (V019/V020) at runtime: it
never issues DDL. The claim is Postgres-FIRST — the slot row moves to 'provisioning'
and that move is COMMITTED before the OpenFGA owner grant — so a failure anywhere
after leaves a VISIBLE 'provisioning' row (recoverable), never an invisible orphan.
Only after the access tuple is in place does the row flip to 'ready' + editable=true.

create-layer / create-group are publisher/admin only (_has_publish_role — stricter than
the edit gate). release-layer (DELETE /layers/{id}) is its lifecycle inverse and is
authorized differently — owner (OpenFGA `owner` relation) OR talus-admin — and runs
OpenFGA-FIRST, the deliberate inverse of the claim's Postgres-first order (decision-11
§Q3). NO endpoint ever derives a SQL identifier, table name, or OpenFGA object from user
input: the layer_id is always a pre-existing frozen slot token (regex+registry guarded
before it names a table), and the group object id is a slug scrubbed to [a-z0-9_]. The
publisher's display_name / group name are LABELS only.
"""
import math
import re
from typing import Any, Literal
import secrets
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection, sql
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from pydantic import BaseModel, field_validator

from ..auth import _validated_claims, get_current_user, require_principal
from ..authz import (
    _has_admin_role,
    _has_publish_role,
    _openfga_check,
    _openfga_delete,
    _openfga_read_object_tuples,
    _openfga_write,
)
from ..db import get_db, get_owner_db
from ..geocoders import locator_names_for_layer
from ..layer_views import count_dependent_views
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["publisher"])

# Tags the audit row's client_info via the V003 trigger's app.current_client GUC.
_AUDIT_CLIENT = "portal-api"

_MAX_LABEL_LEN = 200

# GUARD 1 (release): the only layer_ids release will ever touch — pre-minted slot tokens,
# same shape layer_features.py enforces. A `vw_*` view layer carries no slot token: views
# are authored and deleted through /layer-views, never released here.
_SLOT_RE = re.compile(r"^tal_[0-9a-f]{8}$")


# --- request bodies -------------------------------------------------------------


class LayerCreate(BaseModel):
    display_name: str
    display_color: str | None = None
    # The CRS the user authored in (decision-create-wizard.md §4a). For a DEFAULT (canonical) layer this is
    # the WORKING CRS applied only at the edges; storage stays 4326. Default 4326. Existence is validated
    # against spatial_ref_sys at create time (friendly 422 before the FK would raise).
    working_srid: int = 4326
    # Opt-in NATIVE storage (decision-crs-native-slice1.md): physically store geom in working_srid so a
    # survey/high-accuracy layer round-trips BYTE-EXACT. Only valid when working_srid <> 4326 (the V052 check
    # enforces it too). Default false = the canonical-4326 path, byte-for-byte unchanged for the 80%.
    native_storage: bool = False
    # Geometry family of the layer (decision-line-polygon-layers.md). The table is created ON DEMAND
    # with geom BORN at this family (point → Point; line/polygon → Multi*; decision-slot-provisioning.md).
    # 'none' = a standalone non-spatial (hosted) table (decision-nonspatial-tables.md): the same
    # on-demand model, minus geom. A Literal enum so an invalid value is a 422 before it ever reaches
    # the provision function's frozen type allow-list.
    geom_type: Literal["point", "line", "polygon", "none"] = "point"

    @field_validator("display_name")
    @classmethod
    def _validate_label(cls, v: str) -> str:
        # A LABEL only — never used as a layer_id, table name, SQL identifier, or URL.
        v = v.strip()
        if not v:
            raise ValueError("display_name must not be empty")
        if len(v) > _MAX_LABEL_LEN:
            raise ValueError(f"display_name must be at most {_MAX_LABEL_LEN} characters")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            raise ValueError("display_name must not contain control characters")
        return v

    @field_validator("display_color")
    @classmethod
    def _validate_color(cls, v: str | None) -> str | None:
        # Optional — None means "no stored color, fall back to the frontend default".
        # When present, enforce the same #RRGGBB shape as the V021 DB CHECK.
        if v is None:
            return None
        v = v.strip()
        if not re.match(r"^#[0-9a-fA-F]{6}$", v):
            raise ValueError("display_color must be a 7-char hex color like '#1f77b4'")
        return v


# --- publisher-role gate --------------------------------------------------------


async def _require_publisher(
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> User:
    """403 unless the caller holds talus-publisher or talus-admin."""
    if not _has_publish_role(claims):
        log.info(
            "authz_rejected",
            capability="layers:create",
            reason="missing_publish_role",
            user_id=str(user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to create layers",
        )
    return user


# --- the single place all platform.layers lifecycle mutations happen ------------
#
# The create/drop helpers SET LOCAL the actor GUCs the V020 audit trigger reads, so
# every registry change lands in audit.layer_registry_audit with the right changed_by.


async def _set_audit_context(cur: Any, actor: UUID) -> None:
    """Set the transaction-local GUCs the audit trigger stamps onto the row."""
    await cur.execute("SELECT set_config('app.current_user_id', %s, true)", (str(actor),))
    await cur.execute("SELECT set_config('app.current_client', %s, true)", (_AUDIT_CLIENT,))


# --- on-demand layer ids (shared by spatial layers + non-spatial tables) --------


async def _fresh_layer_id(conn: AsyncConnection) -> str:
    """A collision-free tal_<8hex> id (2^32 space; a handful of retries is ample, and the
    platform.layers PK is the backstop). Raises a friendly 409 on the cosmic-ray path."""
    async with conn.cursor() as cur:
        for _ in range(5):
            candidate = f"tal_{secrets.token_hex(4)}"
            await cur.execute("SELECT 1 FROM platform.layers WHERE layer_id = %s", (candidate,))
            if await cur.fetchone() is None:
                return candidate
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail="could not allocate a layer id; retry"
    )


async def _create_spatial_layer(
    conn: AsyncConnection, label: str, color: str | None, working_srid: int, native: bool,
    geom_type: str, actor: UUID,
) -> dict[str, str | int | bool | None]:
    """Create one hosted SPATIAL layer ON DEMAND (decision-slot-provisioning.md — the V059 table
    model extended to geometry; the pre-minted slot pool is RETIRED, so creation can never hit a
    "no free slots" ceiling again). OpenFGA-FIRST (fail-safe, like tables/web_maps/rasters):
    write the owner tuple, then in ONE transaction provision the templated assets.<id> table
    (V061 — geom BORN at the requested family/SRID: native → working_srid, canonical → 4326; no
    post-claim retype) + insert the registry row at status='ready'. A mid-failure leaves at most
    an orphan owner tuple (harmless), never an unowned layer. Tiles need NO per-layer setup —
    the generic assets.slot_mvt function serves every layer, so pg_tileserv never restarts."""
    layer_id = await _fresh_layer_id(conn)

    # Owner grant FIRST — a layer that can't establish ownership must never exist (fail-closed).
    await _openfga_write([(f"user:{actor}", "owner", f"layer:{layer_id}")])

    storage_srid = working_srid if native else 4326
    async with conn.cursor() as cur:
        await _set_audit_context(cur, actor)
        await cur.execute(
            "SELECT platform.provision_spatial_layer(%s, %s, %s)",
            (layer_id, geom_type, storage_srid),
        )
        await cur.execute(
            "INSERT INTO platform.layers "
            "(layer_id, editable, display_name, display_color, status, geom_type, working_srid, native_storage) "
            "VALUES (%(id)s, true, %(label)s, %(color)s, 'ready', %(geom_type)s, %(srid)s, %(native)s)",
            {"id": layer_id, "label": label, "color": color, "geom_type": geom_type,
             "srid": working_srid, "native": native},
        )
    await conn.commit()
    log.info(
        "layer_created", layer_id=layer_id, actor=str(actor), geom_type=geom_type,
        working_srid=working_srid, native_storage=native,
    )
    return {
        "layer_id": layer_id,
        "display_name": label,
        "display_color": color,
        "working_srid": working_srid,
        "native_storage": native,
        "geom_type": geom_type,
        "status": "ready",
    }


# --- standalone non-spatial (hosted) tables -------------------------------------
# A "table" is a geometry-less platform.layers layer (decision-nonspatial-tables.md, Approach A),
# created ON-DEMAND exactly like spatial layers are now (V059 pioneered the model; V061 extended it
# to geometry and retired the slot pool): a fresh tal_<8hex> id + the V059 templated table (slot
# schema minus geom) + a registry row with geom_type='none'. Everything downstream (owner-fields/
# domains/constraints, sharing, feature CRUD, metadata, favorites, the grid) treats it as a layer.


async def _create_hosted_table(
    conn: AsyncConnection, label: str, color: str | None, actor: UUID
) -> dict[str, str | int | bool | None]:
    """Create one standalone non-spatial table. OpenFGA-FIRST (fail-safe, like web_map/raster): write the owner
    tuple, then in ONE transaction provision the templated assets.<id> table (V059) + insert the registry row.
    A mid-failure leaves at most an orphan owner tuple (harmless), never an unowned table."""
    layer_id = await _fresh_layer_id(conn)

    # Owner grant FIRST — a table that can't establish ownership must never exist (fail-closed; 503 on error).
    await _openfga_write([(f"user:{actor}", "owner", f"layer:{layer_id}")])

    # Physical table (scoped template DDL) + registry row in ONE transaction: a failure rolls back the CREATE,
    # leaving only the orphan owner tuple. Layers carry NO owner_id column — the OpenFGA tuple IS the owner.
    async with conn.cursor() as cur:
        await _set_audit_context(cur, actor)
        await cur.execute("SELECT platform.provision_hosted_table(%s)", (layer_id,))
        await cur.execute(
            "INSERT INTO platform.layers (layer_id, editable, display_name, display_color, status, geom_type) "
            "VALUES (%(id)s, true, %(label)s, %(color)s, 'ready', 'none')",
            {"id": layer_id, "label": label, "color": color},
        )
    await conn.commit()
    log.info("hosted_table_created", layer_id=layer_id, actor=str(actor))
    return {
        "layer_id": layer_id,
        "display_name": label,
        "display_color": color,
        "working_srid": 4326,
        "native_storage": False,
        "geom_type": "none",
        "status": "ready",
    }


async def _drop_hosted_layer(conn: AsyncConnection, layer_id: str, actor: UUID) -> int:
    """Delete a hosted layer — spatial OR table (decision-slot-provisioning.md unified delete on the
    V059 semantics): DROP the physical asset + its audit shadow, then delete the registry row + its
    metadata + owner-field defs. Every hosted layer is on-demand now (the slot pool is retired), so
    delete is truly REMOVAL — there is no pool to return anything to. Returns the row count that went
    with it. Called only after the regex + registry + authorize + delete-protection + view-dependency
    guards (so layer_id is a proven token; the identifiers are defense-in-depth, never raw input)."""
    tbl = sql.Identifier("assets", layer_id)
    audit_tbl = sql.Identifier("audit", f"{layer_id}_audit")
    async with conn.cursor() as cur:
        await _set_audit_context(cur, actor)
        await cur.execute(sql.SQL("SELECT count(*) FROM {tbl}").format(tbl=tbl))
        n = (await cur.fetchone())[0]
        await cur.execute(sql.SQL("DROP TABLE {tbl}").format(tbl=tbl))
        await cur.execute(sql.SQL("DROP TABLE {atbl}").format(atbl=audit_tbl))
        await cur.execute(
            "DELETE FROM platform.item_metadata WHERE item_type = 'layer' AND item_id = %s", (layer_id,)
        )
        await cur.execute("DELETE FROM platform.layer_fields WHERE layer_id = %s", (layer_id,))
        await cur.execute("DELETE FROM platform.layers WHERE layer_id = %s", (layer_id,))
    await conn.commit()
    return n


# --- endpoints ------------------------------------------------------------------


@router.post(
    "/layers",
    status_code=status.HTTP_201_CREATED,
    summary="Create a hosted layer (publisher only)",
)
async def create_layer(
    payload: LayerCreate,
    conn: AsyncConnection = Depends(get_owner_db),  # S-1a: provisions a table (runtime DDL) — owner pool
    user: User = Depends(_require_publisher),
) -> dict[str, str | int | bool | None]:
    actor = user.id
    label = payload.display_name
    color = payload.display_color

    # A standalone non-spatial table (decision-nonspatial-tables.md): the on-demand geometry-less path — no
    # slot claim, no CRS (aspatial). Branch BEFORE the CRS/native checks, which don't apply.
    if payload.geom_type == "none":
        return await _create_hosted_table(conn, label, color, actor)

    # Native storage only makes sense in a projected (non-4326) CRS — reject early with a friendly message
    # (the V052 check would otherwise raise a raw constraint error).
    if payload.native_storage and payload.working_srid == 4326:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="native storage requires a projected working CRS (working_srid other than 4326)",
        )

    # Friendly 422 if the picked CRS isn't a real EPSG (before the working_srid FK would raise a raw error).
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM public.spatial_ref_sys WHERE srid = %s", (payload.working_srid,)
        )
        if await cur.fetchone() is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"working_srid {payload.working_srid} is not a known coordinate system",
            )

    # On-demand creation (decision-slot-provisioning.md): no pool, no ceiling — the old
    # "no free layer slots; ask an admin to provision more" 409 is gone by design.
    return await _create_spatial_layer(
        conn, label, color, payload.working_srid, payload.native_storage, payload.geom_type, actor
    )


# Surfaced first when the CRS picker opens with no query — the place-neutral systems every deployment meets.
# (WGS 84, Web Mercator, NAD83.) A deployment's own projected zone is one search away; no region is assumed.
_COMMON_SRIDS = [4326, 3857, 4269]
# r6 (decision-talus-field-receivers.md): the frames a GNSS fix can arrive in — geographic only; the phone's
# Fix-datum picker offers these offline and searches the rest through this door.
_COMMON_GEOGRAPHIC_SRIDS = [4326, 6318, 4152, 4269, 4258, 7844, 7912]
_SRS_KIND_SQL = {"any": "", "geographic": " AND srtext LIKE 'GEOGCS%%'", "projected": " AND srtext LIKE 'PROJCS%%'"}
# Pull the human name out of the WKT (the first quoted token in srtext), e.g. "WGS 84" / "NAD83".
_SRS_NAME = "substring(srtext from '\"([^\"]+)\"')"


@router.get("/spatial-refs", summary="Search coordinate reference systems for the layer CRS picker")
async def list_spatial_refs(
    q: str = "",
    kind: Literal["any", "geographic", "projected"] = "any",
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(require_principal),
) -> dict[str, list[dict[str, Any]]]:
    """Reference lookup over PostGIS `spatial_ref_sys` for the wizard's Spatial Reference step — and (r6,
    decision-talus-field-receivers.md) the phone's Fix-datum picker. Empty query → a curated 'common' set
    (municipal defaults; the geographic frames when `kind=geographic`); otherwise match by EPSG number or name
    substring. `kind` narrows to geographic (a fix's frame can only be one) or projected systems. Read-only,
    any authenticated principal — a session OR a Basic PAT (a pack's device token), like the transform door;
    anonymous 401 (inv45)."""
    q = (q or "").strip()
    kind_sql = _SRS_KIND_SQL[kind]
    async with conn.cursor(row_factory=dict_row) as cur:
        if not q:
            await cur.execute(
                f"SELECT srid, auth_name, {_SRS_NAME} AS name FROM public.spatial_ref_sys "
                f"WHERE srid = ANY(%(ids)s){kind_sql} ORDER BY array_position(%(ids)s, srid)",
                {"ids": _COMMON_GEOGRAPHIC_SRIDS if kind == "geographic" else _COMMON_SRIDS},
            )
        else:
            await cur.execute(
                f"SELECT srid, auth_name, {_SRS_NAME} AS name FROM public.spatial_ref_sys "
                f"WHERE (srid = %(num)s::int OR {_SRS_NAME} ILIKE %(like)s){kind_sql} "
                "ORDER BY srid LIMIT 50",
                {"num": int(q) if q.isdigit() else None, "like": f"%{q}%"},
            )
        rows = await cur.fetchall()
    return {"results": [
        {"srid": r["srid"], "authName": r["auth_name"], "name": r["name"] or f"{r['auth_name']}:{r['srid']}"}
        for r in rows
    ]}


@router.get("/transform-point", summary="Transform a point between coordinate systems (map readout + working-CRS entry)")
async def transform_point(
    lon: float,
    lat: float,
    srid: int,
    from_srid: int = 4326,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(require_principal),
) -> dict[str, float | int]:
    """Reproject a point from `from_srid` (the ordinates `lon`/`lat` are in it) into `srid` via PostGIS
    ST_Transform. Three uses: E2 readout (`from_srid=4326` → State Plane/UTM, decision-whats-here.md), E3
    working-CRS entry (`from_srid=<working>`, `srid=4326` → store; native click 4326→native CRS,
    decision-coord-entry.md), and Talus Field's drain (`toNative`: a queued lon/lat on a native-CRS layer →
    the layer's x/y at sync). Datum corrections (NAD27, original NAD83) use the files in
    api/datum_grids, which the database mounts; exact survey values are TYPED native and never transformed.
    A point outside the target system's projection domain is the caller's error: 422. Any
    authenticated principal — a session Bearer OR a personal access token as Basic (a desktop PAT, a pack's
    device token: the only credential a crew phone holds once its session lapses); anonymous 401 (inv45).
    It serves two numbers and no data, so no view gate. 422 for an unknown srid."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT srid FROM public.spatial_ref_sys WHERE srid = ANY(%s)", ([srid, from_srid],)
        )
        known = {r[0] for r in await cur.fetchall()}
        for s in (srid, from_srid):
            if s not in known:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"srid {s} is not a known coordinate system",
                )
        try:
            await cur.execute(
                "SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), %s), %s) AS p) s",
                (lon, lat, from_srid, srid),
            )
            row = await cur.fetchone()
        except pg_errors.InternalError_:
            # The coordinate library refuses a point outside the target system's projection domain
            # ("Point outside of projection domain"). That is a property of the caller's point.
            await conn.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"these coordinates are outside the area that coordinate system EPSG:{srid} covers",
            )
    if row is None or row[0] is None or row[1] is None or not (math.isfinite(row[0]) and math.isfinite(row[1])):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"these coordinates are outside the area that coordinate system EPSG:{srid} covers",
        )
    return {"x": row[0], "y": row[1], "srid": srid}


@router.delete(
    "/layers/{layer_id}",
    summary="Delete a hosted layer (owner or admin)",
)
async def release_layer(
    layer_id: str,
    conn: AsyncConnection = Depends(get_owner_db),  # S-1a: DROPs the table + audit shadow (DDL) — owner pool
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, str | int]:
    # GUARDS (same family as layer_features.py). The registry SELECT runs FIRST and binds layer_id
    # as a parameter (never interpolated), so it's safe for any id; the regex still gates the DROP
    # below (which DOES name a table) — layer_id is never used to name a table before that check.
    #   1. registry row exists, else 404 (genuinely unknown id).
    #   2. regex: a slot token. The only REGISTERED non-slots are `vw_*` view layers, which
    #      have their own delete door (/layer-views) — a clear 409 pointing there, not a bare 404.
    #   3. status == 'ready' (claimed), else 409.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, delete_protected FROM platform.layers WHERE layer_id = %s", (layer_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    if not _SLOT_RE.fullmatch(layer_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This is a view layer — delete it via DELETE /layer-views/{id}.",
        )
    if row[0] != "ready":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="layer not claimed")
    delete_protected = bool(row[1])

    actor = user.id

    # AUTHORIZE (after guards): owner OR talus-admin, else 403. The caller has already
    # passed the normal request gate (valid token + platform.users row, via
    # get_current_user). The admin path is role-ONLY and must NOT consult OpenFGA —
    # checked first so it short-circuits the FGA call. It is also the recovery path for
    # the half-released state ('ready' with zero tuples), so it has to authorize when no
    # tuples exist. The owner path checks the `owner` relation SPECIFICALLY — not can_view
    # / can_edit_access, so a shared editor cannot release — and _openfga_check fails
    # CLOSED (503) on an FGA exception, per this module's convention.
    if _has_admin_role(claims):
        auth_path = "admin"
    elif await _openfga_check(actor, "owner", layer_id):
        auth_path = "owner"
    else:
        log.info(
            "authz_rejected",
            capability=f"{layer_id}:release",
            reason="not_owner_not_admin",
            user_id=str(actor),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to release this layer",
        )

    # Delete protection (Talus 24): a protected layer can't be released by ANYONE — owner OR admin —
    # until protection is turned off (ABSOLUTE; AGO-faithful, guards human error
    # incl. an admin's). No admin override; an admin still has the toggle, so it's a deliberate
    # two-step (untick → release), never a one-click force. Checked AFTER authorize — only someone
    # already permitted to release hits this guard-rail; the message is actionable. Releasing wipes the
    # slot's features, so this is exactly the accidental-loss the flag guards against.
    if delete_protected:
        log.info(
            "layer_release_blocked", layer_id=layer_id, actor=str(actor), reason="delete_protected",
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This layer is protected from deletion. Turn off delete protection "
            "(Manage → Delete protection) before releasing it.",
        )

    # View-dependency guard (Talus 38, F-lifecycle). A view layer is a PG view over this layer's
    # assets.<id> table; deleting the layer DROPs that table out from under the view. The
    # platform.layer_views FK (ON DELETE RESTRICT) would also block the registry DELETE, but with a
    # raw constraint error — this explicit guard is the friendly, actionable message. Checked after
    # authorize, like delete_protected, so only a permitted deleter sees it.
    dependents = await count_dependent_views(conn, layer_id)
    if dependents:
        log.info(
            "layer_release_blocked", layer_id=layer_id, actor=str(actor),
            reason="view_dependents", dependents=dependents,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{dependents} view layer(s) depend on this layer. Delete them first, "
            "then release this layer.",
        )

    # Locator-dependency guard (Mode B, decision-own-data-geocoder.md §3 F6 — same shape as the
    # view guard above). An internal geocoder searches this layer's address points; deleting the
    # layer would silently break the org's search box. The V066 FK (no cascade) would also block
    # the registry DELETE, but with a raw constraint error — this is the friendly, actionable
    # message in front of that belt.
    locators = await locator_names_for_layer(conn, layer_id)
    if locators:
        log.info(
            "layer_release_blocked", layer_id=layer_id, actor=str(actor),
            reason="geocoder_dependents", locators=locators,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Locator(s) {', '.join(repr(n) for n in locators)} search this layer's "
            "addresses. Remove them in Admin → Geocoders first, then delete this layer.",
        )

    # OpenFGA FIRST, then Postgres — deliberately inverts the claim's Postgres-first order
    # (decision-11 §Q3). FGA-first fails SAFE: a crash after tuple delete but before the
    # registry flip leaves a tuple-less 'ready' slot that an admin re-runs to finish.
    # Postgres-first could instead leave a stale `owner` tuple that silently grants the
    # previous owner access once the slot is re-claimed. Do NOT "fix" this asymmetry.
    # Delete ALL tuples on the object (owner AND any org_shared/shared_group), not just
    # owner — §Q4.
    tuples = await _openfga_read_object_tuples(f"layer:{layer_id}")
    await _openfga_delete(tuples)

    # Unified delete (decision-slot-provisioning.md): every hosted layer — spatial or table — is
    # on-demand, so delete DROPs it outright. The old return-to-pool path is retired with the pool.
    features_deleted = await _drop_hosted_layer(conn, layer_id, actor)
    log.info(
        "layer_deleted",
        layer_id=layer_id,
        actor=str(actor),
        auth_path=auth_path,
        tuples_deleted=len(tuples),
        features_deleted=features_deleted,
    )
    return {"layer_id": layer_id, "status": "deleted", "features_deleted": features_deleted}


# NOTE: POST /groups (create_group) moved to routers/groups.py — the groups domain owns its own
# lifecycle (Groups management UI, decision-groups-ui.md). The path is unchanged.
