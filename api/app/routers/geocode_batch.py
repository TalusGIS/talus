"""Batch (bulk) geocoding endpoints (decision-batch-geocoding.md).

The run is ASYNC (F5): `run` does only the fast work — the three-layer preflight/credit gate, provision the
output point layer, insert every input row as a `pending` null-geom feature — then returns `{jobId,
outputLayerId}`. The background worker (geocode_batch_worker) geocodes the pending features. `preflight`
is the pre-run capability/credit check; `jobs/{id}` polls progress; `cancel` stops a run.

Authz: `run` is PUBLISHER-gated (it creates a layer, same class as create-layer); a hosted-table source is
additionally `can_view`-gated on that table; `jobs/{id}` and `cancel` are OWNER-or-admin (a user can't read
or cancel another user's job). No anonymous batch geocoding. The worker resolves the locator token fresh —
no secret rides these request/response bodies.
"""
import math
import os
import re
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from psycopg import AsyncConnection, sql
from pydantic import BaseModel

from .. import geocode, geocode_batch
from .. import geocode_batch_service as svc
from .. import import_jobs_service as imp
from ..auth import _validated_claims, get_current_user
from ..authz import _has_admin_role, edit_allowed, edit_deny_sentence, manage_allowed, view_allowed
from ..db import get_db, get_owner_db
from ..geocoders import active_geocoder_configs
from ..layer_props import active_field_defs
from ..models.user import User
from .publisher import _require_publisher

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/geocode/batch", tags=["geocode-batch"])

# ESRI's published stored-batch geocoding rate (advisory — the explicit confirm is the gate, not this
# number). ~40 credits / 1,000 stored geocodes; surfaced so the user sees the spend before acknowledging.
_CREDIT_RATE_PER_1000 = 40

_HOSTED_RE = re.compile(r"^tal_[0-9a-f]{8}$")
_MAX_DISPLAY_NAME = 200


async def _resolve_locator(conn: AsyncConnection, name: str):
    cfg = next((c for c in await active_geocoder_configs(conn) if c.name == name), None)
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown locator {name!r}")
    return cfg


# --- preflight ------------------------------------------------------------------


class PreflightRequest(BaseModel):
    locator: str
    rowCount: int = 0


@router.post("/preflight", summary="Batch pre-flight: can this locator batch, does it cost credits/need a token")
async def preflight(
    body: PreflightRequest,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cfg = await _resolve_locator(conn, body.locator)
    pf = await geocode.preflight(cfg)
    estimated = None
    if pf.get("creditConsuming") and body.rowCount > 0:
        estimated = round(body.rowCount / 1000 * _CREDIT_RATE_PER_1000, 1)
    return {
        **pf,
        "locator": body.locator,
        "rowCap": geocode_batch.cap_for_type(cfg.type),
        "estimatedCredits": estimated,
        "creditRatePer1000": _CREDIT_RATE_PER_1000,
    }


# --- run ------------------------------------------------------------------------


class RunRequest(BaseModel):
    locator: str
    columns: list[str]                 # the address column(s): single, or several concatenated (F2)
    csvText: str | None = None         # CSV upload text (in-memory), OR ...
    sourceLayerId: str | None = None   # ... an existing hosted layer/table to read rows from, OR ...
    # ... rows already in the (headers, rows) shape — what `POST /geocode/batch/kml/rows` returns for
    # an address-only KML/KMZ (decision-kml-address-geocode.md F1). Same caps and gates as csvText.
    rows: list[dict[str, str]] | None = None
    displayName: str | None = None
    threshold: float = geocode_batch.DEFAULT_THRESHOLD
    chargesAcknowledged: bool = False  # required when the locator consumes credits (F6-C)
    # Optional country-narrow (external locators only) — ISO alpha-2 codes; empty/None = global (default).
    country: list[str] | None = None


def _clean_country(codes: list[str] | None) -> list[str]:
    """Keep only well-formed ISO alpha-2 codes (lowercased) — what sourceCountry / countrycodes want."""
    if not codes:
        return []
    seen: list[str] = []
    for c in codes:
        cc = c.strip().lower()
        if len(cc) == 2 and cc.isalpha() and cc not in seen:
            seen.append(cc)
    return seen


async def _read_source_layer(
    conn: AsyncConnection, layer_id: str, user: User, claims: dict | None
) -> tuple[list[str], list[dict[str, str]]]:
    """Read a hosted layer's fields + rows as (headers, rows) for geocoding its address column(s). Gated on
    can_view (or manage). layer_id is validated to the slot-token shape + registry before it names a table."""
    if not _HOSTED_RE.fullmatch(layer_id):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="source must be a hosted layer/table")
    if not (await view_allowed(user, layer_id) or await manage_allowed(claims, user, layer_id)):
        log.info("authz_rejected", capability=f"{layer_id}:batch-source-read",
                 reason="no_view_access", user_id=str(user.id))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"not authorized to read {layer_id}")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status FROM platform.layers WHERE layer_id = %s", (layer_id,)
        )
        row = await cur.fetchone()
    if row is None or row[0] != "ready":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source layer not found")
    field_defs = await active_field_defs(conn, layer_id)
    headers = list(field_defs.keys())
    tbl = sql.Identifier("assets", layer_id)
    async with conn.cursor() as cur:
        await cur.execute(sql.SQL("SELECT props FROM {tbl}").format(tbl=tbl))
        rows = [
            {h: ("" if (props or {}).get(h) is None else str((props or {}).get(h))) for h in headers}
            for (props,) in await cur.fetchall()
        ]
    return headers, rows


def _rows_source(raw: list[dict[str, str]]) -> tuple[list[str], list[dict[str, str]]]:
    """The `rows` source normalized to the parse_csv shape: headers = keys in first-seen order (JSON
    object order is preserved), every value a string, every row carrying every header. The same
    byte cap as csvText, measured over the values, so a JSON body cannot dodge the CSV limit."""
    if not raw:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="rows is empty")
    headers: list[str] = []
    seen: set[str] = set()
    total = 0
    for r in raw:
        for k, v in r.items():
            if k not in seen:
                headers.append(k)
                seen.add(k)
            total += len(str(v)) if v is not None else 0
            if total > geocode_batch_max_bytes():
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="rows too large")
    if not headers:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="rows carry no columns")
    rows = [{h: ("" if r.get(h) is None else str(r.get(h))) for h in headers} for r in raw]
    return headers, rows


@router.post("/kml/rows", summary="An address-only KML/KMZ as batch-geocoding rows (publisher; raw body)")
async def kml_rows(
    request: Request,
    user: User = Depends(_require_publisher),
) -> dict[str, Any]:
    """G3b (decision-kml-address-geocode.md): a Google My Maps export carries an <address> per
    placemark and no coordinates. Stream the upload into an import-style jail under the batch byte
    cap, read it with the import door's own KML reader (same DTD / zip / nesting fences; nothing is
    fetched), return the (headers, rows) shape the run door takes as `rows`, and reap the jail —
    nothing outlives the request. Publisher-gated like `run` (the rows come straight back to the
    uploader, but the door exists to feed a layer-creating run). Values are never logged."""
    job_id = str(uuid4())
    jail = imp.job_dir(job_id)
    os.makedirs(jail, exist_ok=False)
    cap = geocode_batch_max_bytes()
    try:
        upload = os.path.join(jail, "upload.kml")
        size = 0
        with open(upload, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > cap:
                    raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                        detail=f"upload exceeds the {cap // (1024 * 1024)} MiB limit")
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="empty upload")
        headers, rows, stats = imp.kml_rows(upload, jail)
    finally:
        imp.reap_staging(job_id)
    log.info("geocode_batch_kml_rows", actor=str(user.id), bytes=size, **stats)
    return {"headers": headers, "rows": rows, **stats}


@router.post("/run", status_code=status.HTTP_201_CREATED, summary="Start an async batch-geocode run (publisher)")
async def run(
    body: RunRequest,
    conn: AsyncConnection = Depends(get_owner_db),  # provisions the output layer (runtime DDL) — owner pool
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(_require_publisher),
) -> dict[str, Any]:
    cfg = await _resolve_locator(conn, body.locator)
    pf = await geocode.preflight(cfg)
    if not pf.get("batchCapable"):
        # The non-batch ESRI service / public-Nominatim block (F6/F6a) — honest, before any work.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=pf.get("reason") or "this locator can't batch geocode")

    # Source → (headers, rows). CSV parsed in-memory (file_ingest cap), or an existing hosted layer.
    if body.csvText is not None:
        if len(body.csvText) > geocode_batch_max_bytes():
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                detail="CSV too large")
        try:
            headers, rows = geocode_batch.parse_csv(body.csvText)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    elif body.sourceLayerId:
        headers, rows = await _read_source_layer(conn, body.sourceLayerId, user, claims)
    elif body.rows is not None:
        headers, rows = _rows_source(body.rows)
    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="provide csvText, sourceLayerId, or rows")

    if not body.columns:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="choose at least one address column")
    missing = [c for c in body.columns if c not in headers]
    if missing:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"columns not found in the input: {', '.join(missing)}")

    cap = geocode_batch.cap_for_type(cfg.type)
    if len(rows) > cap:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{len(rows)} rows exceeds the {cap:,}-row limit for this locator; "
            "split the input, or use your internal locator",
        )

    if pf.get("creditConsuming") and not body.chargesAcknowledged:
        est = math.ceil(len(rows) / 1000 * _CREDIT_RATE_PER_1000)
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"This locator consumes credits (~{est} for {len(rows)} rows). "
            "Re-run with chargesAcknowledged=true to proceed.",
        )

    name = (body.displayName or "").strip()[:_MAX_DISPLAY_NAME] or f"Geocoded — {body.locator}"
    # The country-narrow only applies to the external locators; the internal (own-data) locator ignores it.
    countries = _clean_country(body.country) if pf.get("type") in ("arcgis", "nominatim") else []
    options = {"country": countries} if countries else None
    result = await svc.create_batch_job(
        conn, user.id, name, body.locator, float(body.threshold),
        bool(pf.get("creditConsuming")), headers, rows, body.columns, options=options,
    )
    log.info("geocode_batch_run", job_id=result["jobId"], layer_id=result["outputLayerId"],
             locator=body.locator, rows=result["totalRows"], actor=str(user.id))
    return result


def geocode_batch_max_bytes() -> int:
    """The CSV byte cap — the shared add-from-file 50 MB limit (decision §2)."""
    from ..file_ingest import MAX_FILE_BYTES
    return MAX_FILE_BYTES


# --- job status / cancel --------------------------------------------------------


async def _owned_job_or_403(conn: AsyncConnection, job_id: str, user: User, claims: dict | None) -> dict:
    job = await svc.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    if job["createdBy"] != str(user.id) and not _has_admin_role(claims):
        log.info("authz_rejected", capability=f"job:{job_id}:read", reason="not_owner_not_admin",
                 user_id=str(user.id))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized for this job")
    return job


@router.get("/jobs/{job_id}", summary="Poll a batch-geocode job's progress (owner or admin)")
async def job_status(
    job_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    return await _owned_job_or_403(conn, job_id, user, claims)


@router.post("/jobs/{job_id}/cancel", summary="Cancel a running batch-geocode job (owner or admin)")
async def cancel(
    job_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _owned_job_or_403(conn, job_id, user, claims)
    new_status = await svc.cancel_job(conn, job_id)
    return {"jobId": job_id, "status": new_status}


# --- rematch (re-run misses / selected rows) ------------------------------------


class RematchRequest(BaseModel):
    # Specific rows to re-geocode; omit to re-run ALL misses (no_match / low_score / tie / canceled).
    # (Edit an address first via the normal feature PATCH — rematch re-geocodes the current geocode_input.)
    featureIds: list[str] | None = None
    chargesAcknowledged: bool = False


@router.post("/{layer_id}/rematch", status_code=status.HTTP_201_CREATED,
             summary="Re-geocode a batch output's misses (or selected rows) — async re-queue")
async def rematch(
    layer_id: str,
    body: RematchRequest,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    if not _HOSTED_RE.fullmatch(layer_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    # A rematch re-writes point geometry on existing rows: op `geometry` (decision-layer-edit-capabilities.md
    # §3.3, ruled 2026-09-26).
    allowed, reason = await edit_allowed(conn, claims, user, layer_id, "geometry")
    if not allowed:
        log.info("authz_rejected", capability=f"{layer_id}:rematch", reason=reason, user_id=str(user.id))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=edit_deny_sentence(reason) or f"not authorized to edit {layer_id}")
    job = await svc.latest_job_for_layer(conn, layer_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="this layer is not a batch-geocode output")
    # Rematch re-runs against the SAME locator, so a credit-consuming locator re-confirms (it spends more).
    if job["credit_consuming"] and not body.chargesAcknowledged:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED,
                            detail="re-running against this locator consumes more credits; "
                            "re-run with chargesAcknowledged=true to proceed")
    return await svc.requeue(
        conn, user.id, layer_id, job["locator_name"], job["threshold"], job["credit_consuming"],
        body.featureIds, options=job.get("options"),
    )
