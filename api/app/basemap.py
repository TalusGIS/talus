"""Basemap provisioning — the portal-installed jurisdiction .pmtiles archive
(decision-basemap-provisioning.md).

One archive per deployment: `basemaps/current.pmtiles` in the MEDIA store (F1 — riding the
media store means the basemap survives DR restore with everything else). This module is the
domain seam: pmtiles header validation, Range parsing, the staging→promote dance, the
go-pmtiles extract subprocess, and the single-operation guard. The router
(routers/basemap.py) stays thin over it.

Concurrency: ONE provisioning operation at a time, enforced with a Postgres session advisory
lock (the T39/batch-worker pattern) so the guard holds even scaled out. Progress is
module-level in-memory state — accurate on the replica that runs the job (the dev/prod
compose runs one api), and the lock keeps other replicas from starting a second job either
way.

SSRF posture (F4): the fetch subprocess only ever receives a URL CONSTRUCTED here against
the two pinned official hosts — `build.protomaps.com` (archives) and
`build-metadata.protomaps.dev` (the builds index used to resolve "latest"). The admin
supplies at most a `YYYYMMDD` date, regex-pinned before it touches the URL.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import struct
import tempfile
import time
from dataclasses import dataclass

import httpx
import structlog
from psycopg import AsyncConnection

from .config import settings
from .db import get_pool
from .media import MediaStoreUnavailable
from .storage import MediaObjectMissing, get_media_storage

log = structlog.get_logger(__name__)

OBJECT_KEY = "basemaps/current.pmtiles"
STAGING_KEY = "basemaps/staging.pmtiles"

BUILD_HOST = "https://build.protomaps.com"
BUILDS_INDEX_URL = "https://build-metadata.protomaps.dev/builds.json"

MAX_MAXZOOM = 15
DEFAULT_MAXZOOM = 15
# V093 / decision-default-basemap.md fork A1 — the basemap a NEW map opens on, org-wide. These are
# exactly the four the switcher offers (web/src/features/map/BasemapSwitcher.tsx `Basemap`) and
# exactly the V093 CHECK; there is deliberately no "match the user's theme" value. FALLBACK is what
# an absent settings row resolves to, mirroring the column's own DEFAULT.
#
# V115 / decision-streamed-basemap.md §14 widened it: three DOWNLOADED looks (drawn from the org's
# own archive) and three STREAMED maps (each viewer's browser fetches them from the publisher —
# OpenStreetMap, USGS). These are NAMES. The tile addresses are constants in the web client; the
# api never stores, accepts or returns one, which is the whole of the slice's security answer.
DOWNLOADED_BASEMAP_VALUES = ("light", "dark", "grayscale")
# The streamed PUBLISHERS a portal may offer — canonical order, as stored and returned. This is
# the offered list's vocabulary and it stays three entries.
STREAMED_BASEMAP_VALUES = ("osm", "usgstopo", "imagery")
# V116 / decision-streamed-basemap.md §16 — streamed LOOKS, each belonging to one publisher. A
# look is what a map names; a publisher is what an organization offers. 'osm-gray' and 'osm-dark'
# are the standard OpenStreetMap tiles drawn gray or dark by the viewer's browser: the same
# publisher and the same tile address as 'osm', so they are offered exactly when 'osm' is and add
# no entry to the offered list. Canonical order (the order a switcher lists them in).
STREAMED_LOOK_PUBLISHER = {
    "osm": "osm",
    "osm-gray": "osm",
    "osm-dark": "osm",
    "usgstopo": "usgstopo",
    "imagery": "imagery",
}
STREAMED_LOOK_VALUES = tuple(STREAMED_LOOK_PUBLISHER)
DEFAULT_BASEMAP_VALUES = DOWNLOADED_BASEMAP_VALUES + STREAMED_LOOK_VALUES
DEFAULT_BASEMAP_FALLBACK = "light"
# V116 — whether a portal LISTS its downloaded looks. What a settings row that predates V116 (or
# is absent) resolves to: listed, which is what every install did before it.
DOWNLOADED_LISTED_FALLBACK = True
# What a settings row that predates V115 (or is absent) resolves to: exactly what every install
# offered before the slice. An upgrade never starts offering a new streamed map on its own.
STREAMED_BASEMAPS_FALLBACK = ("imagery",)
# V094 / decision-org-default-extent.md fork C — units for the EMBED scale bar (its only consumer;
# the main map shows a unitless ratio). Metric factory default; the router's Literal is the gate
# and the column CHECK the backstop, same three-layer shape as the basemap enum above.
MAP_UNITS_VALUES = ("metric", "imperial")
MAP_UNITS_FALLBACK = "metric"
UPLOAD_CAP_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB (F7)
# F7 hard cap — roughly a state-sized bbox. 64 deg² ≈ a large state with margin; refined against real
# extract measurements if the field proves it wrong. The SOFT county-scale warn lives in the UI.
MAX_BBOX_AREA_DEG2 = 64.0
EXTRACT_TIMEOUT_S = 600  # bounded runtime (F3)
EXTRACT_MIN_FREE_BYTES = 8 * 1024 * 1024 * 1024  # disk precheck before an extract lands in tmp

# Distinct advisory-lock key — basemap provisioning is its own subsystem (maintenance holds
# 3_900_000_039, the geocode batch worker 8_100_000_081).
_ADVISORY_LOCK_KEY = 8_400_000_084

_PMTILES_MAGIC = b"PMTiles"
_HEADER_LEN = 127


class BasemapBusy(Exception):
    """A provisioning operation is already running (advisory lock held)."""


class BasemapNotOffered(Exception):
    """The default named a basemap this portal does not list (V115, widened by V116): a streamed
    look whose publisher is not offered, or a downloaded look when those are not listed. The
    router's 409."""


class NothingListed(Exception):
    """A write would leave the portal listing no basemap at all — no streamed publisher offered
    and the downloaded looks not listed (V116). The router's 422."""


def publisher_of(name: str) -> str | None:
    """The streamed publisher a basemap name belongs to, or None for a downloaded look (or an
    unknown name). Pure; the one home of the look-to-publisher rule on the server."""
    return STREAMED_LOOK_PUBLISHER.get(name)


def is_listed(name: str, *, downloaded_listed: bool, streamed: object) -> bool:
    """Whether a portal with these two settings lists `name`: a downloaded look needs the
    downloaded looks listed, a streamed look needs its publisher offered. Pure."""
    if name in DOWNLOADED_BASEMAP_VALUES:
        return bool(downloaded_listed)
    publisher = publisher_of(name)
    return publisher is not None and publisher in (streamed or ())


def first_listed(*, downloaded_listed: bool, streamed: object) -> str:
    """What "new maps open on" falls to when its map stops being listed: Light when the
    downloaded looks are listed (the pre-V116 rule, so an upgraded install behaves as before),
    otherwise the first offered publisher's own look. Raises NothingListed when there is none."""
    if downloaded_listed:
        return DEFAULT_BASEMAP_FALLBACK
    for name in STREAMED_BASEMAP_VALUES:
        if name in (streamed or ()):
            return name
    raise NothingListed()


def plan_listing(
    *, current_default: str, current_listed: bool, values: list[str], downloaded: bool | None
) -> tuple[bool, str]:
    """The settings a listing write results in: (downloaded_listed, default_basemap). `downloaded`
    None leaves that setting as it is (the V115 caller, which sends only the offered list). The
    default is kept when the portal still lists it and otherwise becomes first_listed — so
    choosing Downloaded, or Streamed, or unticking a publisher is ONE write that never leaves
    "new maps open on" naming a map the switcher will not show. Raises NothingListed. Pure, so
    the rule is tested without a database."""
    listed = current_listed if downloaded is None else bool(downloaded)
    if not listed and not values:
        raise NothingListed()
    if is_listed(current_default, downloaded_listed=listed, streamed=values):
        return listed, current_default
    return listed, first_listed(downloaded_listed=listed, streamed=values)


def normalize_streamed(values: object) -> list[str]:
    """A streamed-maps selection as the canonical list: known names only, no duplicates, in
    STREAMED_BASEMAP_VALUES order. Raises ValueError on anything else — a tile ADDRESS included.
    Pure, so the closed-list rule has one home the router, the writer and the tests all share."""
    if not isinstance(values, (list, tuple)):
        raise ValueError("streamed basemaps must be a list of names")
    seen = set()
    for v in values:
        if not isinstance(v, str) or v not in STREAMED_BASEMAP_VALUES:
            raise ValueError(f"unknown streamed basemap {v!r}; allowed: {list(STREAMED_BASEMAP_VALUES)}")
        seen.add(v)
    return [v for v in STREAMED_BASEMAP_VALUES if v in seen]


class NotAPmtiles(Exception):
    """The uploaded/produced file is not a PMTiles v3 archive."""


@dataclass(frozen=True)
class PmtilesHeader:
    """The fields the metadata write wants from a v3 header (spec: PMTiles v3, 127-byte header)."""

    min_zoom: int
    max_zoom: int
    bbox: list[float]  # [w, s, e, n] WGS84 (stored E7 in the header)


@dataclass
class JobState:
    """In-memory progress of the current/most-recent provisioning job (single api process)."""

    state: str = "idle"  # idle | running | done | failed
    kind: str | None = None  # fetch | upload
    message: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self.state,
            "kind": self.kind,
            "message": self.message,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
        }


job = JobState()


def parse_pmtiles_header(path: str) -> PmtilesHeader:
    """Validate magic bytes + version and read zooms/bounds from a PMTiles v3 header.
    Raises NotAPmtiles on anything that isn't a v3 archive — the staging gate (F7)."""
    with open(path, "rb") as f:
        head = f.read(_HEADER_LEN)
    if len(head) < _HEADER_LEN or head[:7] != _PMTILES_MAGIC:
        raise NotAPmtiles("not a PMTiles archive (bad magic bytes)")
    version = head[7]
    if version != 3:
        raise NotAPmtiles(f"unsupported PMTiles version {version} (need v3)")
    min_zoom = head[100]
    max_zoom = head[101]
    min_lon_e7, min_lat_e7, max_lon_e7, max_lat_e7 = struct.unpack_from("<iiii", head, 102)
    return PmtilesHeader(
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        bbox=[min_lon_e7 / 1e7, min_lat_e7 / 1e7, max_lon_e7 / 1e7, max_lat_e7 / 1e7],
    )


# ---------------------------------------------------------------------------
# Range parsing — the archive is consumed by the pmtiles JS client, which reads
# it via many small single-range requests (bytes=a-b). Single range only.
# ---------------------------------------------------------------------------

def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single-range `Range: bytes=...` header against an object of `size` bytes.
    Returns (offset, length), None when no/blank header (=> 200 full body). Raises ValueError
    on a malformed or unsatisfiable range (=> 416). Multi-range is unsupported (ValueError)."""
    if not header:
        return None
    header = header.strip()
    if not header.startswith("bytes="):
        raise ValueError("unsupported range unit")
    spec = header[len("bytes="):]
    if "," in spec:
        raise ValueError("multi-range not supported")
    start_s, _, end_s = spec.partition("-")
    start_s, end_s = start_s.strip(), end_s.strip()
    if start_s == "" and end_s == "":
        raise ValueError("empty range")
    if start_s == "":
        # suffix range: last N bytes
        n = int(end_s)
        if n <= 0:
            raise ValueError("bad suffix length")
        length = min(n, size)
        return size - length, length
    start = int(start_s)
    if start >= size:
        raise ValueError("range start beyond end of object")
    if end_s == "":
        return start, size - start
    end = int(end_s)
    if end < start:
        raise ValueError("range end before start")
    end = min(end, size - 1)
    return start, end - start + 1


# ---------------------------------------------------------------------------
# Storage — all BLOCKING (call via run_in_threadpool), mirroring app.media.
# The router's error contract is preserved exactly: everything below raises
# MediaStoreUnavailable (503) — a missing archive mid-operation after a
# successful stat_archive() is store weirdness, not a 404.
# ---------------------------------------------------------------------------

def stat_archive() -> tuple[int, str] | None:
    """(size, etag) of the current archive, or None when no basemap is provisioned."""
    try:
        st = get_media_storage().stat(OBJECT_KEY)
        return st.size, st.etag
    except MediaObjectMissing:
        return None


def read_archive_range(offset: int, length: int) -> bytes:
    """Read one byte range of the archive INTO MEMORY. Callers pass a bounded length — the archive
    door reads through iter_archive_range, which never asks for more than one piece."""
    try:
        return get_media_storage().read_range(OBJECT_KEY, offset, length)
    except MediaObjectMissing as exc:
        raise MediaStoreUnavailable(str(exc)) from exc


# One piece of a ranged reply. The pmtiles client asks for KBs at a time, so a real request is one
# piece; the bound exists for the request that is not real (see iter_archive_range).
RANGE_PIECE_BYTES = 1024 * 1024


def iter_archive_range(offset: int, length: int, piece: int = RANGE_PIECE_BYTES):
    """Yield `length` bytes of the archive from `offset`, never reading more than `piece` at once.

    The archive door is ANONYMOUS and the range is the caller's: `Range: bytes=0-` asks for the whole
    object, and the first version answered with ONE read of it — up to 4 GiB into the api's memory
    per request (decision-once-over-security-fixes.md A6). The reply is the same bytes; it is only
    never held whole. A short read (the object was replaced by a smaller one mid-stream) ends the
    stream early rather than looping. BLOCKING — iterate off the event loop."""
    remaining = length
    while remaining > 0:
        chunk = read_archive_range(offset, min(piece, remaining))
        if not chunk:
            return
        yield chunk
        offset += len(chunk)
        remaining -= len(chunk)


def iter_archive(chunk_size: int = 1024 * 1024):
    """Yield the whole archive in chunks — the no-Range 200 path (a multi-GB object must
    stream, never be read whole). Runs inside a threadpool iteration (StreamingResponse
    accepts a sync generator and iterates it off the event loop)."""
    try:
        return get_media_storage().iter_chunks(OBJECT_KEY, chunk_size)
    except MediaObjectMissing as exc:
        raise MediaStoreUnavailable(str(exc)) from exc


def promote_staging(size: int) -> None:
    """staging -> current, then drop staging. Never validate-in-place (F7): a rejected
    upload dies on the staging key without ever touching the serving path. On the
    filesystem backend this is one atomic os.replace — strictly better than the S3
    copy-then-delete it replaces (a reader sees old bytes or new, never neither)."""
    try:
        get_media_storage().replace(STAGING_KEY, OBJECT_KEY)
    except MediaObjectMissing as exc:
        raise MediaStoreUnavailable(str(exc)) from exc
    log.info("basemap_promoted", size_bytes=size)


def put_staging(path: str) -> None:
    """Upload a validated local file to the staging key (streamed from disk)."""
    get_media_storage().write_file(STAGING_KEY, path, content_type="application/vnd.pmtiles")


def drop_staging() -> None:
    """Best-effort cleanup of the staging key (idempotent; absent key is fine)."""
    try:
        get_media_storage().delete(STAGING_KEY)
    except Exception:  # cleanup must never mask the original error
        pass


# ---------------------------------------------------------------------------
# Single-operation guard — Postgres session advisory lock (the batch-worker
# discipline: the JOB owns the lock lifecycle on its own pool connection, and a
# connection whose unlock fails is CLOSED, never returned to the pool locked).
# ---------------------------------------------------------------------------

async def _try_lock(conn: AsyncConnection) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        row = await cur.fetchone()
    return bool(row and row[0])


async def _unlock(conn: AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))


# ---------------------------------------------------------------------------
# Metadata — the V084 org_settings columns, written as ONE provisioning event.
# ---------------------------------------------------------------------------

async def read_metadata(conn: AsyncConnection) -> dict[str, object]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT basemap_bbox, basemap_maxzoom, basemap_source_date, basemap_fetched_at, "
            "basemap_size_bytes, basemap_origin, default_basemap, default_extent, default_units, "
            "streamed_basemaps, downloaded_basemaps_listed "
            "FROM platform.org_settings WHERE id = true"
        )
        row = await cur.fetchone()
    (bbox, maxzoom, source_date, fetched_at, size_bytes, origin, default_basemap,
     default_extent, default_units, streamed, downloaded_listed) = row or (None,) * 11
    # V115 — the streamed maps this portal offers. Filtered through the closed list on the way OUT
    # as well as in: the column's CHECK already guarantees it, and this keeps the anonymous payload
    # a list of known names even if a row were ever written around the api.
    offered = [v for v in STREAMED_BASEMAP_VALUES
               if v in (streamed if streamed is not None else STREAMED_BASEMAPS_FALLBACK)]
    return {
        "installed": fetched_at is not None,
        "bbox": bbox,
        "maxzoom": maxzoom,
        "sourceDate": source_date,
        "fetchedAt": fetched_at.isoformat() if fetched_at else None,
        "sizeBytes": size_bytes,
        "origin": origin,
        # V093 — unrelated to provisioning (it is a display preference, not archive metadata), but
        # it rides this one singleton SELECT rather than adding a second round-trip on a path the
        # anonymous GET /basemap already walks on every map load.
        "defaultBasemap": default_basemap or DEFAULT_BASEMAP_FALLBACK,
        # V094 — like defaultBasemap, org display preferences riding the one singleton SELECT the
        # anonymous GET /basemap already walks. The extent only ever *fires* on the authenticated
        # /map route (every saved map owns its view — decision doc §2.3); it rides the anon payload
        # for ordering (map construction gates on this fetch), and the jurisdiction bbox is already
        # anon-readable from the archive header regardless. NULL extent = unset (coverage fit).
        "defaultExtent": default_extent,
        "defaultUnits": default_units or MAP_UNITS_FALLBACK,
        # V115 — rides the anonymous payload because every map surface, signed-out ones included,
        # must know which streamed maps it may draw before it builds a basemap. Names only.
        "streamed": offered,
        # V116 — whether this portal lists its downloaded looks (Light, Dark, Gray). Anonymous for
        # the same reason: a signed-out map surface builds a switcher too. It tells a viewer
        # nothing they cannot see by opening the switcher on a public map.
        "downloadedListed": (
            DOWNLOADED_LISTED_FALLBACK if downloaded_listed is None else bool(downloaded_listed)
        ),
    }


async def write_metadata(
    conn: AsyncConnection,
    *,
    bbox: list[float],
    maxzoom: int,
    source_date: str | None,
    size_bytes: int,
    origin: str,
    actor_id: object,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.org_settings SET "
            "basemap_bbox = %s::jsonb, basemap_maxzoom = %s, basemap_source_date = %s, "
            "basemap_fetched_at = now(), basemap_size_bytes = %s, basemap_origin = %s, "
            # V094 fork F auto-seed: a fresh install's first provision answers "where does the map
            # open" with the bbox it just extracted/read — but ONLY when unset, or a routine
            # re-fetch (the staleness nudge tells admins to!) would silently move everyone's
            # opening view. COALESCE expresses seed-only-when-NULL in one clause, race-free.
            "default_extent = COALESCE(default_extent, %s::jsonb), "
            "updated_by = %s, updated_at = now() WHERE id = true",
            (json.dumps(bbox), maxzoom, source_date, size_bytes, origin,
             json.dumps(bbox), actor_id),
        )
    await conn.commit()


async def write_default_basemap(
    conn: AsyncConnection, *, value: str, actor_id: object
) -> None:
    """Set the org-wide default basemap (V093). Separate from write_metadata on purpose: that
    writes a provisioning EVENT as one atomic group, this is an independent display preference an
    admin flips at will. `value` must already be one of DEFAULT_BASEMAP_VALUES — the router's
    Literal is the gate, and the column's CHECK is the backstop.

    V115: a STREAMED default must be one the portal offers, or the switcher would be told to open
    on a map it does not show. Checked in the UPDATE's own WHERE so it cannot race a change to the
    offered list; no row updated => BasemapNotOffered (the V115 cross-column CHECK is the backstop).

    V116: the rule is now "a default must be a map the portal LISTS". A downloaded look needs
    downloaded_basemaps_listed; a streamed look needs its PUBLISHER offered (osm-gray and osm-dark
    belong to osm). For a downloaded look the publisher parameter is NULL, and NULL = ANY(...) is
    not true, so that branch cannot pass it."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.org_settings SET "
            "default_basemap = %s, updated_by = %s, updated_at = now() "
            "WHERE id = true AND ("
            "(%s = ANY(%s::text[]) AND downloaded_basemaps_listed) "
            "OR %s::text = ANY(streamed_basemaps))",
            (value, actor_id, value, list(DOWNLOADED_BASEMAP_VALUES), publisher_of(value)),
        )
        updated = cur.rowcount
    if not updated:
        await conn.rollback()
        raise BasemapNotOffered(value)
    await conn.commit()


async def write_streamed_basemaps(
    conn: AsyncConnection, *, values: list[str], actor_id: object, downloaded: bool | None = None
) -> dict[str, object]:
    """Set what the portal LISTS: which streamed publishers it offers (V115) and, when
    `downloaded` is given, whether it lists its downloaded looks (V116). `values` must already be
    normalized (normalize_streamed) — the router's Literal is the gate, the column CHECK the
    backstop. Both settings move in ONE write, so choosing Downloaded (list the looks, offer
    nothing — the setting for no external map request) or Streamed is one act and one audit row.

    If the map new maps open on stops being listed, the default moves IN THE SAME TRANSACTION to
    the first map the portal still lists (plan_listing): a default left naming a map the switcher
    will not show would violate the cross-column CHECK. The settings row is read FOR UPDATE first,
    so the decision cannot race another write. Raises NothingListed when the result would list no
    basemap at all. Returns the resulting settings so the caller can show and audit them."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT default_basemap, downloaded_basemaps_listed "
            "FROM platform.org_settings WHERE id = true FOR UPDATE"
        )
        row = await cur.fetchone()
        current_default, current_listed = row or (DEFAULT_BASEMAP_FALLBACK, DOWNLOADED_LISTED_FALLBACK)
        try:
            listed, default_basemap = plan_listing(
                current_default=current_default or DEFAULT_BASEMAP_FALLBACK,
                current_listed=DOWNLOADED_LISTED_FALLBACK if current_listed is None else current_listed,
                values=values,
                downloaded=downloaded,
            )
        except NothingListed:
            await conn.rollback()
            raise
        await cur.execute(
            "UPDATE platform.org_settings SET "
            "streamed_basemaps = %s::text[], downloaded_basemaps_listed = %s, default_basemap = %s, "
            "updated_by = %s, updated_at = now() WHERE id = true",
            (values, listed, default_basemap, actor_id),
        )
    await conn.commit()
    return {
        "streamed": [v for v in STREAMED_BASEMAP_VALUES if v in values],
        "downloadedListed": listed,
        "defaultBasemap": default_basemap,
    }


# ---------------------------------------------------------------------------
# Fetch — resolve the build, run the bundled go-pmtiles extract, stage, promote.
# ---------------------------------------------------------------------------

async def resolve_latest_build() -> str:
    """Latest official build date (YYYYMMDD) from the pinned builds index."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        resp = await client.get(BUILDS_INDEX_URL)
        resp.raise_for_status()
        builds = resp.json()
    dates = sorted(
        m.group(1)
        for b in builds
        if isinstance(b, dict)
        for m in [re.match(r"^(\d{8})\.pmtiles$", str(b.get("key", "")))]
        if m
    )
    if not dates:
        raise RuntimeError("builds index returned no dated builds")
    return dates[-1]


def validate_wgs84_bbox(v: list[float]) -> list[float]:
    """Range + ordering gate for a [w, s, e, n] WGS84 bbox. ONE copy, two callers — the fetch
    request and the org default extent (V094) — because two copies of a range check is how they
    drift. Raises ValueError in pydantic-validator style; the V084/V094 jsonb CHECKs are the
    shape-only DB backstop."""
    w, s, e, n = v
    if not (-180.0 <= w < e <= 180.0):
        raise ValueError("bbox longitudes must satisfy -180 <= west < east <= 180")
    if not (-85.06 <= s < n <= 85.06):
        raise ValueError("bbox latitudes must satisfy -85.06 <= south < north <= 85.06")
    return v


def bbox_area_deg2(bbox: list[float]) -> float:
    w, s, e, n = bbox
    return max(0.0, e - w) * max(0.0, n - s)


def _disk_precheck() -> None:
    free = shutil.disk_usage(tempfile.gettempdir()).free
    if free < EXTRACT_MIN_FREE_BYTES:
        raise RuntimeError(
            f"not enough temp disk for an extract ({free // (1024**3)} GiB free, "
            f"{EXTRACT_MIN_FREE_BYTES // (1024**3)} GiB required)"
        )


async def run_extract(bbox: list[float], maxzoom: int, source_date: str, out_path: str) -> None:
    """Run the bundled go-pmtiles binary: argv only, no shell, every input validated to a narrow
    type by the caller (bbox = 4 floats, date =~ ^\\d{8}$, maxzoom = int 1..15), bounded runtime."""
    url = f"{BUILD_HOST}/{source_date}.pmtiles"  # CONSTRUCTED — the only URL shape possible (F4)
    bbox_arg = ",".join(f"{v:.7f}" for v in bbox)
    argv = [
        "pmtiles", "extract", url, out_path,
        f"--bbox={bbox_arg}", f"--maxzoom={maxzoom}",
    ]
    log.info("basemap_extract_start", source_date=source_date, bbox=bbox_arg, maxzoom=maxzoom)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=EXTRACT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"extract exceeded the {EXTRACT_TIMEOUT_S}s limit and was stopped")
    if proc.returncode != 0:
        tail = (out or b"")[-2000:].decode("utf-8", "replace")
        raise RuntimeError(f"go-pmtiles extract failed (rc={proc.returncode}): {tail}")


async def provision_fetch(
    *,
    bbox: list[float],
    maxzoom: int,
    source_date: str,
    actor_id: object,
) -> None:
    """The background fetch job (asyncio.create_task from the router). Owns the whole lifecycle:
    advisory lock on its own pool connection, temp file, extract, staging→promote, metadata
    write, job state. Fail-safe — every failure lands in job.state='failed', never an unhandled
    task exception."""
    from fastapi.concurrency import run_in_threadpool

    job.state, job.kind, job.message = "running", "fetch", f"extracting build {source_date}"
    job.started_at, job.finished_at = time.time(), None
    fd, tmp_path = tempfile.mkstemp(suffix=".pmtiles")
    os.close(fd)
    try:
        async with get_pool().connection() as conn:
            if not await _try_lock(conn):
                # Another replica is provisioning (same-process conflicts were 409'd sync).
                raise RuntimeError("another provisioning operation is already running")
            try:
                _disk_precheck()
                await run_extract(bbox, maxzoom, source_date, tmp_path)
                header = await run_in_threadpool(parse_pmtiles_header, tmp_path)
                size = os.path.getsize(tmp_path)
                job.message = "storing archive"
                await run_in_threadpool(put_staging, tmp_path)
                await run_in_threadpool(promote_staging, size)
                await write_metadata(
                    conn, bbox=bbox, maxzoom=header.max_zoom, source_date=source_date,
                    size_bytes=size, origin="fetched", actor_id=actor_id,
                )
                job.state, job.message = (
                    "done", f"installed build {source_date} ({size // (1024 * 1024)} MB)",
                )
                log.info("basemap_fetch_done", source_date=source_date, size_bytes=size)
            finally:
                try:
                    await _unlock(conn)
                except Exception:  # noqa: BLE001 — never return a lock-holding conn to the pool
                    await conn.close()
                    log.warning("basemap_unlock_failed_conn_closed", exc_info=True)
    except Exception as exc:  # noqa: BLE001 — terminal job state, never an orphaned task error
        job.state, job.message = "failed", str(exc)
        log.warning("basemap_fetch_failed", error=str(exc))
        await run_in_threadpool(drop_staging)
    finally:
        job.finished_at = time.time()
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def promote_upload(
    conn: AsyncConnection,
    tmp_path: str,
    *,
    actor_id: object,
) -> dict[str, object]:
    """The upload path's staging dance, SYNCHRONOUS in the request (the archive is already
    local — there is no long extract to background). The caller has gated admin and streamed
    the body to tmp_path under the size cap; lock handling lives HERE, mirroring
    provision_fetch: try-lock on this request's connection, BasemapBusy (=> 409) when a fetch
    holds it. Validates the header (NotAPmtiles => 400 on garbage), stages, promotes, writes
    metadata; returns the fresh metadata dict."""
    from fastapi.concurrency import run_in_threadpool

    if not await _try_lock(conn):
        raise BasemapBusy()
    job.state, job.kind, job.message = "running", "upload", "validating upload"
    job.started_at, job.finished_at = time.time(), None
    try:
        header = await run_in_threadpool(parse_pmtiles_header, tmp_path)  # magic-bytes gate (F7)
        size = os.path.getsize(tmp_path)
        job.message = "storing archive"
        await run_in_threadpool(put_staging, tmp_path)
        await run_in_threadpool(promote_staging, size)
        await write_metadata(
            conn, bbox=header.bbox, maxzoom=header.max_zoom, source_date=None,
            size_bytes=size, origin="uploaded", actor_id=actor_id,
        )
        job.state, job.message = "done", f"uploaded archive installed ({size // (1024 * 1024)} MB)"
        log.info("basemap_upload_done", size_bytes=size)
        return await read_metadata(conn)
    except NotAPmtiles:
        job.state, job.message = "idle", None  # a rejected upload is a 400, not a failed job
        raise
    except Exception as exc:  # noqa: BLE001
        job.state, job.message = "failed", str(exc)
        await run_in_threadpool(drop_staging)
        raise
    finally:
        job.finished_at = time.time()
        try:
            await _unlock(conn)
        except Exception:  # noqa: BLE001
            await conn.close()
            log.warning("basemap_unlock_failed_conn_closed", exc_info=True)
