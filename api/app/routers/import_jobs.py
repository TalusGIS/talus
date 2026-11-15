"""Bulk-import jobs — the 9d door (decision-9d-bulk-import.md §3/§8).

POST /import-jobs      publisher-gated; streams the RAW BODY to jailed staging (the rasters
                       pattern: disk-bounded, never memory-bounded), validates archives,
                       sniffs the schema, provisions target=new layers (the publisher
                       creation flow + the shared field-import core), enqueues a V089 row.
GET  /import-jobs/{id} owner-scoped (someone else's job = 404, existence not disclosed).
POST /import-jobs/{id}/cancel   owner-scoped; pending dies now, running dies at the
                       worker's next batch boundary.

The gate is proven by scripts/verify-invariants/inv24-import-jobs-gate.sh (written FIRST,
recorded red 2026-08-21 pre-build).
"""
from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from psycopg import AsyncConnection

from ..auth import _validated_claims, get_current_user
from ..authz import _has_publish_role
from ..config import settings
from ..db import get_owner_db
from ..models.user import User
from .. import import_jobs_service as svc
from .layer_features import _layer_storage, _require_edit, _validate_slot
from .layer_fields import _run_field_import
from .publisher import _create_spatial_layer

log = structlog.get_logger()
router = APIRouter(tags=["import-jobs"])

# The staged filename per format is the service's ONE mapping (svc.STAGED_EXT) — the worker reads
# the file back by the same table, so the two can never drift. 'kml' covers .kml AND .kmz: staged as
# .kml either way; the service tells them apart by zip magic, never by the browser's extension.
_EXT = svc.STAGED_EXT


async def _require_publisher(
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> User:
    """403 unless the caller holds talus-publisher or talus-admin — the door stages files
    and mints layers, same class as create-layer / rasters (inv24 [2])."""
    if not _has_publish_role(claims):
        log.info("authz_rejected", capability="import_jobs:create",
                 reason="missing_publish_role", user_id=str(user.id))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="not authorized to import data")
    return user


def _job_uuid_or_404(job_id: str) -> str:
    """Non-UUID ids 404 before touching a uuid column (existence-hiding; the rasters rule)."""
    try:
        UUID(job_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return job_id


async def _spool_to(request: Request, path: str) -> int:
    """Stream the body to the jail, cap enforced as bytes arrive (the rasters pattern)."""
    cap = settings.import_max_upload_mb * 1024 * 1024
    size = 0
    with open(path, "wb") as out:
        async for chunk in request.stream():
            if not chunk:
                continue
            size += len(chunk)
            if size > cap:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"upload exceeds the {settings.import_max_upload_mb} MiB limit",
                )
            out.write(chunk)
    if size == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="empty request body — POST the file bytes as the body")
    return size


@router.post("/import-jobs", status_code=status.HTTP_202_ACCEPTED,
             summary="Stage a bulk vector import (publisher only)")
async def create_import_job(
    request: Request,
    format: str,
    name: str | None = None,
    layer_id: str | None = None,
    source_srid: int | None = None,
    filename: str | None = None,
    conn: AsyncConnection = Depends(get_owner_db),  # target=new provisions a layer (runtime DDL)
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(_require_publisher),
) -> dict[str, Any]:
    if format not in svc.FORMATS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"format must be one of {', '.join(svc.FORMATS)}")
    if (name is None) == (layer_id is None):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="exactly one of name (new layer) or layer_id (append) is required")

    job_id = uuid4()
    jail = svc.job_dir(str(job_id))
    os.makedirs(jail, exist_ok=False)
    field_summary: dict[str, int] | None = None
    try:
        upload = os.path.join(jail, "upload" + _EXT[format])
        staged_bytes = await _spool_to(request, upload)

        # per-format: validate/extract + cheap schema sniff
        if format == "shp-zip":
            svc.validate_and_extract_zip(upload, jail)
            drafts, family, detected, _shp = svc.sniff_shp(jail)
        elif format == "gpkg":
            drafts, family, detected, _table = await run_in_threadpool(svc.sniff_gpkg, upload)  # off the loop (K1)
        elif format == "csv":
            # Points only; detected is 4326 for a lat/lon-named pair and None for a projected
            # x/y pair — None falls through to the existing "supply source_srid" 422 below, so
            # the dialog's EPSG override (built for .prj-less shapefiles) covers CSV for free.
            drafts, family, detected = svc.sniff_csv(upload)
        elif format == "kml":
            # WGS-84 by the KML spec — no CRS picker; a KMZ is unpacked into the jail here.
            drafts, family, detected = svc.sniff_kml(upload, jail)
        else:
            drafts, family, detected = svc.sniff_geojson(upload)

        srid = source_srid or detected
        if srid is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="the source declares no usable CRS — supply source_srid (EPSG) explicitly",
            )

        if layer_id is not None:
            # append: the target must exist, be editable by the caller, and match the family
            await _validate_slot(conn, layer_id)
            await _require_edit(conn, claims, user, layer_id, op="add")
            _native, _wsrid, geom_type = await _layer_storage(conn, layer_id)
            if geom_type == "none":
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    detail="target is a non-spatial table — imports need geometry")
            if geom_type != family:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"source geometry is {family}; the target layer is {geom_type}",
                )
            target, created_new = layer_id, False
        else:
            # new layer: canonical 4326 storage (the house default; the worker reprojects in),
            # fields from the sniff via the shared validate->commit core (never diverges from
            # the single-field path).
            created = await _create_spatial_layer(
                conn, label=name, color=None, working_srid=4326, native=False,
                geom_type=family, actor=user.id,
            )
            target, created_new = str(created["layer_id"]), True
            if drafts:
                # ★ NEVER ignore this summary: the core reports per-draft verdicts instead of
                # raising, so a shape/type mismatch silently yields a geometry-only layer with
                # every attribute dropped (the 2026-08-21 bug). A run that creates NO field from
                # a file that HAS columns is a defect, not a preference — fail the request loudly
                # so the caller keeps their data instead of an empty layer.
                summary = (await _run_field_import(conn, target, drafts, True, str(user.id)))["summary"]
                fields_made = summary.get("created", 0) + summary.get("renamed", 0)
                if fields_made == 0:
                    log.warning("import_job_field_import_empty", layer_id=target,
                                drafts=len(drafts), summary=summary)
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"the file's {len(drafts)} attribute columns could not be created as "
                            f"fields ({summary}) — nothing was imported"
                        ),
                    )
                if summary.get("error", 0) or summary.get("coded", 0) or summary.get("duplicate", 0):
                    log.info("import_job_fields_partial", layer_id=target, summary=summary)
                field_summary = summary

        job = await svc.create_job(
            conn, job_id=job_id, fmt=format, original_name=(filename or "upload")[:200],
            staged_bytes=staged_bytes, target_layer_id=target, created_new=created_new,
            source_srid=srid, actor=user.id,
        )
    except BaseException:
        svc.reap_staging(str(job_id))
        raise
    log.info("import_job_staged", job_id=str(job_id), format=format,
             staged_bytes=staged_bytes, target_layer_id=target, created_new=created_new,
             fields=field_summary, actor=str(user.id))
    return {**job, "fields": field_summary}


@router.get("/import-jobs/{job_id}", summary="Import job status (owner only)")
async def get_import_job(
    job_id: str,
    conn: AsyncConnection = Depends(get_owner_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    job = await svc.get_job_owned(conn, _job_uuid_or_404(job_id), user.id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return job


@router.post("/import-jobs/{job_id}/cancel", summary="Cancel an import job (owner only)")
async def cancel_import_job(
    job_id: str,
    conn: AsyncConnection = Depends(get_owner_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    job = await svc.cancel_job(conn, _job_uuid_or_404(job_id), user.id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="job not found or already terminal")
    return job
