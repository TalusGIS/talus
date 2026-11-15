"""Where-used REST (decision-where-used.md) — per-item usage + the admin cross-reference report.

Three per-item routes (layers — which covers views and tables, rasters, external refs) feed the
delete-time warning and the Manage "Used in" panel; `/admin/reports/usage` is the T29-style org-wide
roll-up. The §3.1 rule everywhere: referencing items the CALLER can't view are OMITTED entirely —
no titles, no "+N hidden" counts (the client surfaces carry the standard disclaimer instead).

Gates (§4.1, fail-closed): every per-item route requires auth + can_view on the SUBJECT item (403;
404 for unknown ids; no anon — usage is management legibility, not a public read). Web-map and
view-layer hits are filtered by ONE list-objects call each (never a per-hit check storm); geocoder
hits are org-visible by design (the Locate panel lists locators) and are never filtered. talus-admin
sees every hit (parity with admin-reads-everything). OpenFGA down → 503 via the helpers.
"""
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from psycopg import AsyncConnection

from .. import usage
from ..authz import (
    _objects_shared_with_group,
    _openfga_check,
    _openfga_list_objects,
    view_allowed,
)
from ..auth import get_current_user
from ..db import get_db
from ..deps import _require_admin
from ..models.user import User
from .reports import _csv_response

log = structlog.get_logger(__name__)

router = APIRouter(tags=["usage"])


# --- shared helpers -------------------------------------------------------------------------------


async def _visible_maps(user: User, maps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter referencing maps to what the caller can view (§3.1: omit, never count). One
    list-objects call; skipped entirely for admins and for empty hit lists."""
    if not maps or user.is_admin:
        return maps
    viewable = set(await _openfga_list_objects(user.id, "can_view", "web_map"))
    return [m for m in maps if m["id"] in viewable]


async def _visible_layers(user: User, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The view-layer twin of _visible_maps (views are layers in OpenFGA)."""
    if not items or user.is_admin:
        return items
    viewable = set(await _openfga_list_objects(user.id, "can_view", "layer"))
    return [v for v in items if v["id"] in viewable]


async def _layer_row(conn: AsyncConnection, layer_id: str) -> tuple[str, str] | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(display_name, layer_id), geom_type FROM platform.layers WHERE layer_id = %s",
            (layer_id,),
        )
        row = await cur.fetchone()
    return (row[0], row[1]) if row is not None else None


# --- per-item routes ------------------------------------------------------------------------------


@router.get(
    "/layers/{layer_id}/usage",
    summary="Where a layer/view/table is used — viewer-filtered (decision-where-used.md)",
)
async def layer_usage(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    row = await _layer_row(conn, layer_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    if not user.is_admin and not await view_allowed(user, layer_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"not authorized to view {layer_id}")
    title, _geom = row
    maps = await _visible_maps(user, await usage.maps_referencing(conn, layer_id=layer_id))
    views = await _visible_layers(user, await usage.views_of(conn, layer_id))
    geocoders = await usage.geocoders_on(conn, layer_id)
    # Relates touching this item, either end (V078) — each hit names the OTHER end, which is a
    # layer-typed object, so the same §3.1 viewer filter applies (omit, never count).
    relates = await _visible_layers(user, await usage.relates_touching(conn, layer_id))
    return {
        "item": {"id": layer_id, "type": "layer", "title": title},
        "webMaps": maps,
        "viewLayers": views,
        "geocoders": geocoders,
        "relates": relates,
        "openData": layer_id in await _objects_shared_with_group("open_data", "layer"),
        "homeFeatured": layer_id in await _objects_shared_with_group("home", "layer"),
    }


@router.get(
    "/rasters/{raster_id}/usage",
    summary="Where a raster is used — viewer-filtered (decision-where-used.md)",
)
async def raster_usage(
    raster_id: str,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    async with conn.cursor() as cur:
        await cur.execute("SELECT title FROM platform.rasters WHERE id::text = %s", (raster_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="raster not found")
    if not user.is_admin and not await _openfga_check(
        user.id, "can_view", raster_id, object_type="raster"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"not authorized to view {raster_id}")
    maps = await _visible_maps(user, await usage.maps_referencing(conn, raster_id=raster_id))
    return {"item": {"id": raster_id, "type": "raster", "title": row[0]}, "webMaps": maps}


@router.get(
    "/external-refs/{ref_id}/usage",
    summary="Where an external reference item is used — viewer-filtered (decision-where-used.md)",
)
async def external_ref_usage(
    ref_id: str,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    async with conn.cursor() as cur:
        await cur.execute("SELECT title FROM platform.external_refs WHERE id = %s", (ref_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="external ref not found")
    # External ref ids double as layer-type OpenFGA objects (external_refs.py) — view_allowed applies.
    if not user.is_admin and not await view_allowed(user, ref_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"not authorized to view {ref_id}")
    maps = await _visible_maps(user, await usage.maps_referencing(conn, external_item_id=ref_id))
    return {"item": {"id": ref_id, "type": "external", "title": row[0]}, "webMaps": maps}


# --- the org-wide cross-reference report (T29 house style) ----------------------------------------


@router.get(
    "/admin/reports/usage",
    summary="Org-wide cross-reference report — every item + its reference counts (admin)",
)
async def usage_report(
    format: str = Query("json", pattern="^(json|csv)$"),
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> Any:
    map_counts = await usage.map_reference_counts(conn)
    view_counts, geocoder_counts = await usage.dependent_counts_by_layer(conn)

    # Group flags fail-SOFT (a degraded OpenFGA read blanks the two membership columns, never the
    # report — the content report's per-row degrade precedent). Everything else here is plain SQL.
    degraded = False
    try:
        open_data = set(await _objects_shared_with_group("open_data", "layer"))
        home_layers = set(await _objects_shared_with_group("home", "layer"))
        home_maps = set(await _objects_shared_with_group("home", "web_map"))
    except Exception as exc:  # noqa: BLE001 — degrade the flags, keep the report
        log.warning("usage_report_group_read_failed", detail=str(exc))
        degraded = True
        open_data, home_layers, home_maps = set(), set(), set()

    async with conn.cursor() as cur:
        await cur.execute("SELECT view_layer_id FROM platform.layer_views")
        view_ids = {r[0] for r in await cur.fetchall()}
        await cur.execute(
            "SELECT layer_id, COALESCE(display_name, layer_id), geom_type FROM platform.layers "
            "ORDER BY COALESCE(display_name, layer_id)"
        )
        layers = await cur.fetchall()
        await cur.execute("SELECT id::text, title FROM platform.rasters ORDER BY title")
        rasters = await cur.fetchall()
        await cur.execute("SELECT id, title FROM platform.external_refs ORDER BY title")
        externals = await cur.fetchall()
        await cur.execute("SELECT id::text, title FROM platform.web_maps ORDER BY title")
        maps = await cur.fetchall()

    rows: list[dict[str, Any]] = []
    for layer_id, title, geom in layers:
        kind = "view" if layer_id in view_ids else ("table" if geom == "none" else "layer")
        wm = map_counts.get(("layer", layer_id), 0)
        vc = view_counts.get(layer_id, 0)
        gc = geocoder_counts.get(layer_id, 0)
        od = layer_id in open_data
        hf = layer_id in home_layers
        rows.append({
            "itemType": kind, "itemId": layer_id, "title": title,
            "webMaps": wm, "viewLayers": vc, "geocoders": gc,
            "openData": od, "homeFeatured": hf,
            "total": wm + vc + gc + int(od) + int(hf),
        })
    for rid, title in rasters:
        wm = map_counts.get(("raster", rid), 0)
        rows.append({
            "itemType": "raster", "itemId": rid, "title": title,
            "webMaps": wm, "viewLayers": 0, "geocoders": 0,
            "openData": False, "homeFeatured": False, "total": wm,
        })
    for eid, title in externals:
        wm = map_counts.get(("external", eid), 0)
        rows.append({
            "itemType": "external", "itemId": eid, "title": title,
            "webMaps": wm, "viewLayers": 0, "geocoders": 0,
            "openData": False, "homeFeatured": False, "total": wm,
        })
    for mid, title in maps:
        hf = mid in home_maps
        rows.append({
            "itemType": "web_map", "itemId": mid, "title": title,
            "webMaps": 0, "viewLayers": 0, "geocoders": 0,
            "openData": False, "homeFeatured": hf, "total": int(hf),
        })

    if format == "csv":
        header = ["itemType", "itemId", "title", "webMaps", "viewLayers", "geocoders",
                  "openData", "homeFeatured", "total"]
        return _csv_response(
            "talus-cross-references.csv", header,
            [[r[k] for k in header] for r in rows],
        )
    referenced = sum(1 for r in rows if r["total"] > 0)
    return {
        "summary": {
            "items": len(rows),
            "itemsReferenced": referenced,
            "totalReferences": sum(r["total"] for r in rows),
            "degraded": degraded,
        },
        "rows": rows,
    }
