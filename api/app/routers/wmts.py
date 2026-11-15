"""Interop I-2 — WMTS 1.0.0 façade over the raster tile core (decision-interop-i2-wmts.md).

Every viewable READY raster becomes a WMTS layer — no publish step, no service registry — so
ArcGIS Pro and QGIS stream hosted imagery with a personal access token exactly the way they read
vector through the OGC doors. Two front doors from ONE factory, mirroring routers/ogc.py:
  - /api/v1/wmts       (PUBLIC)  — never challenges; anon sees public rasters (none until rasters
    gain public sharing), credential-volunteering callers see their full scope.
  - /api/v1/wmts-auth  (SIGN-IN) — an unauthenticated request gets 401 + the shared BASIC_CHALLENGE
    so a challenge-driven desktop client authenticates.

Scope: GetCapabilities + GetTile ONLY (the North-Star fence — no GetFeatureInfo, no WMS, no styles
beyond `default`, no dimensions). The single well-known GoogleMapsCompatible TileMatrixSet
(EPSG:3857, z 0..22) — the raster tile core is already XYZ/WebMercator, so WMTS
TileMatrix/TileRow/TileCol map 1:1 onto z/y/x. Tiles delegate to the existing render path
(default render); the raster's own Bearer-only routes are untouched.
"""
from typing import Any
from xml.sax.saxutils import escape as xml_escape

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from psycopg import AsyncConnection

from ..auth import BASIC_CHALLENGE, get_optional_user
from ..authz import _openfga_check, _openfga_list_objects, public_object_allowed
from ..db import get_db
from ..media import MediaStoreUnavailable
from ..models.user import User
from ..raster_serve import render_tile, tilejson_meta

log = structlog.get_logger(__name__)

PUBLIC_MOUNT = "/wmts"
AUTH_MOUNT = "/wmts-auth"

_TILE_CACHE = "private, max-age=3600"
_MATRIX_SET = "GoogleMapsCompatible"
_MAX_ZOOM = 22
# The OGC well-known GoogleMapsCompatible scale set: scaleDenominator[0] then halving per level;
# top-left is the WebMercator origin; matrix at level z is 2^z × 2^z tiles of 256px.
_SCALE_DENOM_0 = 559082264.028717
_ORIGIN = "-20037508.342789244 20037508.342789244"


def _base(request: Request, mount: str) -> str:
    """Absolute WMTS root for THIS door from the address the client dialed (the links-stay-on-the-
    door lesson) — valid on whichever interface / caddy edge served the request."""
    u = request.url
    return f"{u.scheme}://{u.netloc}/api/v1{mount}"


def _xml(body: str) -> Response:
    # no-store: per-user capabilities + the Qt truncated-cache lesson (a once-interrupted response
    # must never be replayed from a desktop disk cache).
    return Response(
        content=body, media_type="application/xml", headers={"Cache-Control": "no-store"}
    )


async def _viewable_rasters(
    conn: AsyncConnection, principal: User | None
) -> list[tuple[str, str | None]]:
    """(raster_id, title) for every READY raster THIS caller may see. Authenticated = can_view
    (ListObjects) ∪ god-mode admin; anonymous = public_object_allowed (rasters have no public seam
    yet, so this is empty today — the public door is future-proofed for when they gain one).
    Fail-SOFT per item: an OpenFGA hiccup omits the raster, never breaks the whole capabilities."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id::text, title FROM platform.rasters WHERE status = 'ready' "
            "ORDER BY title NULLS LAST, id"
        )
        rows = await cur.fetchall()
    if not rows:
        return []

    if principal is not None and not principal.is_admin:
        # One ListObjects, then intersect — cheaper than a per-row check for a signed-in user.
        try:
            viewable = set(await _openfga_list_objects(principal.id, "can_view", "raster"))
        except HTTPException:
            log.warning("wmts_scope_degraded", user_id=str(principal.id))
            return []
        return [(rid, title) for rid, title in rows if rid in viewable]

    out: list[tuple[str, str | None]] = []
    for rid, title in rows:
        try:
            if principal is not None:  # admin god-mode: sees all ready rasters
                out.append((rid, title))
            elif await public_object_allowed(conn, rid, object_type="raster"):
                out.append((rid, title))
        except HTTPException:
            continue  # fail-soft
    return out


async def _load_ready(conn: AsyncConnection, raster_id: str) -> dict[str, Any] | None:
    """The serve-relevant row for a ready raster, or None (absent / not ready). No auth here — the
    caller gates first."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT object_key, band_count, default_render, status FROM platform.rasters "
            "WHERE id = %s",
            (raster_id,),
        )
        row = await cur.fetchone()
    if row is None or row[3] != "ready":
        return None
    return {"object_key": row[0], "band_count": row[1], "default_render": row[2] or {}}


def _tile_matrix_set() -> str:
    """The GoogleMapsCompatible TileMatrixSet (EPSG:3857, z 0..22) — the OGC well-known scale set
    both pinned clients accept."""
    matrices = []
    for z in range(_MAX_ZOOM + 1):
        denom = _SCALE_DENOM_0 / (2 ** z)
        n = 2 ** z
        matrices.append(
            "    <TileMatrix>\n"
            f"      <ows:Identifier>{z}</ows:Identifier>\n"
            f"      <ScaleDenominator>{denom:.9f}</ScaleDenominator>\n"
            f"      <TopLeftCorner>{_ORIGIN}</TopLeftCorner>\n"
            "      <TileWidth>256</TileWidth>\n"
            "      <TileHeight>256</TileHeight>\n"
            f"      <MatrixWidth>{n}</MatrixWidth>\n"
            f"      <MatrixHeight>{n}</MatrixHeight>\n"
            "    </TileMatrix>"
        )
    return (
        "  <TileMatrixSet>\n"
        f"    <ows:Identifier>{_MATRIX_SET}</ows:Identifier>\n"
        "    <ows:SupportedCRS>urn:ogc:def:crs:EPSG::3857</ows:SupportedCRS>\n"
        + "\n".join(matrices)
        + "\n  </TileMatrixSet>"
    )


def _layer_xml(base: str, rid: str, title: str | None, bbox: list[float] | None) -> str:
    """One <Layer>: default style, image/png, a RESTful ResourceURL tile template, the shared
    matrix set. WGS84BoundingBox makes both clients zoom to the raster instead of the world."""
    t = xml_escape(title or rid)
    bbox_xml = ""
    if bbox is not None:
        w, s, e, n = bbox
        bbox_xml = (
            "    <ows:WGS84BoundingBox>\n"
            f"      <ows:LowerCorner>{w} {s}</ows:LowerCorner>\n"
            f"      <ows:UpperCorner>{e} {n}</ows:UpperCorner>\n"
            "    </ows:WGS84BoundingBox>\n"
        )
    tmpl = f"{base}/1.0.0/{rid}/default/{_MATRIX_SET}/{{TileMatrix}}/{{TileRow}}/{{TileCol}}.png"
    return (
        "  <Layer>\n"
        f"    <ows:Title>{t}</ows:Title>\n"
        f"    <ows:Identifier>{xml_escape(rid)}</ows:Identifier>\n"
        f"{bbox_xml}"
        "    <Style isDefault=\"true\"><ows:Identifier>default</ows:Identifier></Style>\n"
        "    <Format>image/png</Format>\n"
        f"    <TileMatrixSetLink><TileMatrixSet>{_MATRIX_SET}</TileMatrixSet></TileMatrixSetLink>\n"
        f"    <ResourceURL format=\"image/png\" resourceType=\"tile\" template=\"{xml_escape(tmpl)}\"/>\n"
        "  </Layer>"
    )


def _build_wmts_router(mount: str, *, challenge: bool) -> APIRouter:
    """One WMTS door. `challenge` makes an unauthenticated request answer 401 + BASIC_CHALLENGE
    (the SIGN-IN door) instead of the anonymous view. Handlers are identical across doors — only the
    guard + link base differ — so behaviour can't drift. The sign-in door is hidden from OpenAPI."""
    router = APIRouter(tags=["wmts"])
    in_schema = not challenge

    def guard(principal: User | None) -> None:
        if challenge and principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required — this is the authenticated WMTS endpoint",
                headers={"WWW-Authenticate": BASIC_CHALLENGE},
            )

    async def _capabilities(request: Request, conn: AsyncConnection, principal: User | None) -> Response:
        guard(principal)
        base = _base(request, mount)
        rasters = await _viewable_rasters(conn, principal)
        layers = []
        for rid, title in rasters:
            rec = await _load_ready(conn, rid)
            if rec is None:
                continue
            try:
                meta = await run_in_threadpool(tilejson_meta, rec["object_key"])
                bbox = meta.bounds
            except Exception:  # noqa: BLE001 — a metadata read failure omits the bbox, not the layer
                bbox = None
            layers.append(_layer_xml(base, rid, title, bbox))
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Capabilities xmlns="http://www.opengis.net/wmts/1.0" '
            'xmlns:ows="http://www.opengis.net/ows/1.1" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" version="1.0.0">\n'
            "  <ows:ServiceIdentification>\n"
            "    <ows:Title>Talus GIS — WMTS</ows:Title>\n"
            "    <ows:ServiceType>OGC WMTS</ows:ServiceType>\n"
            "    <ows:ServiceTypeVersion>1.0.0</ows:ServiceTypeVersion>\n"
            "  </ows:ServiceIdentification>\n"
            "  <ows:OperationsMetadata>\n"
            + _operation("GetCapabilities", base)
            + _operation("GetTile", base)
            + "  </ows:OperationsMetadata>\n"
            "  <Contents>\n"
            + ("\n".join(layers) + "\n" if layers else "")
            + _tile_matrix_set()
            + "\n  </Contents>\n"
            f'  <ServiceMetadataURL xlink:href="{xml_escape(base)}/1.0.0/WMTSCapabilities.xml"/>\n'
            "</Capabilities>\n"
        )
        return _xml(body)

    @router.get(f"{mount}/1.0.0/WMTSCapabilities.xml",
                summary="WMTS capabilities (RESTful) — the caller's viewable rasters",
                include_in_schema=in_schema)
    async def wmts_capabilities_rest(
        request: Request,
        conn: AsyncConnection = Depends(get_db),
        principal: User | None = Depends(get_optional_user),
    ) -> Response:
        return await _capabilities(request, conn, principal)

    @router.get(mount, summary="WMTS KVP endpoint (GetCapabilities alias)", include_in_schema=in_schema)
    async def wmts_kvp(
        request: Request,
        service: str | None = None,
        request_: str | None = None,
        conn: AsyncConnection = Depends(get_db),
        principal: User | None = Depends(get_optional_user),
    ) -> Response:
        # KVP alias for GetCapabilities only (fork 2): some clients probe ?SERVICE=WMTS&REQUEST=
        # GetCapabilities before the RESTful URL. GetTile stays RESTful (no pinned client needs KVP
        # tiles). FastAPI can't bind a param literally named "request", so read REQUEST from the raw
        # query, case-insensitively.
        req = request_ or request.query_params.get("REQUEST") or request.query_params.get("request")
        if req and req.lower() != "getcapabilities":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="only REQUEST=GetCapabilities is served on the KVP endpoint (tiles are RESTful)",
            )
        return await _capabilities(request, conn, principal)

    @router.get(
        f"{mount}/1.0.0/{{raster_id}}/default/{_MATRIX_SET}/{{z}}/{{y}}/{{x}}.png",
        summary="One WMTS tile (RESTful) — TileRow=y, TileCol=x",
        include_in_schema=in_schema,
    )
    async def wmts_tile(
        raster_id: str,
        z: int,
        y: int,
        x: int,
        conn: AsyncConnection = Depends(get_db),
        principal: User | None = Depends(get_optional_user),
    ) -> Response:
        guard(principal)
        # can_view gate — anon only when the raster is public (none today); a signed-in caller via
        # OpenFGA. A denied/absent raster is a flat 404 (no existence oracle) except the sign-in door
        # already 401'd a no-credentials request in guard().
        allowed = False
        if principal is not None:
            allowed = principal.is_admin or await _openfga_check(
                principal.id, "can_view", raster_id, object_type="raster"
            )
        else:
            try:
                allowed = await public_object_allowed(conn, raster_id, object_type="raster")
            except HTTPException:
                allowed = False
        if not allowed:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

        rec = await _load_ready(conn, raster_id)
        if rec is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        idx = rec["default_render"].get("bidx") or [1]
        # Guard the default render against a stale bidx (a re-banded COG) — clamp to band range.
        idx = [b for b in idx if 1 <= b <= rec["band_count"]] or [1]
        try:
            png = await run_in_threadpool(render_tile, rec["object_key"], z, x, y, idx, None, None)
        except MediaStoreUnavailable as exc:
            log.warning("wmts_tile_store_unreachable", raster=raster_id, detail=str(exc))
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="raster store unreachable")
        if png is None:  # outside the footprint — an empty tile, which both clients tolerate
            return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": _TILE_CACHE})
        return Response(content=png, media_type="image/png", headers={"Cache-Control": _TILE_CACHE})

    return router


def _operation(name: str, base: str) -> str:
    return (
        f'    <ows:Operation name="{name}">\n'
        "      <ows:DCP><ows:HTTP><ows:Get "
        f'xlink:href="{xml_escape(base)}/">\n'
        '        <ows:Constraint name="GetEncoding">'
        "<ows:AllowedValues><ows:Value>RESTful</ows:Value></ows:AllowedValues></ows:Constraint>\n"
        "      </ows:Get></ows:HTTP></ows:DCP>\n"
        "    </ows:Operation>\n"
    )


public_router = _build_wmts_router(PUBLIC_MOUNT, challenge=False)
auth_router = _build_wmts_router(AUTH_MOUNT, challenge=True)
