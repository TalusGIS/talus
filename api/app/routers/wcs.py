"""Interop I-6 — WCS 1.0.0 façade over the raster core (decision-interop-i6-coverages.md).

Every downloadable READY raster becomes a WCS coverage — no publish step, no service registry —
so ArcGIS Pro and QGIS read live, governed pixel VALUES with a PAT exactly the way they stream
tiles through the WMTS doors. Two front doors from ONE factory, mirroring routers/wmts.py:
  - /api/v1/wcs       (PUBLIC)  — never challenges; anon sees public+downloadable rasters (none
    until rasters gain public sharing); credential-volunteering callers see their full scope.
  - /api/v1/wcs-auth  (SIGN-IN) — an unauthenticated request gets 401 + the shared BASIC_CHALLENGE.

WHY 1.0.0 and only 1.0.0 (recon 2026-07-12, doc §1): QGIS's native WCS provider speaks 1.0.0/1.1.x
and PREFERS 1.0.0 ("1.1 has many issues" — qgswcscapabilities.cpp); Pro's version dropdown accepts
a 1.0.0-only server; OGC API — Coverages is experimental in GDAL and absent in Pro. 1.0.0 is also
the simplest response shape (bare GeoTIFF, no 1.1 multipart, no 2.0 axis-order lore).

Scope fences: GetCapabilities + DescribeCoverage + GetCoverage ONLY — no interpolation choices
beyond the implicit display decimation, no reprojection (native CRS advertised and served, clients
reproject), no analytics. Honoring the client's WIDTH/HEIGHT is display plumbing (the same
COG-overview decimation the tile path does), bounded by raster_max_export_megapixels and NEVER
upsampled past native — identify always returns true stored values.

Governance: can_view gates existence (flat 404 otherwise — no oracle); `downloadable` gates VALUES
(GetCoverage 403 + omission from capabilities/describe): the view-only knob keeps its meaning on
every values-serving path. Rasters whose CRS has no EPSG code are omitted (WKT srsNames are legal
WCS but a client-compat minefield; revisit on demand).
"""
from xml.sax.saxutils import escape as xml_escape

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from psycopg import AsyncConnection
from starlette.background import BackgroundTask

from ..auth import BASIC_CHALLENGE, get_optional_user
from ..authz import _openfga_check, _openfga_list_objects, public_object_allowed
from ..config import settings
from ..db import get_db
from ..media import MediaObjectMissing, MediaStoreUnavailable
from ..models.user import User
from ..raster_export import ExportEmpty, ExportTooLarge, coverage_to_geotiff
from ..raster_serve import NativeMeta, native_meta

log = structlog.get_logger(__name__)

PUBLIC_MOUNT = "/wcs"
AUTH_MOUNT = "/wcs-auth"

_VERSION = "1.0.0"
_FORMAT_LABEL = "GeoTIFF"
# FORMAT values we accept for GetCoverage (case-insensitive) — all mean the one format we serve.
_ACCEPTED_FORMATS = {"geotiff", "gtiff", "image/tiff", "tiff"}


def _base(request: Request, mount: str) -> str:
    """Absolute WCS root for THIS door from the address the client dialed (the links-stay-on-the-
    door lesson)."""
    u = request.url
    return f"{u.scheme}://{u.netloc}/api/v1{mount}"


def _xml(body: str, http_status: int = status.HTTP_200_OK) -> Response:
    # no-store: per-user capabilities + the Qt truncated-cache lesson (I-2).
    return Response(
        content=body, media_type="application/xml", status_code=http_status,
        headers={"Cache-Control": "no-store"},
    )


def _exception(message: str, http_status: int, code: str = "InvalidParameterValue") -> Response:
    """WCS 1.0.0 error body (ogc:ServiceExceptionReport) carried on an honest HTTP status — desktop
    clients show the message, scripts branch on the code."""
    return _xml(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ServiceExceptionReport version="1.2.0" xmlns="http://www.opengis.net/ogc">\n'
        f'  <ServiceException code="{xml_escape(code)}">{xml_escape(message)}</ServiceException>\n'
        "</ServiceExceptionReport>\n",
        http_status,
    )


def _qp(request: Request, name: str) -> str | None:
    """Case-insensitive KVP lookup (WCS parameter NAMES are case-insensitive; values are not)."""
    lname = name.lower()
    for k, v in request.query_params.items():
        if k.lower() == lname:
            return v
    return None


async def _coverage_rasters(
    conn: AsyncConnection, principal: User | None
) -> list[tuple[str, str | None, str]]:
    """(raster_id, title, object_key) for every READY + DOWNLOADABLE raster THIS caller may see —
    the WMTS scoping pattern (ListObjects ∪ admin god-mode; anon = public_object_allowed, empty
    until rasters gain public sharing) with the downloadable fence applied in SQL. Fail-SOFT per
    item: an OpenFGA hiccup omits the raster, never breaks the whole capabilities."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id::text, title, object_key FROM platform.rasters "
            "WHERE status = 'ready' AND downloadable ORDER BY title NULLS LAST, id"
        )
        rows = await cur.fetchall()
    if not rows:
        return []

    if principal is not None and not principal.is_admin:
        try:
            viewable = set(await _openfga_list_objects(principal.id, "can_view", "raster"))
        except HTTPException:
            log.warning("wcs_scope_degraded", user_id=str(principal.id))
            return []
        return [r for r in rows if r[0] in viewable]

    out: list[tuple[str, str | None, str]] = []
    for r in rows:
        try:
            if principal is not None:  # admin god-mode
                out.append(r)
            elif await public_object_allowed(conn, r[0], object_type="raster"):
                out.append(r)
        except HTTPException:
            continue  # fail-soft
    return out


async def _gated_coverage(
    conn: AsyncConnection, principal: User | None, raster_id: str
) -> dict:
    """The one-raster gate for DescribeCoverage/GetCoverage: can_view (or public) else flat 404;
    not-ready 404; downloadable=false 403 (mirrors the /export wording — the caller can already
    see this raster in the catalog, so the flag is no secret)."""
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

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT object_key, title, status, downloadable FROM platform.rasters WHERE id = %s",
            (raster_id,),
        )
        row = await cur.fetchone()
    if row is None or row[2] != "ready":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not row[3]:
        log.info("authz_rejected", capability="wcs:coverage", reason="not_downloadable",
                 raster=raster_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this raster is marked view-only (not downloadable)",
        )
    return {"object_key": row[0], "title": row[1]}


def _lonlat_envelope(bounds: list[float]) -> str:
    w, s, e, n = bounds
    return (
        '    <lonLatEnvelope srsName="urn:ogc:def:crs:OGC:1.3:CRS84">\n'
        f"      <gml:pos>{w} {s}</gml:pos>\n"
        f"      <gml:pos>{e} {n}</gml:pos>\n"
        "    </lonLatEnvelope>\n"
    )


def _brief_xml(rid: str, title: str | None, meta: NativeMeta) -> str:
    return (
        "  <CoverageOfferingBrief>\n"
        f"    <name>{xml_escape(rid)}</name>\n"
        f"    <label>{xml_escape(title or rid)}</label>\n"
        + _lonlat_envelope(meta.lonlat_bounds)
        + "  </CoverageOfferingBrief>"
    )


def _offering_xml(rid: str, title: str | None, meta: NativeMeta) -> str:
    """One full CoverageOffering for DescribeCoverage: native gml:Envelope + RectifiedGrid from
    the geotransform, native-CRS-only requestResponseCRSs, GeoTIFF-only formats."""
    left, bottom, right, top = meta.bounds
    a, b, c, d, e, f = meta.transform
    srs = f"EPSG:{meta.epsg}"
    return (
        "  <CoverageOffering>\n"
        f"    <name>{xml_escape(rid)}</name>\n"
        f"    <label>{xml_escape(title or rid)}</label>\n"
        + _lonlat_envelope(meta.lonlat_bounds)
        + "    <domainSet>\n"
        "      <spatialDomain>\n"
        f'        <gml:Envelope srsName="{srs}">\n'
        f"          <gml:pos>{left} {bottom}</gml:pos>\n"
        f"          <gml:pos>{right} {top}</gml:pos>\n"
        "        </gml:Envelope>\n"
        '        <gml:RectifiedGrid dimension="2">\n'
        "          <gml:limits><gml:GridEnvelope>"
        f"<gml:low>0 0</gml:low><gml:high>{meta.width - 1} {meta.height - 1}</gml:high>"
        "</gml:GridEnvelope></gml:limits>\n"
        "          <gml:axisName>x</gml:axisName>\n"
        "          <gml:axisName>y</gml:axisName>\n"
        f"          <gml:origin><gml:pos>{c} {f}</gml:pos></gml:origin>\n"
        f"          <gml:offsetVector>{a} {b}</gml:offsetVector>\n"
        f"          <gml:offsetVector>{d} {e}</gml:offsetVector>\n"
        "        </gml:RectifiedGrid>\n"
        "      </spatialDomain>\n"
        "    </domainSet>\n"
        "    <rangeSet>\n"
        "      <RangeSet>\n"
        "        <name>bands</name>\n"
        "        <label>bands</label>\n"
        "        <axisDescription><AxisDescription>\n"
        "          <name>Band</name><label>Band</label>\n"
        f"          <values><interval><min>1</min><max>{meta.band_count}</max></interval></values>\n"
        "        </AxisDescription></axisDescription>\n"
        # nullValues declares the fill so a client knows which pixels are absent rather than real.
        # WCS 1.0.0 orders RangeSet as name, label, (description)?, axisDescription*, (nullValues)? —
        # so this must stay AFTER axisDescription or the document fails schema validation.
        # Omitted entirely when the coverage has no nodata: an empty <nullValues> is not valid, and
        # claiming a null value the raster does not have would mask real pixels in a client.
        + (
            "        <nullValues><singleValue>"
            f"{meta.nodata:.10g}"
            "</singleValue></nullValues>\n"
            if meta.nodata is not None
            else ""
        )
        + "      </RangeSet>\n"
        "    </rangeSet>\n"
        f"    <supportedCRSs><requestResponseCRSs>{srs}</requestResponseCRSs></supportedCRSs>\n"
        f'    <supportedFormats nativeFormat="{_FORMAT_LABEL}">'
        f"<formats>{_FORMAT_LABEL}</formats></supportedFormats>\n"
        '    <supportedInterpolations default="nearest neighbor">'
        "<interpolationMethod>nearest neighbor</interpolationMethod></supportedInterpolations>\n"
        "  </CoverageOffering>"
    )


async def _meta_or_none(object_key: str, raster_id: str) -> NativeMeta | None:
    """native_meta with the fail-soft + EPSG fence applied: a metadata failure or a no-EPSG CRS
    omits the raster from the façade (logged), never breaks the whole response."""
    try:
        meta = await run_in_threadpool(native_meta, object_key)
    except Exception as exc:  # noqa: BLE001 — fail-soft per item
        log.warning("wcs_meta_unreadable", raster=raster_id, detail=str(exc))
        return None
    if meta.epsg is None:
        log.info("wcs_raster_omitted_no_epsg", raster=raster_id)
        return None
    return meta


def _parse_dims(request: Request, bbox: tuple[float, float, float, float]) -> tuple[int, int]:
    """WIDTH/HEIGHT, or RESX/RESY converted (the WCS 1.0.0 alternative Pro may send). Malformed or
    absent -> ValueError with a client-readable message."""
    w_raw, h_raw = _qp(request, "WIDTH"), _qp(request, "HEIGHT")
    if w_raw is not None or h_raw is not None:
        try:
            w, h = int(w_raw or ""), int(h_raw or "")
        except ValueError:
            raise ValueError("WIDTH and HEIGHT must both be positive integers")
        if w < 1 or h < 1:
            raise ValueError("WIDTH and HEIGHT must both be positive integers")
        return w, h
    rx_raw, ry_raw = _qp(request, "RESX"), _qp(request, "RESY")
    if rx_raw is not None or ry_raw is not None:
        try:
            rx, ry = abs(float(rx_raw or "")), abs(float(ry_raw or ""))
        except ValueError:
            raise ValueError("RESX and RESY must both be numbers")
        if rx <= 0 or ry <= 0:
            raise ValueError("RESX and RESY must both be non-zero")
        left, bottom, right, top = bbox
        return max(1, round((right - left) / rx)), max(1, round((top - bottom) / ry))
    raise ValueError("GetCoverage requires WIDTH & HEIGHT (or RESX & RESY)")


def _build_wcs_router(mount: str, *, challenge: bool) -> APIRouter:
    """One WCS door. `challenge` makes an unauthenticated request answer 401 + BASIC_CHALLENGE
    (the SIGN-IN door) instead of the anonymous view. Handlers are identical across doors — only
    the guard + link base differ — so behaviour can't drift. Hidden from OpenAPI on the sign-in
    door, like WMTS."""
    router = APIRouter(tags=["wcs"])
    in_schema = not challenge

    def guard(principal: User | None) -> None:
        if challenge and principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required — this is the authenticated WCS endpoint",
                headers={"WWW-Authenticate": BASIC_CHALLENGE},
            )

    async def _capabilities(request: Request, conn: AsyncConnection, principal: User | None) -> Response:
        base = _base(request, mount)
        briefs = []
        for rid, title, object_key in await _coverage_rasters(conn, principal):
            meta = await _meta_or_none(object_key, rid)
            if meta is not None:
                briefs.append(_brief_xml(rid, title, meta))
        op = (
            "      <{name}><DCPType><HTTP><Get>"
            f'<OnlineResource xlink:href="{xml_escape(base)}?"/>'
            "</Get></HTTP></DCPType></{name}>\n"
        )
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<WCS_Capabilities version="{_VERSION}" xmlns="http://www.opengis.net/wcs" '
            'xmlns:gml="http://www.opengis.net/gml" xmlns:xlink="http://www.w3.org/1999/xlink">\n'
            "  <Service>\n"
            "    <name>talus-wcs</name>\n"
            "    <label>Talus GIS — WCS (live raster values)</label>\n"
            "    <fees>NONE</fees>\n"
            "    <accessConstraints>NONE</accessConstraints>\n"
            "  </Service>\n"
            "  <Capability>\n"
            "    <Request>\n"
            + op.format(name="GetCapabilities")
            + op.format(name="DescribeCoverage")
            + op.format(name="GetCoverage")
            + "    </Request>\n"
            "    <Exception><Format>application/vnd.ogc.se_xml</Format></Exception>\n"
            "  </Capability>\n"
            "  <ContentMetadata>\n"
            + ("\n".join(briefs) + "\n" if briefs else "")
            + "  </ContentMetadata>\n"
            "</WCS_Capabilities>\n"
        )
        return _xml(body)

    async def _describe(request: Request, conn: AsyncConnection, principal: User | None) -> Response:
        # DescribeCoverage with no COVERAGE param describes ALL the caller's coverages (per spec).
        raw = _qp(request, "COVERAGE") or _qp(request, "COVERAGES")
        scoped = await _coverage_rasters(conn, principal)
        if raw:
            wanted = {c.strip() for c in raw.split(",") if c.strip()}
            by_id = {rid: (rid, title, key) for rid, title, key in scoped}
            missing = wanted - by_id.keys()
            if missing:
                # in-scope-or-404: an unknown/unviewable/non-downloadable name is indistinguishable
                return _exception(
                    f"no such coverage: {', '.join(sorted(missing))}",
                    status.HTTP_404_NOT_FOUND, code="CoverageNotDefined",
                )
            scoped = [by_id[c] for c in sorted(wanted)]
        offerings = []
        for rid, title, object_key in scoped:
            meta = await _meta_or_none(object_key, rid)
            if meta is not None:
                offerings.append(_offering_xml(rid, title, meta))
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<CoverageDescription version="{_VERSION}" xmlns="http://www.opengis.net/wcs" '
            'xmlns:gml="http://www.opengis.net/gml">\n'
            + ("\n".join(offerings) + "\n" if offerings else "")
            + "</CoverageDescription>\n"
        )
        return _xml(body)

    async def _get_coverage(request: Request, conn: AsyncConnection, principal: User | None) -> Response:
        rid = _qp(request, "COVERAGE")
        if not rid:
            return _exception("GetCoverage requires COVERAGE", status.HTTP_400_BAD_REQUEST,
                              code="MissingParameterValue")
        rec = await _gated_coverage(conn, principal, rid)
        meta = await _meta_or_none(rec["object_key"], rid)
        if meta is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        srs = f"EPSG:{meta.epsg}"

        fmt = _qp(request, "FORMAT")
        if fmt is not None and fmt.lower() not in _ACCEPTED_FORMATS:
            return _exception(f"unsupported FORMAT '{fmt}' — this service serves {_FORMAT_LABEL}",
                              status.HTTP_400_BAD_REQUEST)
        # native CRS only (doc §0 fence): CRS, and RESPONSE_CRS if sent, must be the advertised one.
        for pname in ("CRS", "RESPONSE_CRS"):
            v = _qp(request, pname)
            if v is not None and v.upper() != srs:
                return _exception(
                    f"{pname} '{v}' is not supported — this coverage is served in {srs} only "
                    "(reproject in your client)", status.HTTP_400_BAD_REQUEST)

        raw_bbox = _qp(request, "BBOX")
        if not raw_bbox:
            return _exception("GetCoverage requires BBOX (minx,miny,maxx,maxy in the native CRS)",
                              status.HTTP_400_BAD_REQUEST, code="MissingParameterValue")
        try:
            parts = [float(p) for p in raw_bbox.split(",")]
        except ValueError:
            parts = []
        if len(parts) != 4 or parts[0] >= parts[2] or parts[1] >= parts[3]:
            return _exception("BBOX must be minx,miny,maxx,maxy with min < max",
                              status.HTTP_400_BAD_REQUEST)
        bbox = (parts[0], parts[1], parts[2], parts[3])

        try:
            req_w, req_h = _parse_dims(request, bbox)
        except ValueError as exc:
            return _exception(str(exc), status.HTTP_400_BAD_REQUEST,
                              code="MissingParameterValue")

        try:
            path, out_w, out_h = await run_in_threadpool(
                coverage_to_geotiff, rec["object_key"], bbox, req_w, req_h,
                settings.raster_max_export_megapixels,
            )
        except ExportTooLarge as exc:
            return _exception(
                f"requested grid is {exc.native_width}x{exc.native_height} px, over the "
                f"{exc.cap_megapixels} MP cap — request a smaller window or coarser resolution",
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, code="InvalidParameterValue")
        except ExportEmpty:
            return _exception("BBOX does not overlap this coverage", status.HTTP_400_BAD_REQUEST)
        except (MediaStoreUnavailable, MediaObjectMissing) as exc:
            log.warning("wcs_store_unreachable", raster=rid, detail=str(exc))
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="raster store unreachable")
        except Exception as exc:  # noqa: BLE001 — a GDAL failure is a 500, temp cleaned up inside
            log.warning("wcs_coverage_failed", raster=rid, detail=str(exc))
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail="coverage read failed")
        log.info("wcs_coverage_served", raster=rid,
                 actor=str(principal.id) if principal else "anon", out_px=out_w * out_h)
        return FileResponse(
            path, media_type="image/tiff",
            # live values: never replayed from a desktop disk cache
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(_silent_unlink, path),
        )

    @router.get(mount, summary="WCS 1.0.0 KVP endpoint (GetCapabilities / DescribeCoverage / GetCoverage)",
                include_in_schema=in_schema)
    async def wcs_kvp(
        request: Request,
        conn: AsyncConnection = Depends(get_db),
        principal: User | None = Depends(get_optional_user),
    ) -> Response:
        guard(principal)
        version = _qp(request, "VERSION")
        if version is not None and version != _VERSION:
            return _exception(f"only WCS {_VERSION} is served", status.HTTP_400_BAD_REQUEST,
                              code="InvalidParameterValue")
        req = (_qp(request, "REQUEST") or "getcapabilities").lower()
        if req == "getcapabilities":
            return await _capabilities(request, conn, principal)
        if req == "describecoverage":
            return await _describe(request, conn, principal)
        if req == "getcoverage":
            return await _get_coverage(request, conn, principal)
        return _exception(f"unknown REQUEST '{req}'", status.HTTP_400_BAD_REQUEST,
                          code="OperationNotSupported")

    return router


def _silent_unlink(path: str) -> None:
    import os

    try:
        os.unlink(path)
    except OSError:
        pass


public_router = _build_wcs_router(PUBLIC_MOUNT, challenge=False)
auth_router = _build_wcs_router(AUTH_MOUNT, challenge=True)
