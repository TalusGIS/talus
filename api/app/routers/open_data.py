"""Talus 33 — Open Data curation: the governed, audited "publish to Open Data" action.

Curating a layer into Open Data is ONE deliberate action that, in a single confirmed step:
  1. **Metadata gate (T31)** — `metadata_complete('layer', id)` must pass, else **422** with the
     missing fields. Open data without metadata is bad open data; this gate is enforced ONLY here,
     never on ordinary internal sharing.
  2. **Make public (T23)** — runs the governance gate (`assert_may_share_public`: the F6 deploy
     switch + the non-admin `nonadmin_can_share_public` policy) and writes the `public_shared` tuple
     as an audited public transition (`set_object_public`).
  3. **Curate** — adds the `shared_group -> group:open_data` tuple (preserving any existing shares).

The public catalog (c2) = layers curated into `group:open_data`, filtered to what the caller may
see (anon -> public_shared only). NO new authz: this reuses the generic member-scoped share gate
(admins always; non-admins must be `group:open_data` members), the T23 public-write primitives, and
the T31 metadata validator. The `PUBLIC_SYSTEM_GROUPS` invariant (in the generic share endpoints)
guarantees a curated layer is public; this endpoint is the metadata-gated, audited front door.

**Un-curate** drops the layer from `group:open_data` (the inverse) — it deliberately does NOT
un-publish; reducing public exposure stays the layer's own share endpoint's job (only-reduces-
exposure, always allowed for owner/admin), so an editor can pull a dataset from the catalog without
having to remember to also flip it private, and vice-versa.
"""
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from pydantic import BaseModel

from .. import admin_audit
from ..auth import _validated_claims, get_current_user, get_optional_user
from ..authz import (
    _has_admin_role,
    _objects_shared_with_group,
    _openfga_check,
    _openfga_read_user_groups,
    assert_may_share_public,
    get_object_shares,
    get_object_shares_and_owner,
    manage_allowed,
    set_object_public,
    set_object_shares,
)
from ..config import settings
from ..geom_meta import geom_meta
from ..db import get_db
from ..models.user import User
from ..public_sharing import public_sharing_enabled
from .metadata import metadata_complete

log = structlog.get_logger(__name__)

router = APIRouter(tags=["open-data"])

_OPEN_DATA_GROUP = "open_data"


class CurationStatus(BaseModel):
    """A layer's Open Data curation state + what it would take to curate it — drives the catalog
    Manage dialog (the server is the authority; this just shapes the UI)."""

    curated: bool          # currently shared to group:open_data
    public: bool           # currently public_shared (curation implies this)
    metadataComplete: bool  # passes the T31 publish gate
    missing: list[str]     # required metadata fields still missing (empty when complete)
    canManage: bool        # caller may manage this layer (owner or admin)
    canCurate: bool        # caller may curate (manages it AND is admin or open_data member)
    publicSharePermitted: bool  # caller may turn it public (admin, or org policy allows non-admins)


async def _require_registered(conn: AsyncConnection, layer_id: str) -> None:
    """layer_id must be a row in platform.layers, else 404 (binds layer_id as a parameter)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.layers WHERE layer_id = %s", (layer_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")


async def _may_curate(claims: dict[str, Any] | None, user: User) -> bool:
    """May this caller curate into Open Data? Admins always; everyone else must be a member of
    `group:open_data` (the same member-scope rule the generic share path enforces, so no new
    authz). `_openfga_read_user_groups` fails closed (503)."""
    if _has_admin_role(claims):
        return True
    return _OPEN_DATA_GROUP in set(await _openfga_read_user_groups(user.id))


async def _may_publish_public(conn: AsyncConnection, claims: dict[str, Any] | None) -> bool:
    """Non-raising mirror of `assert_may_share_public`, for the curate-readiness hint: may this caller
    turn an item public? The F6 deploy switch must be on; admins always pass; a non-admin needs the
    org `nonadmin_can_share_public` policy (fail-safe False on any read error). Distinct from
    `_may_curate` so the UI can tell "not an Open Data member" apart from "your org forbids non-admin
    public sharing" — the curate action itself still 403s loudly via `assert_may_share_public`."""
    if not await public_sharing_enabled(conn):
        return False
    if _has_admin_role(claims):
        return True
    from ..org_settings import read_org_settings  # lazy: keep org_settings a leaf (no import cycle)

    try:
        return bool((await read_org_settings(conn)).nonadminCanSharePublic)
    except Exception:  # noqa: BLE001 — fail-safe: deny on any policy-read error
        return False


async def _status(
    conn: AsyncConnection, claims: dict[str, Any] | None, user: User, layer_id: str
) -> CurationStatus:
    """Assemble the curation status for a layer the caller manages."""
    complete, missing = await metadata_complete(conn, "layer", layer_id)
    shares = await get_object_shares(f"layer:{layer_id}")
    return CurationStatus(
        curated=_OPEN_DATA_GROUP in shares["groups"],
        public=shares["publicAccess"],
        metadataComplete=complete,
        missing=missing,
        canManage=True,
        canCurate=await _may_curate(claims, user),
        publicSharePermitted=await _may_publish_public(conn, claims),
    )


@router.get(
    "/open-data/layers/{layer_id}/curation",
    summary="A layer's Open Data curation state + what it would take to curate it (owner/admin)",
)
async def get_curation(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> CurationStatus:
    await _require_registered(conn, layer_id)
    # Management info (publish-readiness, missing metadata) — owner-or-admin only, like the share
    # dialog it backs. An ownerless system layer is therefore admin-only.
    if not await manage_allowed(claims, user, layer_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to manage this layer",
        )
    return await _status(conn, claims, user, layer_id)


@router.post(
    "/open-data/layers/{layer_id}/curate",
    summary="Curate a layer into Open Data: metadata-gate -> make public (audited) -> share to group",
)
async def curate_layer(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> CurationStatus:
    await _require_registered(conn, layer_id)
    # AUTHZ — two gates, both required: (1) manage the layer (owner-or-admin; ownerless => admin),
    # (2) be allowed to curate into Open Data (admin, or a group:open_data member). Mirrors the
    # generic share path: manage to touch the item + member-scope for the reserved group.
    if not await manage_allowed(claims, user, layer_id):
        log.info(
            "authz_rejected", capability="open_data:curate",
            reason="not_owner_not_admin", user_id=str(user.id), layer=layer_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to manage this layer",
        )
    if not await _may_curate(claims, user):
        log.info(
            "authz_rejected", capability="open_data:curate",
            reason="not_open_data_member", user_id=str(user.id), layer=layer_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to curate into Open Data (membership required)",
        )

    # 1) METADATA GATE (T31) — open-data-specific. Incomplete metadata blocks the publish (422 with
    # the missing fields), so the UI can route the publisher to "complete metadata first".
    complete, missing = await metadata_complete(conn, "layer", layer_id)
    if not complete:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "metadata_incomplete", "missing": missing},
        )

    # 2) MAKE PUBLIC (T23) — the governance gate (F6 switch + non-admin policy) then the audited
    # public transition. assert_may_share_public fails CLOSED; set_object_public is idempotent and
    # reports whether the public state actually flipped (so we only audit a real transition).
    await assert_may_share_public(conn, claims)
    public_changed = await set_object_public(f"layer:{layer_id}", public=True)

    # 3) CURATE — add open_data to the layer's group set (union, preserving existing org/group
    # shares; set_object_shares diffs so a re-curate is a no-op). Never touches owner/public_shared.
    shares = await get_object_shares(f"layer:{layer_id}")
    groups = sorted(set(shares["groups"]) | {_OPEN_DATA_GROUP})
    await set_object_shares(
        f"layer:{layer_id}", organization_only=shares["organizationOnly"], groups=groups
    )

    if public_changed:
        # The loud, distinct audit line for the irreversible-exposure transition (decision §F3) —
        # same event the share endpoints emit, so the audit trail is uniform across publish paths.
        log.info("object_public_changed", object=f"layer:{layer_id}", actor=str(user.id), public=True)
    log.info("open_data_curated", layer=layer_id, actor=str(user.id))
    await admin_audit.record(
        conn, actor_id=user.id, action="open_data_curated", target_type="layer", target_id=layer_id,
        detail={"publicChanged": public_changed},
    )
    return await _status(conn, claims, user, layer_id)


@router.delete(
    "/open-data/layers/{layer_id}/curate",
    summary="Un-curate a layer from Open Data (drops it from the catalog; does NOT un-publish)",
)
async def uncurate_layer(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> CurationStatus:
    await _require_registered(conn, layer_id)
    # Un-curate only REDUCES exposure (drops from the public catalog), so — like un-publish — it is
    # allowed for any manager (owner-or-admin); no Open Data membership required to pull back.
    if not await manage_allowed(claims, user, layer_id):
        log.info(
            "authz_rejected", capability="open_data:uncurate",
            reason="not_owner_not_admin", user_id=str(user.id), layer=layer_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to manage this layer",
        )
    shares = await get_object_shares(f"layer:{layer_id}")
    groups = sorted(set(shares["groups"]) - {_OPEN_DATA_GROUP})
    await set_object_shares(
        f"layer:{layer_id}", organization_only=shares["organizationOnly"], groups=groups
    )
    log.info("open_data_uncurated", layer=layer_id, actor=str(user.id))
    await admin_audit.record(
        conn, actor_id=user.id, action="open_data_uncurated", target_type="layer", target_id=layer_id,
    )
    return await _status(conn, claims, user, layer_id)


# --- raster curation (raster-public slice, decision-raster-public-sharing.md) ----
#
# The SAME three-step publish action for rasters — metadata gate -> make public (audited) ->
# `shared_group -> group:open_data` tuple (raster.shared_group is modeled, V045). Manage authority
# for a raster = admin or OpenFGA owner (rasters have no manage_allowed helper — it's layer-typed).


async def _require_raster(conn: AsyncConnection, raster_id: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.rasters WHERE id = %s::uuid", (raster_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="raster not found")


async def _raster_manage(claims: dict[str, Any] | None, user: User, raster_id: str) -> bool:
    if _has_admin_role(claims):
        return True
    return await _openfga_check(user.id, "owner", raster_id, object_type="raster")


async def _raster_status(
    conn: AsyncConnection, claims: dict[str, Any] | None, user: User, raster_id: str
) -> CurationStatus:
    complete, missing = await metadata_complete(conn, "raster", raster_id)
    shares = await get_object_shares(f"raster:{raster_id}")
    return CurationStatus(
        curated=_OPEN_DATA_GROUP in shares["groups"],
        public=shares["publicAccess"],
        metadataComplete=complete,
        missing=missing,
        canManage=True,
        canCurate=await _may_curate(claims, user),
        publicSharePermitted=await _may_publish_public(conn, claims),
    )


@router.get(
    "/open-data/rasters/{raster_id}/curation",
    summary="A raster's Open Data curation state + what it would take to curate it (owner/admin)",
)
async def get_raster_curation(
    raster_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> CurationStatus:
    await _require_raster(conn, raster_id)
    if not await _raster_manage(claims, user, raster_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="not authorized to manage this raster")
    return await _raster_status(conn, claims, user, raster_id)


@router.post(
    "/open-data/rasters/{raster_id}/curate",
    summary="Curate a raster into Open Data: metadata-gate -> make public (audited) -> share to group",
)
async def curate_raster(
    raster_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> CurationStatus:
    await _require_raster(conn, raster_id)
    if not await _raster_manage(claims, user, raster_id):
        log.info("authz_rejected", capability="open_data:curate", reason="not_owner_not_admin",
                 user_id=str(user.id), raster=raster_id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="not authorized to manage this raster")
    if not await _may_curate(claims, user):
        log.info("authz_rejected", capability="open_data:curate", reason="not_open_data_member",
                 user_id=str(user.id), raster=raster_id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="not authorized to curate into Open Data (membership required)")
    complete, missing = await metadata_complete(conn, "raster", raster_id)
    if not complete:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "metadata_incomplete", "missing": missing},
        )
    await assert_may_share_public(conn, claims)
    public_changed = await set_object_public(f"raster:{raster_id}", public=True)
    shares = await get_object_shares(f"raster:{raster_id}")
    groups = sorted(set(shares["groups"]) | {_OPEN_DATA_GROUP})
    await set_object_shares(
        f"raster:{raster_id}", organization_only=shares["organizationOnly"], groups=groups
    )
    if public_changed:
        log.info("object_public_changed", object=f"raster:{raster_id}", actor=str(user.id), public=True)
    log.info("open_data_curated", raster=raster_id, actor=str(user.id))
    await admin_audit.record(
        conn, actor_id=user.id, action="open_data_curated", target_type="raster", target_id=raster_id,
        detail={"publicChanged": public_changed},
    )
    return await _raster_status(conn, claims, user, raster_id)


@router.delete(
    "/open-data/rasters/{raster_id}/curate",
    summary="Un-curate a raster from Open Data (drops it from the catalog; does NOT un-publish)",
)
async def uncurate_raster(
    raster_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> CurationStatus:
    await _require_raster(conn, raster_id)
    if not await _raster_manage(claims, user, raster_id):
        log.info("authz_rejected", capability="open_data:uncurate", reason="not_owner_not_admin",
                 user_id=str(user.id), raster=raster_id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="not authorized to manage this raster")
    shares = await get_object_shares(f"raster:{raster_id}")
    groups = sorted(set(shares["groups"]) - {_OPEN_DATA_GROUP})
    await set_object_shares(
        f"raster:{raster_id}", organization_only=shares["organizationOnly"], groups=groups
    )
    log.info("open_data_uncurated", raster=raster_id, actor=str(user.id))
    await admin_audit.record(
        conn, actor_id=user.id, action="open_data_uncurated", target_type="raster", target_id=raster_id,
    )
    return await _raster_status(conn, claims, user, raster_id)


# --- public catalog read surface (c2) -------------------------------------------
#
# The PUBLIC read view over the curated set: GET /open-data/datasets (the catalog) and
# GET /open-data/datasets/{id} (one dataset's detail). Optional-auth + F6-gated for anon (mirrors
# /home/featured): an anon caller sees ONLY public_shared curated layers; signed-in staff see
# anything curated they can_view. Fail-SOFT — an OpenFGA hiccup degrades to fewer datasets, never a
# leak. "Last updated" + the published fields come from the T31 item_metadata record (the uniform
# source — asset tables don't all carry an updated_at, and this is what DCAT's `modified` serializes
# in c5). Formats are geometry-derived; the anon /export that serves them lands in c3.

_API_FEATURES_URL = "/api/v1/layers/{layer_id}/features"  # the per-dataset programmatic URL (F3)


async def _external_base(request: Request, conn: AsyncConnection) -> str:
    """The absolute root for externally-consumable URLs (OGC links, DCAT distributions): the
    admin-set advertised base URL (V063 — the authoritative externally-reachable address) when
    set, else this request's own base. Fail-soft to the request base on any settings-read error."""
    from ..org_settings import read_org_settings  # lazy: keep org_settings a leaf (no import cycle)

    try:
        adv = (await read_org_settings(conn)).advertisedBaseUrl
    except Exception:  # noqa: BLE001 — a settings hiccup must not take down the public catalog
        adv = None
    return (adv or str(request.base_url)).rstrip("/")


class OpenDataset(BaseModel):
    """One curated public dataset, as the catalog list + detail page render it (decision §F3)."""

    layerId: str
    title: str
    abstract: str | None = None
    keywords: list[str] = []
    topicCategory: str | None = None
    license: str | None = None
    ownerName: str | None = None
    featureCount: int
    updatedAt: datetime | None = None
    hasGeometry: bool
    # The fact strip's two reasons to exist (handoff-auth-opendata-4 §3.2): without them a visitor
    # must DOWNLOAD a dataset to learn its geometry type or projection. PostGIS's own spelling
    # ('Point', 'MultiPolygon'); collapsing Multi* to a UI label is the client's job. Both None for
    # a hosted table, and for a geom column with no typmod constraint — omitted, never guessed.
    geometryType: str | None = None
    srid: int | None = None
    formats: list[str]
    apiUrl: str
    # Interop I-1 tail: the standard OGC API — Features collection on the PUBLIC door — the URL a
    # desktop GIS, GDAL, or any OGC client consumes directly. ABSOLUTE (advertised base ?? request
    # base) because its whole audience is off-portal. None for geometry-less tables (no OGC story).
    ogcUrl: str | None = None


class OpenRasterDataset(BaseModel):
    """One curated public RASTER (raster-public slice): imagery/elevation the org publishes. The
    download formats are the two GUARANTEED raster exports (GeoTIFF + COG — img/jp2 are probed
    per-build, so the public catalog only promises what every build can serve), present only when
    the raster is `downloadable`; view-only rasters still stream (tiles/WMTS) but offer no files."""

    rasterId: str
    title: str
    abstract: str | None = None
    keywords: list[str] = []
    topicCategory: str | None = None
    license: str | None = None
    ownerName: str | None = None
    updatedAt: datetime | None = None
    bandCount: int | None = None
    downloadable: bool = True
    formats: list[str] = []          # ['geotiff','cog'] when downloadable, else []
    thumbnailUrl: str                # anon-capable (raster-public anon read path)
    tilejsonUrl: str                 # the public preview source (anon-capable)
    wmtsUrl: str                     # PUBLIC WMTS door capabilities (stream in a desktop)
    wcsUrl: str | None = None        # PUBLIC WCS door (live values) — only when downloadable


_RASTER_FORMATS = ["geotiff", "cog"]


async def _registered_curated_rasters(conn: AsyncConnection) -> list[str]:
    """Curated raster ids that are REAL, READY platform.rasters rows — fail-SOFT like the layer
    twin (an enumeration error empties the raster section, never 500s the catalog)."""
    try:
        ids = await _objects_shared_with_group(_OPEN_DATA_GROUP, "raster")
    except HTTPException:
        log.warning("open_data_raster_enumerate_failed")
        return []
    if not ids:
        return []
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id::text FROM platform.rasters WHERE id = ANY(%s::uuid[]) AND status = 'ready'",
            (ids,),
        )
        return [r[0] for r in await cur.fetchall()]


async def _visible_owner_raster(principal: User | None, raster_id: str) -> tuple[bool, str | None]:
    """Per-viewer visibility for a curated raster + its owner. Anon -> public_shared only; staff ->
    can_view. The raster twin of _visible_owner."""
    shares, owner = await get_object_shares_and_owner(f"raster:{raster_id}")
    if principal is None:
        visible = bool(shares["publicAccess"])
    else:
        visible = await _openfga_check(principal.id, "can_view", raster_id, object_type="raster")
    return visible, owner


async def _raster_rows(conn: AsyncConnection, raster_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Batch-load the catalog-relevant raster columns + item_metadata docs — two queries total."""
    rows: dict[str, dict[str, Any]] = {}
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id::text AS id, title, band_count, downloadable FROM platform.rasters "
            "WHERE id = ANY(%s::uuid[])",
            (raster_ids,),
        )
        for r in await cur.fetchall():
            rows[r["id"]] = {**r, "doc": {}, "updated_at": None}
        await cur.execute(
            "SELECT item_id, doc, updated_at FROM platform.item_metadata "
            "WHERE item_type = 'raster' AND item_id = ANY(%s)",
            (raster_ids,),
        )
        for r in await cur.fetchall():
            if r["item_id"] in rows:
                rows[r["item_id"]]["doc"] = r["doc"] or {}
                rows[r["item_id"]]["updated_at"] = r["updated_at"]
    return rows


def _assemble_raster(raster_id: str, row: dict[str, Any], owner_name: str | None, base: str) -> OpenRasterDataset:
    doc = row["doc"]
    kws = doc.get("keywords")
    downloadable = bool(row["downloadable"])
    return OpenRasterDataset(
        rasterId=raster_id,
        title=(doc.get("title") or row["title"] or raster_id),
        abstract=doc.get("abstract"),
        keywords=kws if isinstance(kws, list) else [],
        topicCategory=doc.get("topicCategory"),
        license=doc.get("license"),
        ownerName=owner_name,
        updatedAt=row["updated_at"],
        bandCount=row["band_count"],
        downloadable=downloadable,
        formats=_RASTER_FORMATS if downloadable else [],
        thumbnailUrl=f"{base}/api/v1/rasters/{raster_id}/thumbnail.png",
        tilejsonUrl=f"{base}/api/v1/rasters/{raster_id}/tilejson.json",
        wmtsUrl=f"{base}/api/v1/wmts/1.0.0/WMTSCapabilities.xml",
        wcsUrl=f"{base}/api/v1/wcs" if downloadable else None,
    )


def _formats_for(has_geometry: bool) -> list[str]:
    """Download formats a dataset offers: a geometry layer -> CSV/GeoJSON/Shapefile/GeoPackage; a
    geometry-less hosted table -> CSV only (the rest are geometry encodings). GeoPackage
    (decision-gpkg-feature-export.md) is the exact, modern OGC vector format."""
    return ["csv", "geojson", "shapefile", "gpkg"] if has_geometry else ["csv"]


async def _registered_curated(conn: AsyncConnection) -> list[str]:
    """The curated layer ids that are REAL platform.layers rows (drops any dangling OpenFGA tuple),
    fail-SOFT: an enumeration error yields an empty catalog, never a 500."""
    try:
        ids = await _objects_shared_with_group(_OPEN_DATA_GROUP, "layer")
    except HTTPException:
        log.warning("open_data_enumerate_failed")
        return []
    if not ids:
        return []
    async with conn.cursor() as cur:
        await cur.execute("SELECT layer_id FROM platform.layers WHERE layer_id = ANY(%s)", (ids,))
        return [r[0] for r in await cur.fetchall()]


async def _visible_owner(principal: User | None, layer_id: str) -> tuple[bool, str | None]:
    """Per-viewer visibility for a curated layer + its owner (one OpenFGA read). Anon -> public_shared
    only; staff -> can_view. Mirrors /home/featured exactly. Fail-soft is the caller's job."""
    shares, owner = await get_object_shares_and_owner(f"layer:{layer_id}")
    if principal is None:
        visible = bool(shares["publicAccess"])
    else:
        visible = await _openfga_check(principal.id, "can_view", layer_id, object_type="layer")
    return visible, owner


async def _metadata_and_geom(
    conn: AsyncConnection, layer_ids: list[str]
) -> tuple[
    dict[str, tuple[dict, datetime | None]],
    set[str],
    dict[str, tuple[str | None, int | None]],
]:
    """Batch-load (doc, updated_at) per layer from item_metadata, the set of layer_ids whose asset
    table has a `geom` column, and each one's (geometry_type, srid) — three queries for the whole
    catalog, not per-row.

    The `geom` SET is deliberately still derived from its original information_schema query rather
    than from the new typmod lookup: it drives `formats`, `ogcUrl` and the DCAT feed, and those must
    not shift because a column is typed differently than expected. The typmod read is purely
    additive (handoff-auth-opendata-4 §6.6)."""
    meta: dict[str, tuple[dict, datetime | None]] = {}
    geom: set[str] = set()
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT item_id, doc, updated_at FROM platform.item_metadata "
            "WHERE item_type = 'layer' AND item_id = ANY(%s)",
            (layer_ids,),
        )
        for r in await cur.fetchall():
            meta[r["item_id"]] = (r["doc"] or {}, r["updated_at"])
        await cur.execute(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'assets' AND column_name = 'geom' AND table_name = ANY(%s)",
            (layer_ids,),
        )
        geom = {r["table_name"] for r in await cur.fetchall()}
    return meta, geom, await geom_meta(conn, layer_ids)


async def _feature_count(conn: AsyncConnection, layer_id: str) -> int:
    """count(*) on assets.<layer_id> — layer_id is a proven platform.layers row before it names a
    table (and Identifier quotes it). The curated set is small, so per-dataset counts are fine."""
    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL("SELECT count(*) FROM {tbl}").format(tbl=sql.Identifier("assets", layer_id))
        )
        return (await cur.fetchone())[0]


def _assemble(
    layer_id: str, doc: dict, updated_at: datetime | None, owner_name: str | None,
    count: int, has_geom: bool, base: str,
    gmeta: tuple[str | None, int | None] = (None, None),
) -> OpenDataset:
    kws = doc.get("keywords")
    return OpenDataset(
        layerId=layer_id,
        title=(doc.get("title") or layer_id),
        abstract=doc.get("abstract"),
        keywords=kws if isinstance(kws, list) else [],
        topicCategory=doc.get("topicCategory"),
        license=doc.get("license"),
        ownerName=owner_name,
        featureCount=count,
        updatedAt=updated_at,
        hasGeometry=has_geom,
        geometryType=gmeta[0],
        srid=gmeta[1],
        formats=_formats_for(has_geom),
        apiUrl=_API_FEATURES_URL.format(layer_id=layer_id),
        ogcUrl=f"{base}/api/v1/ogc/collections/{layer_id}" if has_geom else None,
    )


async def _owner_names(conn: AsyncConnection, owner_ids: set[str]) -> dict[str, str]:
    if not owner_ids:
        return {}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id::text, COALESCE(display_name, username, email) FROM platform.users "
            "WHERE id = ANY(%s)",
            (list(owner_ids),),
        )
        return {r[0]: r[1] for r in await cur.fetchall()}


@router.get(
    "/open-data/datasets",
    summary="The public Open Data catalog: curated public datasets the caller may see",
)
async def list_datasets(
    request: Request,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> dict[str, list[Any]]:
    # F6 gate: an anon caller gets nothing when this deployment hasn't enabled the public face — the
    # same switch that gates the rest of the anon surface (T23). Staff are unaffected.
    if principal is None and not await public_sharing_enabled(conn):
        return {"datasets": [], "rasters": []}

    base = await _external_base(request, conn)

    # Curated RASTERS (raster-public slice) — same visibility filter, its own section.
    raster_out: list[OpenRasterDataset] = []
    raster_ids = await _registered_curated_rasters(conn)
    if raster_ids:
        rows = await _raster_rows(conn, raster_ids)
        r_visible: list[tuple[str, str | None]] = []
        for rid in raster_ids:
            if rid not in rows:
                continue
            try:
                ok, owner = await _visible_owner_raster(principal, rid)
            except HTTPException:
                continue  # fail-soft per item
            if ok:
                r_visible.append((rid, owner))
        r_names = await _owner_names(conn, {o for _rid, o in r_visible if o})
        raster_out = [
            _assemble_raster(rid, rows[rid], r_names.get(owner) if owner else None, base)
            for rid, owner in r_visible
        ]
        raster_out.sort(key=lambda d: d.title.lower())

    layer_ids = await _registered_curated(conn)
    if not layer_ids:
        return {"datasets": [], "rasters": raster_out}
    meta, geom, gmeta = await _metadata_and_geom(conn, layer_ids)

    # Per-viewer visibility filter (fail-soft per item) -> the layers this caller may actually see.
    visible: list[tuple[str, str | None]] = []
    for layer_id in layer_ids:
        try:
            ok, owner = await _visible_owner(principal, layer_id)
        except HTTPException:
            continue  # one item's check failed — skip it, keep the catalog
        if ok:
            visible.append((layer_id, owner))

    names = await _owner_names(conn, {o for _lid, o in visible if o})
    datasets: list[OpenDataset] = []
    for layer_id, owner in visible:
        doc, updated_at = meta.get(layer_id, ({}, None))
        count = await _feature_count(conn, layer_id)
        datasets.append(
            _assemble(layer_id, doc, updated_at, names.get(owner) if owner else None,
                      count, layer_id in geom, base, gmeta.get(layer_id, (None, None)))
        )
    datasets.sort(key=lambda d: d.title.lower())
    return {"datasets": datasets, "rasters": raster_out}


@router.get(
    "/open-data/datasets/{layer_id}",
    summary="One curated public dataset's detail (metadata, feature count, formats, API URL)",
)
async def get_dataset(
    layer_id: str,
    request: Request,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> OpenDataset:
    # Existence-hiding for anon: anything not curated-AND-visible is a flat 404 (never reveal that a
    # non-public layer exists). F6-gated for anon like the list.
    if principal is None and not await public_sharing_enabled(conn):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if layer_id not in await _registered_curated(conn):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    try:
        visible, owner = await _visible_owner(principal, layer_id)
    except HTTPException:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    meta, geom, gmeta = await _metadata_and_geom(conn, [layer_id])
    doc, updated_at = meta.get(layer_id, ({}, None))
    names = await _owner_names(conn, {owner} if owner else set())
    count = await _feature_count(conn, layer_id)
    return _assemble(layer_id, doc, updated_at, names.get(owner) if owner else None,
                     count, layer_id in geom, await _external_base(request, conn),
                     gmeta.get(layer_id, (None, None)))


@router.get(
    "/open-data/rasters/{raster_id}",
    summary="One curated public raster's detail (metadata, formats, preview + connect URLs)",
)
async def get_raster_dataset(
    raster_id: str,
    request: Request,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> OpenRasterDataset:
    # Existence-hiding for anon: anything not curated-AND-visible is a flat 404; F6-gated like the
    # list. (Separate route from the layer detail — raster ids are uuids, layer ids tal_ slugs.)
    if principal is None and not await public_sharing_enabled(conn):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if raster_id not in await _registered_curated_rasters(conn):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    try:
        visible, owner = await _visible_owner_raster(principal, raster_id)
    except HTTPException:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    rows = await _raster_rows(conn, [raster_id])
    names = await _owner_names(conn, {owner} if owner else set())
    return _assemble_raster(
        raster_id, rows[raster_id], names.get(owner) if owner else None,
        await _external_base(request, conn),
    )


# --- DCAT-US feed (c5) — data.gov federation ------------------------------------
#
# GET /open-data/dcat serializes the curated PUBLIC datasets to a DCAT-US 1.1 (Project Open Data)
# catalog — the emit-only interop surface that lets data.gov / open-data aggregators harvest the org's
# open data. We PUBLISH a feed; we are NOT a CSW/harvest server. Always the public view (curated ∩
# public) regardless of caller — a harvester only cares about what it can actually fetch — and
# F6-gated (an air-gapped deploy emits an empty, still well-formed catalog). The first metadata
# serializer: pure `item_metadata.doc -> DCAT encoding` (the .qmd / ISO-19139 serializers come later).

_DCAT_CONTEXT = "https://project-open-data.cio.gov/v1.1/schema/catalog.jsonld"
_DCAT_SCHEMA = "https://project-open-data.cio.gov/v1.1/schema"

# (mediaType, human format label) per download format the catalog advertises.
_DCAT_DIST = {
    "csv": ("text/csv", "CSV"),
    "geojson": ("application/geo+json", "GeoJSON"),
    "shapefile": ("application/zip", "Esri Shapefile"),
    "gpkg": ("application/geopackage+sqlite3", "GeoPackage"),
}


def _dcat_distribution(base: str, layer_id: str, fmt: str) -> dict[str, Any]:
    media, label = _DCAT_DIST[fmt]
    return {
        "@type": "dcat:Distribution",
        "downloadURL": f"{base}/api/v1/layers/{layer_id}/export?format={fmt}",
        "mediaType": media,
        "format": label,
        "title": f"{label} download",
    }


def _dcat_ogc_distribution(base: str, layer_id: str) -> dict[str, Any]:
    """The live-API distribution (Interop I-1 tail): the dataset's OGC API — Features collection on
    the PUBLIC door — what a harvester advertises as the queryable service next to the downloads.
    `accessURL` (not downloadURL): it's an API root, not a file."""
    return {
        "@type": "dcat:Distribution",
        "accessURL": f"{base}/api/v1/ogc/collections/{layer_id}",
        "format": "API",
        "title": "OGC API — Features",
        "conformsTo": "http://www.opengis.net/spec/ogcapi-features-1/1.0",
    }


def _dcat_dataset(
    base: str, layer_id: str, doc: dict[str, Any], updated_at: datetime | None,
    formats: list[str], has_geometry: bool,
) -> dict[str, Any]:
    """One curated public dataset as a DCAT-US 1.1 `dcat:Dataset` (pure doc -> encoding)."""
    landing = f"{base}/open-data/datasets/{layer_id}"
    contact = doc.get("pointOfContact") if isinstance(doc.get("pointOfContact"), dict) else {}
    keywords = doc.get("keywords")
    ds: dict[str, Any] = {
        "@type": "dcat:Dataset",
        "identifier": landing,
        "title": doc.get("title") or layer_id,
        "description": doc.get("abstract") or "",
        "keyword": keywords if isinstance(keywords, list) else [],
        "modified": updated_at.isoformat() if updated_at else "",
        "accessLevel": "public",
        "landingPage": landing,
        "publisher": {"@type": "org:Organization", "name": settings.open_data_publisher_name},
        "contactPoint": {
            "@type": "vcard:Contact",
            "fn": contact.get("name") or settings.open_data_publisher_name,
        },
        "distribution": (
            # The live OGC API first (a geometry-less table has no OGC story), then the downloads.
            ([_dcat_ogc_distribution(base, layer_id)] if has_geometry else [])
            + [_dcat_distribution(base, layer_id, f) for f in formats]
        ),
    }
    if contact.get("email"):
        ds["contactPoint"]["hasEmail"] = f"mailto:{contact['email']}"
    if doc.get("license"):
        ds["license"] = doc["license"]
    if doc.get("topicCategory"):
        ds["theme"] = [doc["topicCategory"]]
    bbox = doc.get("spatialExtent")
    if isinstance(bbox, dict) and all(k in bbox for k in ("west", "south", "east", "north")):
        # POD `spatial` = a bbox string "minX,minY,maxX,maxY" (lon/lat, EPSG:4326).
        ds["spatial"] = f"{bbox['west']},{bbox['south']},{bbox['east']},{bbox['north']}"
    return ds


_DCAT_RASTER_DIST = {
    "geotiff": ("image/tiff", "GeoTIFF"),
    "cog": ("image/tiff; application=geotiff; profile=cloud-optimized", "Cloud-Optimized GeoTIFF"),
}


def _dcat_raster_dataset(
    base: str, raster_id: str, row: dict[str, Any],
) -> dict[str, Any]:
    """One curated public RASTER as a dcat:Dataset: file distributions (downloadable only) + the
    live WMTS door, + the WCS values door when downloadable. Landing page = the portal detail."""
    doc = row["doc"]
    updated_at = row["updated_at"]
    landing = f"{base}/open-data/rasters/{raster_id}"
    contact = doc.get("pointOfContact") if isinstance(doc.get("pointOfContact"), dict) else {}
    keywords = doc.get("keywords")
    dists: list[dict[str, Any]] = [{
        "@type": "dcat:Distribution",
        "accessURL": f"{base}/api/v1/wmts/1.0.0/WMTSCapabilities.xml",
        "format": "API",
        "title": "WMTS (tiled imagery)",
        "conformsTo": "http://www.opengis.net/spec/wmts/1.0",
    }]
    if row["downloadable"]:
        dists.append({
            "@type": "dcat:Distribution",
            "accessURL": f"{base}/api/v1/wcs",
            "format": "API",
            "title": "WCS (live raster values)",
            "conformsTo": "http://www.opengis.net/spec/wcs/1.0",
        })
        for fmt in _RASTER_FORMATS:
            media, label = _DCAT_RASTER_DIST[fmt]
            dists.append({
                "@type": "dcat:Distribution",
                "downloadURL": f"{base}/api/v1/rasters/{raster_id}/export?format={fmt}",
                "mediaType": media,
                "format": label,
                "title": f"{label} download",
            })
    ds: dict[str, Any] = {
        "@type": "dcat:Dataset",
        "identifier": landing,
        "title": doc.get("title") or row["title"] or raster_id,
        "description": doc.get("abstract") or "",
        "keyword": keywords if isinstance(keywords, list) else [],
        "modified": updated_at.isoformat() if updated_at else "",
        "accessLevel": "public",
        "landingPage": landing,
        "publisher": {"@type": "org:Organization", "name": settings.open_data_publisher_name},
        "contactPoint": {
            "@type": "vcard:Contact",
            "fn": contact.get("name") or settings.open_data_publisher_name,
        },
        "distribution": dists,
    }
    if contact.get("email"):
        ds["contactPoint"]["hasEmail"] = f"mailto:{contact['email']}"
    if doc.get("license"):
        ds["license"] = doc["license"]
    if doc.get("topicCategory"):
        ds["theme"] = [doc["topicCategory"]]
    bbox = doc.get("spatialExtent")
    if isinstance(bbox, dict) and all(k in bbox for k in ("west", "south", "east", "north")):
        ds["spatial"] = f"{bbox['west']},{bbox['south']},{bbox['east']},{bbox['north']}"
    return ds


@router.get(
    "/open-data/dcat",
    summary="DCAT-US 1.1 catalog feed of the curated public datasets (data.gov federation)",
)
async def dcat_feed(request: Request, conn: AsyncConnection = Depends(get_db)) -> dict[str, Any]:
    # Harvesters re-serve these URLs to third parties, so absolute URLs come from the admin-set
    # advertised base (V063) when configured — the request's own base is only the fallback.
    base = await _external_base(request, conn)
    catalog: dict[str, Any] = {
        "@context": _DCAT_CONTEXT,
        "@type": "dcat:Catalog",
        "conformsTo": _DCAT_SCHEMA,
        "dataset": [],
    }
    # F6 OFF: no public face -> an empty (still well-formed) catalog, mirroring the anon catalog gate.
    if not await public_sharing_enabled(conn):
        return catalog

    datasets: list[dict[str, Any]] = []
    layer_ids = await _registered_curated(conn)
    if layer_ids:  # no early return — curated rasters must still emit when zero layers are curated
        meta, geom, _ = await _metadata_and_geom(conn, layer_ids)  # DCAT output must not shift
        for layer_id in layer_ids:
            # PUBLIC-only: a harvester can only fetch public data, so emit exactly curated ∩
            # public_shared (fail-soft per item — an OpenFGA hiccup drops one dataset, never leaks
            # a non-public one).
            try:
                shares, _owner = await get_object_shares_and_owner(f"layer:{layer_id}")
            except HTTPException:
                continue
            if not shares["publicAccess"]:
                continue
            doc, updated_at = meta.get(layer_id, ({}, None))
            datasets.append(
                _dcat_dataset(base, layer_id, doc, updated_at,
                              _formats_for(layer_id in geom), layer_id in geom)
            )
    # Curated public RASTERS join the feed (raster-public slice) — same curated ∩ public rule.
    raster_ids = await _registered_curated_rasters(conn)
    if raster_ids:
        rows = await _raster_rows(conn, raster_ids)
        for rid in raster_ids:
            if rid not in rows:
                continue
            try:
                shares, _owner = await get_object_shares_and_owner(f"raster:{rid}")
            except HTTPException:
                continue
            if not shares["publicAccess"]:
                continue
            datasets.append(_dcat_raster_dataset(base, rid, rows[rid]))
    catalog["dataset"] = datasets
    return catalog
