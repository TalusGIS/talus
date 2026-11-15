import asyncio
from typing import Awaitable, Callable
import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from .authz import assert_openfga_model_live, assert_openfga_store_live
from .config import settings
from .db import close_pool, get_pool, open_pool
from .geocoders import seed_geocoders_from_env_if_unseeded
from .body_limit import RequestBodyLimitMiddleware
from .logging_config import RequestIDMiddleware, configure_logging
from .maintenance import run_scheduler_loop
from .geocode_batch_worker import run_geocode_worker_loop
from .import_batch_worker import run_import_worker_loop
from .sqlsource.worker import run_sql_pull_worker_loop
from .recent_warnings_store import run_recent_warnings_flush_loop
from .raster_cog import StoredRasterRefused
from .ratelimit import AnonRateLimitMiddleware
from .security_headers import SecurityHeadersMiddleware
from .storage import get_media_storage, get_raster_storage
from .routers import (
    attachments,
    backups,
    basemap,
    custom_domains,
    entitlement_admin,
    external_refs,
    field_projects,
    geocode,
    geocode_batch,
    geocoders,
    geoservices,
    groups,
    health,
    home,
    import_jobs,
    layer_features,
    layer_fields,
    layer_forms,
    layer_query,
    layer_shares,
    layer_views,
    maintenance,
    me,
    metadata,
    ogc,
    open_data,
    org_settings,
    ownership,
    pats,
    publisher,
    rasters,
    relates,
    reports,
    search,
    service_accounts,
    sql_sources,
    symbology,
    system_health,
    tiles,
    tiles_dyn,
    usage,
    users,
    wcs,
    web_maps,
    wmts,
)

# Configure logging at import time — runs when uvicorn imports app.main, i.e.
# after uvicorn has applied its own logging config, so our reclaim of the
# uvicorn.* loggers wins.
configure_logging()

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await open_pool()
    try:
        # Fail-closed boot guard: refuse to serve if the configured OpenFGA store/model
        # is blank/stale/unreachable, instead of silently 503-ing on the first gated
        # request. Store FIRST, then model (a model check is meaningless if the store is
        # gone). Either raises OpenFGABootError -> lifespan startup fails -> app does not
        # serve. (close_pool below still runs via finally.)
        await assert_openfga_store_live()
        await assert_openfga_model_live()
        # Storage roots (decision-object-store-build.md B3): mkdir -p the media/raster
        # subtrees so a fresh install's health probe is green before the first byte is
        # written (the make_bucket-at-boot equivalent). The BACKUP target is deliberately
        # NOT ensured here — build_backup_storage raises on a partial S3 config, and the
        # destination is runtime-editable (V090); that error belongs to the backup job
        # (a visible error row), not to boot.
        await run_in_threadpool(get_media_storage().ensure_root)
        await run_in_threadpool(get_raster_storage().ensure_root)
        # One-time env GEOCODERS -> platform.geocoders backfill (Talus 37 F5). A convenience, NOT a
        # gate: on failure we log and serve anyway (the marker stays false, retried next boot), so a
        # transient seed error never blocks startup. No-op once seeded.
        try:
            async with get_pool().connection() as conn:
                seeded = await seed_geocoders_from_env_if_unseeded(conn)
            if seeded:
                log.info("geocoders_seeded_from_env", count=seeded)
        except Exception:
            log.warning("geocoders_seed_failed", exc_info=True)
        # Self-maintenance scheduler (Talus 39). A lifespan-managed asyncio loop that auto-expires
        # cruft (idempotency keys, orphaned blobs) — guarded by a Postgres advisory lock so exactly one
        # replica runs a job even when the api is scaled out. Default ON; MAINTENANCE_ENABLED=false
        # disables it (e.g. a migration window, or running jobs out-of-band). Started AFTER the boot
        # guard + seed so it only runs once we're committed to serving. See app/maintenance.py.
        scheduler_task: asyncio.Task[None] | None = None
        if settings.maintenance_enabled:
            scheduler_task = asyncio.create_task(run_scheduler_loop())
        # Batch-geocoding async worker (decision-batch-geocoding.md F5). A SEPARATE advisory-locked loop
        # from maintenance — it geocodes queued jobs into authoritative features, so it lives outside the
        # maintenance cruft fence. Default ON; GEOCODE_WORKER_ENABLED=false pauses processing.
        geocode_worker_task: asyncio.Task[None] | None = None
        if settings.geocode_worker_enabled:
            geocode_worker_task = asyncio.create_task(run_geocode_worker_loop())
        # Bulk-import async worker (decision-9d-bulk-import.md §4) — its own advisory-locked
        # loop; writes authoritative features (imported rows), so outside the cruft fence.
        import_worker_task: asyncio.Task[None] | None = None
        if settings.import_worker_enabled:
            import_worker_task = asyncio.create_task(run_import_worker_loop())
        # SQL-source pull worker (decision-sql-source-pull.md §3) — the fourth job class, its own
        # advisory lock; writes pulled copies, so outside the cruft fence like the other two.
        sql_pull_worker_task: asyncio.Task[None] | None = None
        if settings.sql_pull_worker_enabled:
            sql_pull_worker_task = asyncio.create_task(run_sql_pull_worker_loop())
        # The recent-warnings persister (V114): copies the WARNING+ ring to platform.recent_warnings so
        # the box that crashed is still the box that can be asked about. No lock — each replica flushes
        # its own ring under its own boot id. Best effort by design (see recent_warnings_store.py).
        warnings_flush_task: asyncio.Task[None] | None = None
        if settings.recent_warnings_persist_enabled:
            warnings_flush_task = asyncio.create_task(run_recent_warnings_flush_loop())
        # Extension-module background loops (the mounting seam's lifecycle half): started after
        # core's own workers, cancelled with them. Each is advisory-locked by its own module.
        extension_tasks: list[asyncio.Task[None]] = [asyncio.create_task(loop()) for loop in _extension_loops]
        try:
            yield
        finally:
            for task in (scheduler_task, geocode_worker_task, import_worker_task, sql_pull_worker_task,
                         warnings_flush_task, *extension_tasks):
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
    finally:
        await close_pool()


app = FastAPI(
    title="Talus GIS API",
    version="1.0",
    description=(
        "Talus GIS portal API. The token-authenticated product surface is served under "
        "**/api/v1** — an OGC-API-Features-style layer-query + feature-CRUD REST API. "
        "Authenticate non-interactively with a service account via the OAuth "
        "client-credentials grant (see docs/api-guide.md for a Python example), or "
        "interactively via the portal. `/health` is unversioned (liveness)."
    ),
    lifespan=lifespan,
    # Gate the interactive docs in production (Talus 32 — web-edge hardening). FastAPI serves these
    # UNAUTHENTICATED; expose_api_docs derives OFF outside dev/test (config.py), so /docs, /redoc,
    # and /openapi.json all 404 on a production edge — the analog of disabling a services directory.
    docs_url="/docs" if settings.expose_api_docs else None,
    redoc_url="/redoc" if settings.expose_api_docs else None,
    openapi_url="/openapi.json" if settings.expose_api_docs else None,
)
# Middleware is wired before the routers. add_middleware is LIFO (the last one
# added is the OUTERMOST layer), so CORS — added last — wraps everything and
# answers the browser's OPTIONS preflight before it reaches auth or the routers.
app.add_middleware(RequestIDMiddleware)
# Request-body ceiling (once-over A8, app/body_limit.py). Added right after the request-id layer, so
# it sits INSIDE the rate limiter, the security headers and CORS: an over-size request is still
# counted by the limiter, and its 413 carries the same headers as any other response.
app.add_middleware(RequestBodyLimitMiddleware)
# Anonymous-surface rate limiter (Talus 23). Added BEFORE CORS so CORS stays outermost (its
# preflight + response headers wrap even a 429), while the limiter sits outside the routers and
# only ever throttles token-less requests. Authenticated traffic passes straight through.
app.add_middleware(AnonRateLimitMiddleware)
# Security response headers (Talus 32). Added AFTER the rate limiter but BEFORE CORS, so it sits
# just INSIDE CORS: CORS stays the outermost layer (still answers OPTIONS preflight first), while
# the header baseline still wraps real responses AND the rate limiter's 429 (the limiter is inner).
# Pure-ASGI (app/security_headers.py); HSTS is NOT set here — the proxy owns it (TLS-only).
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    # If-Match carries the optimistic-concurrency token on writes (Talus 34); Idempotency-Key carries
    # the offline outbox's clientOpId on creates (Talus 35). Range carries the pmtiles client's byte
    # ranges for the basemap archive (decision-basemap-provisioning.md F2 addendum). Each is a
    # non-simple request header, so without it in the allow-list the browser's CORS preflight blocks
    # the request (curl can't catch this — the T34 lesson). ETag is exposed so a browser client may
    # read the token off the response too; Content-Range/Accept-Ranges so the pmtiles client can see
    # its 206 geometry cross-origin (dev: SPA :5173 -> api :8000).
    # If-None-Match/If-Modified-Since carry a browser caller's conditional GET on the
    # field-project archive door (decision-field-projects.md F3; QField itself is not a
    # browser and needs no CORS). Last-Modified exposed for the same read.
    allow_headers=["Authorization", "Content-Type", "If-Match", "Idempotency-Key", "Range",
                   "If-None-Match", "If-Modified-Since",
                   # Talus Field f5: the field app's opt-out from the Basic challenge (auth.pat_denied)
                   "X-Talus-Client"],
    # X-Talus-Export-* : the export-truncation signal (decision-export-truncation.md §2) — a
    # header the browser hides from the SPA is not a signal, and the SPA is cross-origin in dev.
    expose_headers=["ETag", "Content-Range", "Accept-Ranges", "Last-Modified",
                    "Content-Disposition", "X-Talus-Export-Truncated",
                    "X-Talus-Export-Returned", "X-Talus-Export-Cap",
                    # Talus Field f5: why a once-valid PAT was refused (auth.pat_denied)
                    "X-Talus-Auth-Code",
                    # The error code the SPA shows on a
                    # server fault, which the admin pastes into the Helper (RequestIDMiddleware sets it).
                    "X-Request-ID"],
)



@app.exception_handler(StoredRasterRefused)
async def _stored_raster_refused(_request: Request, exc: StoredRasterRefused) -> JSONResponse:
    """A stored raster that is not the single GeoTIFF the store writes is not opened
    (raster_cog.open_stored). One fixed sentence; the reason is in the log, never in the response.
    Doors with their own catch-all (export, WCS) answer in their own fixed words instead."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# /health is unversioned — it's liveness, not part of the product contract, and the
# compose healthcheck hits http://localhost:8000/health.
app.include_router(health.router)

# The versioned public product surface. /api/v1 is the STABLE, DOCUMENTED contract that
# external/script consumers (service accounts) depend on (Talus 19 / AP14) — the OpenAPI
# schema shows only these paths.
_PRODUCT_ROUTERS = (
    tiles.router,
    tiles_dyn.router,  # dynamic MVT for filtered/labeled/view layers (decision-dynamic-mvt.md)
    custom_domains.router,
    me.router,
    home.router,
    groups.router,
    publisher.router,
    service_accounts.router,
    org_settings.router,
    basemap.router,  # anon status/archive + admin fetch/upload (decision-basemap-provisioning.md)
    ownership.router,
    users.router,
    reports.router,
    search.router,
    maintenance.router,
    backups.router,
    web_maps.router,
    rasters.router,
    layer_features.router,
    layer_fields.router,
    layer_query.router,
    attachments.router,
    layer_shares.router,
    layer_views.router,
    relates.router,  # table↔layer relates (decision-relates-and-join-views.md PR A)
    symbology.router,
    layer_forms.router,  # the layer form document — Talus Field f2 (decision-talus-field-f2.md §4.2)
    metadata.router,
    geocode.router,
    geocode_batch.router,
    import_jobs.router,  # 9d bulk-import door: staged file -> async job (decision-9d-bulk-import.md)
    geocoders.router,
    open_data.router,
    entitlement_admin.router,  # Talus+ licence status/upload — core-owned (decision-paid-packaging.md p1)
    external_refs.router,
    field_projects.router,  # .qgz intake + the Import-URL door (decision-field-projects.md)
    usage.router,  # where-used per-item + /admin/reports/usage (decision-where-used.md)
    system_health.router,  # Admin → System glance: probes + operational state (decision-system-health.md)
    ogc.public_router,   # /api/v1/ogc — anon-friendly, never challenges (harvesters/QGIS/scripts)
    ogc.auth_router,     # /api/v1/ogc-auth — challenges unauthenticated (ArcGIS Pro & signed-in desktop)
    geoservices.public_router,  # /api/v1/esri — GeoServices FeatureServer, anon-friendly (I-3)
    geoservices.auth_router,    # /api/v1/esri-auth — GeoServices FeatureServer, challenges (I-3)
    wmts.public_router,  # /api/v1/wmts — WMTS imagery, anon-friendly (I-2)
    wmts.auth_router,    # /api/v1/wmts-auth — WMTS imagery, challenges unauthenticated
    wcs.public_router,   # /api/v1/wcs — WCS live raster values, anon-friendly (I-6)
    wcs.auth_router,     # /api/v1/wcs-auth — WCS live raster values, challenges unauthenticated
    pats.router,
    sql_sources.router,  # SQL-source pull (decision-sql-source-pull.md s1) — admin-only doors
)
# Optional extension modules (decision-story-maps.md §4 / decision-paid-module-home-entitlement.md
# Part A — the ruled mounting seam). TALUS_EXTRA_MODULES names import paths; each exposes a
# module-level `router` mounted exactly like a product router. Core has no knowledge of what the
# modules ARE (the paid/ one-way rule stays structural) — and a configured module that fails to
# import fails the boot LOUDLY, matching the OpenFGA boot-guard philosophy: explicit config that
# is broken should never be skipped silently.
_extension_routers: list[APIRouter] = []
# A module MAY also expose `background_loops`: async callables the lifespan runs as tasks (the
# reports render worker is the first). Core starts and cancels them exactly like its own
# workers and knows nothing else about them — a loop that fails a tick must contain that itself.
_extension_loops: list[Callable[[], Awaitable[None]]] = []
for _name in [m.strip() for m in settings.talus_extra_modules.split(",") if m.strip()]:
    _mod = importlib.import_module(_name)
    _extension_routers.append(_mod.router)
    _extension_loops.extend(getattr(_mod, "background_loops", ()))
    log.info("extension_module_mounted", module=_name, background_loops=len(getattr(_mod, "background_loops", ())))

_api_v1 = APIRouter(prefix="/api/v1")
for _r in (*_PRODUCT_ROUTERS, *_extension_routers):
    _api_v1.include_router(_r)
app.include_router(_api_v1)

# Transitional: the SAME routers also answer at the unversioned root so the first-party
# SPA and the verify-*.sh scripts keep working while they migrate to /api/v1. These are
# HIDDEN from OpenAPI (include_in_schema=False) so the documented contract is /api/v1 only.
# Remove this loop once the SPA + scripts have moved to /api/v1.
for _r in (*_PRODUCT_ROUTERS, *_extension_routers):
    app.include_router(_r, include_in_schema=False)
