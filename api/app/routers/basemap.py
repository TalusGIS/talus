"""Basemap provisioning routes (decision-basemap-provisioning.md).

Two deliberate trust postures in one router:

- ANON READ — `GET /basemap` (status: is one installed, which build) and `GET /basemap/archive`
  (the bytes, Range-capable). Anonymous by DECISION, not accident (§5): the archive is a public
  OSM derivative and public-map embeds load it logged-out. The archive handler takes NO
  path/key/query input — it serves exactly one fixed object, so traversal is structurally
  impossible.
- ADMIN WRITE — `GET /admin/basemap` (full status), `POST /admin/basemap/fetch` (server-side
  go-pmtiles extract), `POST /admin/basemap/upload` (air-gapped walk-in), `PUT
  /admin/basemap/default` and `PUT /admin/basemap/streamed` (which basemap new maps open on, and
  which publisher-hosted maps the portal offers — names from closed lists, never an address;
  decision-streamed-basemap.md §14). All behind `_require_admin` (anon 401 / non-admin 403 —
  inv19, inv51).

The heavy lifting (Range parse, staging→promote, subprocess, advisory lock) lives in
`app.basemap`; this router stays validation + HTTP shape.
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile

import structlog
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from psycopg import AsyncConnection
from pydantic import BaseModel, Field, StrictBool, field_validator

from .. import admin_audit, basemap
from ..db import get_db
from ..deps import _require_admin
from ..media import MediaStoreUnavailable
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["basemap"])

_ARCHIVE_MEDIA_TYPE = "application/vnd.pmtiles"
_UPLOAD_CHUNK = 1024 * 1024  # 1 MiB stream chunks (the rasters-ingest pattern)
_DATE_RE = re.compile(r"^\d{8}$")


class FetchRequest(BaseModel):
    """Admin fetch parameters — every field validated to a narrow type BEFORE it can reach the
    subprocess argv (F3/F4). `date` is 'latest' or a pinned YYYYMMDD."""

    bbox: list[float] = Field(..., min_length=4, max_length=4)
    maxzoom: int = Field(default=basemap.DEFAULT_MAXZOOM, ge=1, le=basemap.MAX_MAXZOOM)
    date: str = Field(default="latest")

    @field_validator("bbox")
    @classmethod
    def _sane_wgs84(cls, v: list[float]) -> list[float]:
        # Shared with the org default extent (V094) — one range gate, two callers.
        return basemap.validate_wgs84_bbox(v)

    @field_validator("date")
    @classmethod
    def _pinned_or_latest(cls, v: str) -> str:
        v = v.strip()
        if v != "latest" and not _DATE_RE.match(v):
            raise ValueError("date must be 'latest' or YYYYMMDD")
        return v


class DefaultBasemapRequest(BaseModel):
    """The org-wide default basemap (V093 / decision-default-basemap.md fork A1). A CLOSED literal,
    not a free string: this value is handed to the client, which uses it to select a MapLibre style
    source. The DB CHECK is the backstop; this is the gate."""

    value: Literal["light", "dark", "grayscale", "imagery", "osm", "usgstopo", "osm-gray", "osm-dark"]


class StreamedBasemapsRequest(BaseModel):
    """Which streamed maps the portal offers (V115 / decision-streamed-basemap.md §14). A list of
    NAMES from a closed literal — never an address: the client turns a name into a tile source from
    its own constants, so nothing a caller sends here can steer a viewer's browser to a host of the
    caller's choosing. An empty list is valid and is the zero-external setting for map backgrounds."""

    value: list[Literal["osm", "usgstopo", "imagery"]] = Field(max_length=len(basemap.STREAMED_BASEMAP_VALUES) * 2)
    # V116 / decision-streamed-basemap.md §16 — whether the portal lists its DOWNLOADED looks
    # (Light, Dark, Gray). Omitted = leave as it is. STRICT: only a JSON true or false. A lax
    # boolean reads the string "no" as false, and this field decides whether a portal's maps may
    # reach an outside publisher at all — it is not quietly inferred from a string.
    downloaded: StrictBool | None = None

    @field_validator("value")
    @classmethod
    def _canonical(cls, v: list[str]) -> list[str]:
        return basemap.normalize_streamed(v)


# ---------------------------------------------------------------------------
# Anonymous read side
# ---------------------------------------------------------------------------

@router.get("/basemap", summary="Basemap status (anonymous) — is a basemap provisioned?")
async def basemap_status(conn: AsyncConnection = Depends(get_db)) -> dict[str, object]:
    """The SPA's runtime discovery (F2): a minimal anon projection. The client joins its own
    API base with `/basemap/archive` when installed — the server never guesses its public
    hostname (the multi-homed-rig lesson)."""
    meta = await basemap.read_metadata(conn)
    return {
        "installed": meta["installed"],
        "sourceDate": meta["sourceDate"],
        "fetchedAt": meta["fetchedAt"],
        # V093 — the new-map seed must reach ANONYMOUS viewers too (a public map and /embed/map/:id
        # both construct a basemap), so it rides this already-anonymous projection rather than
        # widening GET /org-settings. It names a basemap any viewer of a public map can already
        # see, so the projection stays minimal in substance as well as shape.
        "defaultBasemap": meta["defaultBasemap"],
        # V094 — org default extent + units. Units genuinely needs the anon ride (the embed scale
        # bar renders logged-out); the extent rides for ordering (the SPA gates map construction
        # on this fetch) and only ever fires on the authenticated /map route (decision doc §2.3).
        "defaultExtent": meta["defaultExtent"],
        "defaultUnits": meta["defaultUnits"],
        # V115 — which streamed (publisher-hosted) maps this portal offers. Anonymous because a
        # signed-out viewer of a public map draws a basemap too, and must not be handed a streamed
        # one the organization has withdrawn. Names from the closed list; never an address.
        "streamed": meta["streamed"],
        # V116 — whether this portal lists its downloaded looks. A switcher on a signed-out map
        # needs it; it says nothing a viewer of a public map cannot already see.
        "downloadedListed": meta["downloadedListed"],
    }


@router.api_route(
    "/basemap/archive",
    methods=["GET", "HEAD"],
    summary="The provisioned basemap archive (anonymous, Range-capable)",
)
async def basemap_archive(request: Request) -> Response:
    """Serve `basemaps/current.pmtiles` — the ONE fixed object (no key input exists). The
    pmtiles JS client reads it as many small single-range requests; the store does the offset
    seek. No Range => 200 streamed whole (a multi-GB object is never read into memory)."""
    stat = await run_in_threadpool(basemap.stat_archive)
    if stat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no basemap installed")
    size, etag = stat
    base_headers = {
        "Accept-Ranges": "bytes",
        "ETag": f'"{etag}"',
        # Short-lived cache: embeds get cheap repeat loads, while an Update (new archive, new
        # ETag) propagates without an SPA rebuild (acceptance §7.3).
        "Cache-Control": "public, max-age=300",
    }
    try:
        rng = basemap.parse_range(request.headers.get("range"), size)
    except ValueError:
        return Response(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={**base_headers, "Content-Range": f"bytes */{size}"},
        )
    if request.method == "HEAD":
        return Response(
            status_code=status.HTTP_200_OK,
            headers={**base_headers, "Content-Length": str(size)},
            media_type=_ARCHIVE_MEDIA_TYPE,
        )
    if rng is None:
        return StreamingResponse(
            basemap.iter_archive(),
            media_type=_ARCHIVE_MEDIA_TYPE,
            headers={**base_headers, "Content-Length": str(size)},
        )
    offset, length = rng
    # Streamed a piece at a time — the range is the (anonymous) caller's, and `bytes=0-` is the whole
    # archive (basemap.iter_archive_range). The FIRST piece is read here, before any header is sent,
    # so a store failure is still an honest 503 rather than a reply that dies mid-body.
    pieces = basemap.iter_archive_range(offset, length)
    try:
        first = await run_in_threadpool(next, pieces, b"")
    except MediaStoreUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store unavailable")

    def _body():
        yield first
        yield from pieces

    return StreamingResponse(
        _body(),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=_ARCHIVE_MEDIA_TYPE,
        headers={
            **base_headers,
            "Content-Range": f"bytes {offset}-{offset + length - 1}/{size}",
            "Content-Length": str(length),
        },
    )


# ---------------------------------------------------------------------------
# Admin write side
# ---------------------------------------------------------------------------

@router.get("/admin/basemap", summary="Basemap full status + job progress (admin only)")
async def admin_basemap_status(
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> dict[str, object]:
    meta = await basemap.read_metadata(conn)
    return {**meta, "job": basemap.job.snapshot()}


@router.put("/admin/basemap/default", summary="Set the org default basemap (admin only)")
async def set_default_basemap(
    payload: DefaultBasemapRequest,
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> dict[str, object]:
    """Seeds NEW maps only — a saved map restores its own basemap, and an unprovisioned box still
    cold-starts on imagery (decision-default-basemap.md forks B/D)."""
    try:
        await basemap.write_default_basemap(conn, value=payload.value, actor_id=admin.id)
    except basemap.BasemapNotOffered:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that basemap is not one this portal lists — list it first",
        )
    log.info("basemap_default_set", actor=str(admin.id), value=payload.value)
    return {"defaultBasemap": payload.value}


@router.put("/admin/basemap/streamed", summary="Set which streamed basemaps are offered (admin only)")
async def set_streamed_basemaps(
    payload: StreamedBasemapsRequest,
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> dict[str, object]:
    """Offering a streamed map means viewers' browsers — signed-out visitors to public maps
    included — fetch tiles from its publisher. That is an organization's decision about where its
    viewers' requests go, so it is admin-only, attributed, and recorded in the governance audit
    (the public-sharing toggle's reasoning). Withdrawing the map new maps open on resets that
    default to Light in the same write; the response carries both values."""
    try:
        result = await basemap.write_streamed_basemaps(
            conn, values=payload.value, actor_id=admin.id, downloaded=payload.downloaded
        )
    except basemap.NothingListed:
        # A portal must list something (V116). 422, not 409: the request itself asks for a state
        # that cannot exist, whatever the portal's current settings are.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="a portal must list at least one basemap — offer a streamed map or list the downloaded looks",
        )
    log.info("basemap_streamed_set", actor=str(admin.id), **result)
    await admin_audit.record(
        conn,
        actor_id=admin.id,
        action="streamed_basemaps_set",
        target_type="org_settings",
        target_id="org",
        detail=result,
    )
    return result


@router.post(
    "/admin/basemap/fetch",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Fetch a jurisdiction extract from the official Protomaps builds (admin only)",
)
async def start_fetch(
    payload: FetchRequest,
    admin: User = Depends(_require_admin),
) -> dict[str, object]:
    """Validate, resolve 'latest', then background the extract (202 + poll GET /admin/basemap).
    One operation at a time: same-process conflicts 409 here; cross-replica conflicts hit the
    advisory lock inside the job."""
    area = basemap.bbox_area_deg2(payload.bbox)
    if area > basemap.MAX_BBOX_AREA_DEG2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"bbox area {area:.1f} deg² exceeds the {basemap.MAX_BBOX_AREA_DEG2:.0f} deg² cap "
                "— provision a jurisdiction, not a continent (§6 fences)"
            ),
        )
    if basemap.job.state == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a provisioning operation is already running",
        )
    if payload.date == "latest":
        try:
            source_date = await basemap.resolve_latest_build()
        except Exception as exc:  # noqa: BLE001 — no egress / index down => actionable 502
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "could not reach the Protomaps builds index — no egress? "
                    "Air-gapped deployments use the upload path instead."
                ),
            ) from exc
    else:
        source_date = payload.date
    log.info(
        "basemap_fetch_requested", actor=str(admin.id),
        bbox=payload.bbox, maxzoom=payload.maxzoom, source_date=source_date,
    )
    asyncio.create_task(
        basemap.provision_fetch(
            bbox=payload.bbox, maxzoom=payload.maxzoom,
            source_date=source_date, actor_id=admin.id,
        )
    )
    return {"started": True, "sourceDate": source_date}


@router.post("/admin/basemap/upload", summary="Install an uploaded .pmtiles archive (admin only)")
async def upload_archive(
    request: Request,
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> dict[str, object]:
    """Air-gapped walk-in: raw request body (the rasters-ingest pattern — python-multipart-free),
    streamed to disk under the 4 GiB cap, validated in STAGING (magic bytes + v3 header), then
    promoted. A rejected upload never touches `current.pmtiles` (F7 / inv19 [6])."""
    fd, tmp_path = tempfile.mkstemp(suffix=".pmtiles")
    size = 0
    try:
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > basemap.UPLOAD_CAP_BYTES:
                        raise HTTPException(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="archive exceeds the 4 GiB upload limit",
                        )
                    out.write(chunk)
        except HTTPException:
            raise
        if size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="empty request body — POST the .pmtiles bytes as the request body",
            )
        try:
            meta = await basemap.promote_upload(conn, tmp_path, actor_id=admin.id)
        except basemap.NotAPmtiles as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        except basemap.BasemapBusy:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="a provisioning operation is already running",
            )
        except MediaStoreUnavailable:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store unavailable"
            )
        log.info("basemap_uploaded", actor=str(admin.id), size_bytes=size)
        return meta
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
