"""Talus 31 — item metadata (ISO 19115-cored, required-core profile).

A documented metadata record per catalog item (layer / web_map / future raster), stored in the
type-agnostic `platform.item_metadata` table (V033) as JSON shaped to ISO 19115 elements (so a later
slice can serialize it to ISO 19139/19115-3, QGIS `.qmd`, and DCAT-US without re-modeling). This slice
ships the **model + editor backend + a completeness validator** ONLY — no exporters.

  - GET /items/{item_type}/{item_id}/metadata — view-gated read (doc + completeness + canManage).
  - PUT /items/{item_type}/{item_id}/metadata — owner-or-admin write (validates the core, AUTO-DERIVES
    the spatial extent from the layer's geometry, upserts).
  - metadata_complete(conn, item_type, item_id) — the (complete, missing[]) helper the Talus 32 Open
    Data gate consumes ("can't publish to Open Data until the required core is filled").

Required-core (the "minimum for discovery" common to ISO-core / INSPIRE-minimum / DCAT-required / QGIS):
title, abstract, keywords, ISO topicCategory, license, point-of-contact, language. Spatial extent is
auto-derived from geometry (never typed) and required only for a geometry layer that actually has one.
The completeness gate is consumed ONLY at the public (Open Data) boundary — ordinary internal sharing is
never burdened by it.
"""
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, field_validator

from ..auth import _optional_claims, _validated_claims, get_current_user, get_optional_user
from ..authz import (
    _has_admin_role,
    _openfga_check,
    manage_allowed,
    public_object_allowed,
    view_allowed,
)
from ..config import settings
from ..db import get_db
from ..metadata_serializers import to_iso19139, to_qmd
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["metadata"])

# The catalog item types this slice documents. The table + this router are type-agnostic; 'raster'
# joined with the raster store-&-serve slice (V045+).
_ITEM_TYPES = frozenset({"layer", "web_map", "raster"})

# ISO 19115 MD_TopicCategoryCode — the fixed 19-value enumeration (a dropdown, not free text).
_TOPIC_CATEGORIES = frozenset({
    "farming", "biota", "boundaries", "climatologyMeteorologyAtmosphere", "economy", "elevation",
    "environment", "geoscientificInformation", "health", "imageryBaseMapsEarthCover",
    "intelligenceMilitary", "inlandWaters", "location", "oceans", "planningCadastre", "society",
    "structure", "transportation", "utilitiesCommunication",
})

# The required-core keys the Open Data gate enforces. spatialExtent is handled separately (auto-derived,
# required only for a geometry layer that has one).
_REQUIRED_CORE = ("title", "abstract", "keywords", "topicCategory", "license", "pointOfContact", "language")


# --- wire models ----------------------------------------------------------------


class MetadataContact(BaseModel):
    """A point-of-contact (ISO CI_Responsibility). `role` is an ISO CI_RoleCode (e.g. pointOfContact)."""
    name: str | None = None
    organization: str | None = None
    email: str | None = None
    role: str | None = None


class GeoBoundingBox(BaseModel):
    """EX_GeographicBoundingBox, EPSG:4326. Server-derived from geometry — not user-entered."""
    west: float
    south: float
    east: float
    north: float


class MetadataDoc(BaseModel):
    """The ISO-19115-cored record. All fields optional at WRITE time (you may save a draft); the
    required-core is enforced separately at the Open Data boundary (metadata_complete)."""
    title: str | None = None
    abstract: str | None = None
    keywords: list[str] = []
    topicCategory: str | None = None
    license: str | None = None
    pointOfContact: MetadataContact | None = None
    language: str = "en"
    spatialExtent: GeoBoundingBox | None = None  # server-managed (auto-derived); client value ignored

    @field_validator("topicCategory")
    @classmethod
    def _valid_topic(cls, v: str | None) -> str | None:
        if v is not None and v != "" and v not in _TOPIC_CATEGORIES:
            raise ValueError("topicCategory must be one of the ISO MD_TopicCategoryCode values")
        return v


class MetadataResponse(BaseModel):
    itemType: str
    itemId: str
    doc: MetadataDoc
    complete: bool
    missing: list[str]
    canManage: bool
    updatedAt: datetime | None = None


# --- guards / authorization -----------------------------------------------------


def _check_item_type(item_type: str) -> None:
    if item_type not in _ITEM_TYPES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown item type '{item_type}'",
        )


async def _require_item_exists(conn: AsyncConnection, item_type: str, item_id: str) -> None:
    """404 unless the layer/web_map/raster row exists (binds item_id as a parameter). NOTE: rasters
    were declared in _ITEM_TYPES from day one but this helper fell through to the web_maps table
    for them, so raster metadata 404'd — wired for real by the raster-public slice (whose Open
    Data curation needs the T31 gate to be satisfiable for rasters)."""
    if item_type == "layer":
        q, want = "SELECT 1 FROM platform.layers WHERE layer_id = %s", item_id
    elif item_type == "raster":
        q, want = "SELECT 1 FROM platform.rasters WHERE id = %s::uuid", item_id
    else:  # web_map
        q, want = "SELECT 1 FROM platform.web_maps WHERE id = %s", item_id
    async with conn.cursor() as cur:
        await cur.execute(q, (want,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item not found")


async def _authorize_view(claims: dict[str, Any] | None, user: User, item_type: str, item_id: str) -> bool:
    """Read gate: the caller must be able to VIEW the item (anyone who can see it may read its
    metadata). Layer -> view_allowed; web_map/raster -> can_view on the right object type. Stub
    mode authorizes (dev)."""
    if settings.auth_mode == "stub":
        return True
    if item_type == "layer":
        return await view_allowed(user, item_id)
    return await _openfga_check(
        user.id, "can_view", item_id,
        object_type="raster" if item_type == "raster" else "web_map",
    )


async def _authorize_manage(claims: dict[str, Any] | None, user: User, item_type: str, item_id: str) -> bool:
    """Write gate: owner-OR-admin (ownerless system layers are admin-only) — the same gate as
    sharing/symbology. Layer -> manage_allowed; web_map/raster -> admin OR owner on the right
    object type. Stub mode authorizes."""
    if settings.auth_mode == "stub":
        return True
    if item_type == "layer":
        return await manage_allowed(claims, user, item_id)
    if _has_admin_role(claims):
        return True
    return await _openfga_check(
        user.id, "owner", item_id,
        object_type="raster" if item_type == "raster" else "web_map",
    )


# --- extent auto-derivation -----------------------------------------------------


async def _layer_has_geom(conn: AsyncConnection, layer_id: str) -> bool:
    """True iff assets.<layer_id> has a `geom` column (layer_id is a proven registry row)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'assets' AND table_name = %s AND column_name = 'geom'",
            (layer_id,),
        )
        return await cur.fetchone() is not None


async def _derive_extent(conn: AsyncConnection, layer_id: str) -> GeoBoundingBox | None:
    """The EPSG:4326 bounding box of assets.<layer_id> via PostGIS ST_Extent — the auto-derived
    spatial extent (the user never types it). None when the layer has no geometry column or no rows
    (an empty layer has no extent). Fail-soft: any error -> None (never fails the save). The table
    identifier is built from the already-proven layer_id (defense-in-depth, never raw input)."""
    if not await _layer_has_geom(conn, layer_id):
        return None
    tbl = sql.Identifier("assets", layer_id)
    q = sql.SQL(
        "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) "
        "FROM (SELECT ST_Extent(geom) AS e FROM {tbl}) s"
    ).format(tbl=tbl)
    try:
        async with conn.cursor() as cur:
            await cur.execute(q)
            row = await cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — extent is best-effort; never block the save
        log.warning("metadata_extent_derive_failed", layer_id=layer_id, detail=str(exc))
        return None
    if not row or row[0] is None:
        return None
    return GeoBoundingBox(west=row[0], south=row[1], east=row[2], north=row[3])


async def _derive_raster_extent(conn: AsyncConnection, raster_id: str) -> GeoBoundingBox | None:
    """The stored EPSG:4326 footprint from platform.rasters.extent (written at ingest) — the raster
    twin of _derive_extent. Fail-soft: any error/absent extent -> None (never blocks the save)."""
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT ST_XMin(extent), ST_YMin(extent), ST_XMax(extent), ST_YMax(extent) "
                "FROM platform.rasters WHERE id = %s::uuid AND extent IS NOT NULL",
                (raster_id,),
            )
            row = await cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — extent is best-effort
        log.warning("metadata_extent_derive_failed", raster_id=raster_id, detail=str(exc))
        return None
    if not row or row[0] is None:
        return None
    return GeoBoundingBox(west=row[0], south=row[1], east=row[2], north=row[3])


# --- completeness (the Talus 32 Open Data gate dependency) ----------------------


def _present(value: Any) -> bool:
    """A required field is 'present' iff non-empty: str -> non-blank; list -> non-empty; contact ->
    a non-blank name; anything else -> truthy."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, list):
        return len(value) > 0
    if isinstance(value, dict):  # pointOfContact: require at least a name
        return bool((value.get("name") or "").strip())
    return bool(value)


async def _read_doc(conn: AsyncConnection, item_type: str, item_id: str) -> tuple[dict[str, Any], datetime | None]:
    """The stored metadata doc (or {} if none) + updated_at."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT doc, updated_at FROM platform.item_metadata WHERE item_type = %s AND item_id = %s",
            (item_type, item_id),
        )
        row = await cur.fetchone()
    if row is None:
        return {}, None
    return (row["doc"] or {}), row["updated_at"]


async def metadata_complete(
    conn: AsyncConnection, item_type: str, item_id: str
) -> tuple[bool, list[str]]:
    """Is the item's metadata complete enough to publish? Returns (complete, missing[]). The
    required-core must all be present; spatialExtent is required only for a geometry layer that has a
    derivable extent (an empty/geometry-less item is satisfied without one). This is the gate the
    Talus 33 Open Data curate-action consumes."""
    doc, _updated = await _read_doc(conn, item_type, item_id)
    missing = [f for f in _REQUIRED_CORE if not _present(doc.get(f))]
    # spatialExtent: required only when the layer HAS a derivable extent but the doc lacks it (it's
    # auto-derived on save, so a documented geom layer will carry it). Empty/geometry-less -> satisfied.
    if item_type == "layer":
        derived = await _derive_extent(conn, item_id)
        if derived is not None and not doc.get("spatialExtent"):
            missing.append("spatialExtent")
    return (len(missing) == 0), missing


# --- endpoints ------------------------------------------------------------------


@router.get(
    "/items/{item_type}/{item_id}/metadata",
    summary="Read an item's metadata record (+ completeness) — view-gated",
)
async def get_metadata(
    item_type: str,
    item_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> MetadataResponse:
    _check_item_type(item_type)
    await _require_item_exists(conn, item_type, item_id)
    # Read is allowed to anyone who may VIEW the item OR MANAGE it — manage (admin/owner) implies a
    # right to read, so an admin who lacks can_view can still read/round-trip metadata they may edit.
    # Check manage first: for an admin it short-circuits true with no OpenFGA round-trip.
    can_manage = await _authorize_manage(claims, user, item_type, item_id)
    if not can_manage and not await _authorize_view(claims, user, item_type, item_id):
        log.info(
            "authz_rejected", capability="metadata:view",
            reason="no_view_access", user_id=str(user.id), item=f"{item_type}:{item_id}",
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to view this item")
    doc, updated_at = await _read_doc(conn, item_type, item_id)
    complete, missing = await metadata_complete(conn, item_type, item_id)
    return MetadataResponse(
        itemType=item_type, itemId=item_id, doc=MetadataDoc(**doc),
        complete=complete, missing=missing,
        canManage=can_manage,
        updatedAt=updated_at,
    )


@router.put(
    "/items/{item_type}/{item_id}/metadata",
    summary="Write an item's metadata record (owner or admin)",
)
async def put_metadata(
    item_type: str,
    item_id: str,
    payload: MetadataDoc,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> MetadataResponse:
    _check_item_type(item_type)
    await _require_item_exists(conn, item_type, item_id)
    if not await _authorize_manage(claims, user, item_type, item_id):
        log.info(
            "authz_rejected", capability="metadata:manage",
            reason="not_owner_not_admin", user_id=str(user.id), item=f"{item_type}:{item_id}",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to edit this item's metadata",
        )

    # Spatial extent is SERVER-authoritative: ignore any client value and derive from geometry —
    # layers from assets.<id>; rasters from platform.rasters.extent (raster-public slice; feeds the
    # DCAT `spatial` field). Maps derive from member layers in a later slice. Best-effort.
    if item_type == "layer":
        payload.spatialExtent = await _derive_extent(conn, item_id)
    elif item_type == "raster":
        payload.spatialExtent = await _derive_raster_extent(conn, item_id)
    else:
        payload.spatialExtent = None

    doc = payload.model_dump()
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO platform.item_metadata (item_type, item_id, doc, updated_at, updated_by) "
            "VALUES (%s, %s, %s, now(), %s) "
            "ON CONFLICT (item_type, item_id) DO UPDATE "
            "SET doc = EXCLUDED.doc, updated_at = now(), updated_by = EXCLUDED.updated_by",
            (item_type, item_id, Jsonb(doc), user.id),
        )
    await conn.commit()
    log.info("metadata_saved", item=f"{item_type}:{item_id}", actor=str(user.id))

    complete, missing = await metadata_complete(conn, item_type, item_id)
    return MetadataResponse(
        itemType=item_type, itemId=item_id, doc=payload,
        complete=complete, missing=missing, canManage=True,
        updatedAt=datetime.now().astimezone(),
    )


# --- metadata export (Talus 33 c7) — QGIS .qmd / ISO 19139 ----------------------


async def _authorize_metadata_read(
    conn: AsyncConnection, claims: dict[str, Any] | None, principal: User | None,
    item_type: str, item_id: str,
) -> None:
    """Optional-auth gate for the metadata DOWNLOAD (the open-standards export). AUTHENTICATED → may
    view OR manage the item (403 else); ANONYMOUS → the item must be `public_shared` (404 else —
    existence-hiding, the T23 pattern). Lets the public download a public dataset's metadata alongside
    its data, while staff export anything they can see. Both branches fail closed (503 on OpenFGA)."""
    if principal is not None:
        if await _authorize_manage(claims, principal, item_type, item_id) or await _authorize_view(
            claims, principal, item_type, item_id
        ):
            return
        log.info(
            "authz_rejected", capability="metadata:export",
            reason="no_view_access", user_id=str(principal.id), item=f"{item_type}:{item_id}",
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to view this item")
    if not await public_object_allowed(conn, item_id, object_type=item_type):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


# format -> (serializer-key, download extension). XML for both; QGIS reads .qmd, harvesters read 19139.
_EXPORT_EXT = {"qmd": "qmd", "iso19139": "iso19139.xml"}


@router.get(
    "/items/{item_type}/{item_id}/metadata/download",
    summary="Download an item's metadata as QGIS .qmd or ISO 19139 XML — public-capable",
)
async def download_metadata(
    item_type: str,
    item_id: str,
    format: str = Query(..., pattern="^(qmd|iso19139)$", description="qmd | iso19139"),
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_optional_claims),
    principal: User | None = Depends(get_optional_user),
) -> Response:
    """Serialize the stored ISO-19115-cored record to an open standard (pure `doc -> XML`). Optional-
    auth: a PUBLIC dataset's metadata is anon-downloadable (the open-data promise); staff export any
    item they may view. The DCAT-US feed (open_data router) is the third serializer off the same doc."""
    _check_item_type(item_type)
    await _require_item_exists(conn, item_type, item_id)
    await _authorize_metadata_read(conn, claims, principal, item_type, item_id)

    doc, updated_at = await _read_doc(conn, item_type, item_id)
    stamp = (updated_at or datetime.now(timezone.utc)).date().isoformat()
    body = to_qmd(doc, item_id) if format == "qmd" else to_iso19139(doc, item_id, stamp)
    return Response(
        content=body,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{item_id}.{_EXPORT_EXT[format]}"'},
    )
