"""Talus 37 — admin geocoder management (Admin → Geocoders).

The in-portal replacement for editing the GEOCODERS env var + restarting the api: list / add / edit /
reorder / enable / remove the org's locators, plus a non-blocking "Test" probe. Every endpoint is
admin-gated by the shared `deps._require_admin` (JWT-role-based, same gate as org-settings / users —
no OpenFGA coupling). The public address-search proxy stays at /geocode/* (any authenticated user);
this router is the admin write surface that feeds it.

Token locators (decision-geocoder-token-locators.md): the credential is WRITE-ONLY through this
router — GeocoderWrite carries token/clearToken in, GeocoderRow carries only hasToken/tokenHint out,
and the Test probe returns status, never the value. A missing TALUS_ENCRYPTION_KEY surfaces as an
actionable 422 on write and a legible probe failure on Test (never a 500). URLs are restricted to
http/https (F4 — SSRF posture) by the GeocoderWrite validator and re-checked on Test.
"""
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation

from ..config import GeocoderConfig
from ..db import get_db, get_owner_db
from ..deps import _require_admin
from ..geocode import probe
from ..geocode_internal import MappingError, eligible_layers, mappable_fields
from ..geocoders import (
    GeocoderReorder,
    GeocoderRow,
    GeocoderTestRequest,
    GeocoderTestResult,
    GeocoderWrite,
    _validate_url,
    create_geocoder,
    delete_geocoder,
    read_geocoders,
    reorder_geocoders,
    stored_token,
    update_geocoder,
)
from ..models.user import User
from ..secrets import SecretDecryptError, SecretKeyUnavailable

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/geocoders", tags=["geocoders"])


def _parse_uuid(gid: str) -> UUID:
    """A malformed id is treated as 'not found' (404), not a 500."""
    try:
        return UUID(gid)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="geocoder not found")


def _dup_name(name: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"a geocoder named '{name}' already exists",
    )


def _bad_mapping(exc: MappingError) -> HTTPException:
    """An internal locator's layer/field mapping didn't resolve (Mode B) — actionable 422, not a 500."""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("", summary="List the org's geocoders (admin only)")
async def list_geocoders(
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> list[GeocoderRow]:
    return await read_geocoders(conn)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Add a geocoder (admin only)")
async def add_geocoder(
    body: GeocoderWrite,
    conn: AsyncConnection = Depends(get_owner_db),  # S-1a: internal locator mints a GIN index (DDL), atomic with the config INSERT — owner pool
    admin: User = Depends(_require_admin),
) -> GeocoderRow:
    try:
        row = await create_geocoder(conn, body, admin.id)
    except UniqueViolation:
        await conn.rollback()
        raise _dup_name(body.name)
    except MappingError as exc:
        await conn.rollback()
        raise _bad_mapping(exc)
    except SecretKeyUnavailable as exc:
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    log.info("geocoder_created", actor=str(admin.id), name=row.name, type=row.type)
    return row


# Static sub-paths declared BEFORE /{gid} so 'reorder'/'test' aren't captured as an id.
@router.get(
    "/eligible-layers",
    summary="Point layers an internal locator may search (admin only)",
)
async def list_eligible_layers(
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> list[dict[str, str]]:
    """The Add/Edit form's layer picker (Mode B, decision-own-data-geocoder §3 F7). Admin-gated like
    the rest of this router — configuring a locator is deliberate org-wide disclosure (§3 F3), so
    the admin sees every eligible layer regardless of sharing."""
    return await eligible_layers(conn)


@router.get(
    "/eligible-layers/{layer_id}/fields",
    summary="Address-field choices for one eligible layer (admin only)",
)
async def list_mappable_fields(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> list[dict[str, str]]:
    try:
        return await mappable_fields(conn, layer_id)
    except MappingError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.put("/reorder", summary="Reorder geocoders (admin only)")
async def reorder(
    body: GeocoderReorder,
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> list[GeocoderRow]:
    ids = [_parse_uuid(i) for i in body.ids]
    rows = await reorder_geocoders(conn, ids, admin.id)
    log.info("geocoders_reordered", actor=str(admin.id), count=len(ids))
    return rows


@router.post("/test", summary="Test a geocoder before relying on it (admin only)")
async def test_geocoder(
    body: GeocoderTestRequest,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> GeocoderTestResult:
    """Fire one sample geocode (decision §3 F3). Either an existing `id`, or an ad-hoc
    {type, url, outSR, extent} — internal: {type, layerId, addressFields} — so a brand-new locator
    can be probed before it's saved. Non-blocking — the result never gates a save; the caller
    decides what to do with a failure. `geocode.probe` dispatches per transport (internal defs
    probe conn-aware, decision-own-data-geocoder §3 F7); external ones over httpx, as before."""
    if body.id:
        match = next((r for r in await read_geocoders(conn) if r.id == body.id), None)
        if match is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="geocoder not found")
        # The probe exercises the STORED credential (decision-geocoder-token-locators §3 F5 —
        # validating the saved token is half the point). A transient body token wins (the admin is
        # trying a replacement); decrypt failure = a legible probe result, not a 500.
        token = body.token
        if token is None and match.hasToken:
            try:
                token = await stored_token(conn, body.id)
            except (SecretKeyUnavailable, SecretDecryptError) as exc:
                return GeocoderTestResult(ok=False, sampleCount=0, error=f"credential unavailable: {exc}")
        cfg = GeocoderConfig(
            name=match.name, type=match.type, url=match.url, out_sr=match.outSR,
            extent=match.extent, layer_id=match.layerId, address_fields=match.addressFields,
            locality_fields=match.localityFields or None,
            token=token if match.type == "arcgis" else None,
        )
    elif body.type == "internal":
        if not body.layerId or not body.addressFields:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="testing an internal locator requires layerId + addressFields",
            )
        cfg = GeocoderConfig(
            name="(test)", type="internal", extent=body.extent,
            layer_id=body.layerId, address_fields=body.addressFields,
            locality_fields=body.localityFields or None,
        )
    else:
        if not body.type or not body.url:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="test requires either an existing id or a type + url",
            )
        try:
            url = _validate_url(body.url)  # re-check the http/https guard (F4) for an ad-hoc def
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        cfg = GeocoderConfig(
            name="(test)", type=body.type, url=url, out_sr=body.outSR, extent=body.extent,
            # Transient ad-hoc credential (F5): probe-before-save. Used once, never stored/logged.
            token=body.token if body.type == "arcgis" else None,
        )
    ok, count, error = await probe(cfg, body.q, conn=conn)
    return GeocoderTestResult(ok=ok, sampleCount=count, error=error)


@router.put("/{gid}", summary="Edit a geocoder (admin only)")
async def edit_geocoder(
    gid: str,
    body: GeocoderWrite,
    conn: AsyncConnection = Depends(get_owner_db),  # S-1a: may drop/recreate the locator index (DDL) — owner pool
    admin: User = Depends(_require_admin),
) -> GeocoderRow:
    uid = _parse_uuid(gid)
    try:
        row = await update_geocoder(conn, uid, body, admin.id)
    except UniqueViolation:
        await conn.rollback()
        raise _dup_name(body.name)
    except MappingError as exc:
        await conn.rollback()
        raise _bad_mapping(exc)
    except SecretKeyUnavailable as exc:
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="geocoder not found")
    log.info("geocoder_updated", actor=str(admin.id), id=gid, name=row.name)
    return row


@router.delete("/{gid}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a geocoder (admin only)")
async def remove_geocoder(
    gid: str,
    conn: AsyncConnection = Depends(get_owner_db),  # S-1a: drops the locator index (DDL) — owner pool
    admin: User = Depends(_require_admin),
) -> None:
    uid = _parse_uuid(gid)
    if not await delete_geocoder(conn, uid):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="geocoder not found")
    log.info("geocoder_deleted", actor=str(admin.id), id=gid)