"""SQL-source pull doors (decision-sql-source-pull.md s1; F11 amended 2026-10-01) — PUBLISHER-gated, PER-USER.

  /sql-connections            list (own) · create · update · delete · test · catalog
  /sql-connections/all        every connection, read-only (admin only — host + owner, never a secret)
  /sql-pulls                  list (own) · create (owner pool: runtime DDL) · get · delete · run
  /sql-pulls/all              every pull (admin only)
  /sql-pull-runs/{id}         status for polling · cancel

Who may aim the server at a host: a publisher — the tier that already creates tables and runs bulk
imports (inv24). A row that is not the caller's own is a 404 (existence not disclosed); an admin may
additionally DELETE any connection or pull (oversight) but never reads or uses another user's
credential. `api/tests/test_sql_sources.py` asserts the gating structurally; inv47 proves it live.
Errors from the source arrive as `SourceError` with a stable reason: validation-class reasons are
422, upstream-class (the source refused or is unreachable) are 502; the message is the service's
fixed sentence, never the driver's text.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from psycopg import errors as pg_errors

from ..auth import _validated_claims, get_current_user
from ..authz import _has_admin_role, _has_publish_role
from ..db import get_db, get_owner_db
from ..deps import _require_admin
from ..models.user import User
from ..secrets import SecretKeyUnavailable
from ..sqlsource import service as svc
from ..sqlsource.dialects import DIALECTS, SourceError

log = structlog.get_logger(__name__)
router = APIRouter(tags=["sql-sources"])


async def _require_publisher(
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> User:
    """403 unless the caller holds talus-publisher or talus-admin — a pull mints a table, the same
    class as create-layer and the bulk import (inv24 [2])."""
    if not _has_publish_role(claims):
        log.info("authz_rejected", capability="sql_sources", reason="missing_publish_role", user_id=str(user.id))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to use database sources")
    return user


async def _is_admin(claims: dict[str, Any] | None = Depends(_validated_claims)) -> bool:
    return _has_admin_role(claims)


def _uuid_or_404(v: str, what: str) -> UUID:
    try:
        return UUID(v)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


def _source_http(exc: SourceError) -> HTTPException:
    code = status.HTTP_502_BAD_GATEWAY if svc.is_upstream_reason(exc.reason) else status.HTTP_422_UNPROCESSABLE_ENTITY
    return HTTPException(status_code=code, detail={"reason": exc.reason, "message": str(exc)})


async def _own_connection(conn: AsyncConnection, cid: str, user: User) -> svc.ConnectionRow:
    row = await svc.get_connection_row(conn, _uuid_or_404(cid, "connection"))
    if row is None or not svc.owns(row.created_by, user.id):
        raise _not_found("connection")
    return row


async def _own_pull(conn: AsyncConnection, pid: str, user: User) -> dict[str, Any]:
    pull = await svc.get_pull(conn, _uuid_or_404(pid, "pull"))
    if pull is None or not svc.owns(pull["ownerId"], user.id):
        raise _not_found("pull")
    return pull


# ---- connections ----------------------------------------------------------------------------
@router.get("/sql-connections/dialects", summary="The dialects this release can pull from (publisher)")
async def list_dialects(_user: User = Depends(_require_publisher)) -> list[dict[str, Any]]:
    return [{"name": d.name, "defaultPort": d.default_port, "available": d.available,
             "unavailableReason": d.unavailable_reason} for d in DIALECTS.values()]


@router.get("/sql-connections/all", summary="Every connection in the org, read-only — host and owner, never a secret (admin only)")
async def list_all_connections(conn: AsyncConnection = Depends(get_db), _admin: User = Depends(_require_admin),
                               _user: User = Depends(_require_publisher)) -> list[dict[str, Any]]:
    return await svc.list_connections(conn, None)


@router.get("/sql-connections", summary="My database connections (publisher)")
async def list_connections(conn: AsyncConnection = Depends(get_db), user: User = Depends(_require_publisher)) -> list[dict[str, Any]]:
    return await svc.list_connections(conn, user.id)


@router.post("/sql-connections", status_code=status.HTTP_201_CREATED, summary="Add a database connection of my own (publisher)")
async def create_connection(body: svc.ConnectionWrite, conn: AsyncConnection = Depends(get_db),
                            user: User = Depends(_require_publisher)) -> dict[str, Any]:
    try:
        return await svc.create_connection(conn, body, user.id)
    except SourceError as exc:
        raise _source_http(exc)
    except SecretKeyUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except pg_errors.UniqueViolation:
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a connection with that name already exists")


@router.put("/sql-connections/{cid}", summary="Edit my connection — full replace, password write-only (publisher)")
async def update_connection(cid: str, body: svc.ConnectionWrite, conn: AsyncConnection = Depends(get_db),
                            user: User = Depends(_require_publisher)) -> dict[str, Any]:
    await _own_connection(conn, cid, user)
    try:
        out = await svc.update_connection(conn, UUID(cid), body, user.id)
    except SourceError as exc:
        raise _source_http(exc)
    except SecretKeyUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except pg_errors.UniqueViolation:
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a connection with that name already exists")
    if out is None:
        raise _not_found("connection")
    return out


@router.delete("/sql-connections/{cid}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Remove a connection and its pulls; the copied tables stay, unlocked (owner, or admin)")
async def delete_connection(cid: str, conn: AsyncConnection = Depends(get_db), user: User = Depends(_require_publisher),
                            is_admin: bool = Depends(_is_admin)) -> None:
    row = await svc.get_connection_row(conn, _uuid_or_404(cid, "connection"))
    if row is None or not (is_admin or svc.owns(row.created_by, user.id)):
        raise _not_found("connection")
    await svc.delete_connection(conn, UUID(cid), user.id)


@router.post("/sql-connections/{cid}/test", summary="Open my connection and count its tables (publisher)")
async def test_connection(cid: str, conn: AsyncConnection = Depends(get_db), user: User = Depends(_require_publisher)) -> dict[str, Any]:
    await _own_connection(conn, cid, user)
    out = await svc.test_connection(conn, UUID(cid), user.id)
    if out is None:
        raise _not_found("connection")
    return out


@router.get("/sql-connections/{cid}/catalog", summary="Tables and views the login can see (publisher, own connection)")
async def catalog_tables(cid: str, conn: AsyncConnection = Depends(get_db), user: User = Depends(_require_publisher)) -> list[dict[str, Any]]:
    await _own_connection(conn, cid, user)
    try:
        out = await svc.catalog_tables(conn, UUID(cid))
    except SourceError as exc:
        raise _source_http(exc)
    if out is None:
        raise _not_found("connection")
    return out


@router.get("/sql-connections/{cid}/catalog/{schema}/{table}", summary="Columns of one table or view, with the Talus mapping (publisher, own connection)")
async def catalog_columns(cid: str, schema: str, table: str, conn: AsyncConnection = Depends(get_db),
                          user: User = Depends(_require_publisher)) -> dict[str, Any]:
    await _own_connection(conn, cid, user)
    try:
        out = await svc.catalog_columns(conn, UUID(cid), schema, table)
    except SourceError as exc:
        raise _source_http(exc)
    if out is None:
        raise _not_found("connection")
    return out


# ---- pulls ---------------------------------------------------------------------------------
@router.get("/sql-pulls/all", summary="Every pull in the org (admin only)")
async def list_all_pulls(conn: AsyncConnection = Depends(get_db), _admin: User = Depends(_require_admin),
                         _user: User = Depends(_require_publisher)) -> list[dict[str, Any]]:
    return await svc.list_pulls(conn, None)


@router.get("/sql-pulls", summary="My pulls (publisher)")
async def list_pulls(conn: AsyncConnection = Depends(get_db), user: User = Depends(_require_publisher)) -> list[dict[str, Any]]:
    return await svc.list_pulls(conn, user.id)


@router.post("/sql-pulls", status_code=status.HTTP_202_ACCEPTED,
             summary="Create a pull from my connection: a new non-spatial table from one source table or view, run once (publisher)")
async def create_pull(body: svc.PullCreate,
                      conn: AsyncConnection = Depends(get_owner_db),   # provisions a hosted table (runtime DDL)
                      user: User = Depends(_require_publisher)) -> dict[str, Any]:
    try:
        return await svc.create_pull(conn, body, user)
    except SourceError as exc:
        if exc.reason == "connection_missing":
            raise _not_found("connection")
        raise _source_http(exc)
    except svc.RunConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a run is already queued for this pull")


@router.get("/sql-pulls/{pid}", summary="One of my pulls with its latest run (publisher)")
async def get_pull(pid: str, conn: AsyncConnection = Depends(get_db), user: User = Depends(_require_publisher)) -> dict[str, Any]:
    return await _own_pull(conn, pid, user)


@router.delete("/sql-pulls/{pid}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Detach a pull; the copied table stays as an ordinary, unlocked table (owner, or admin)")
async def delete_pull(pid: str, conn: AsyncConnection = Depends(get_db), user: User = Depends(_require_publisher),
                      is_admin: bool = Depends(_is_admin)) -> None:
    pull = await svc.get_pull(conn, _uuid_or_404(pid, "pull"))
    if pull is None or not (is_admin or svc.owns(pull["ownerId"], user.id)):
        raise _not_found("pull")
    await svc.delete_pull(conn, UUID(pid), user.id)


@router.put("/sql-pulls/{pid}/schedule", summary="Set the refresh schedule: manual, nightly at an hour, or every N hours (owner)")
async def set_schedule(pid: str, body: svc.PullSchedule, conn: AsyncConnection = Depends(get_db),
                       user: User = Depends(_require_publisher)) -> dict[str, Any]:
    await _own_pull(conn, pid, user)
    out = await svc.set_schedule(conn, UUID(pid), body, user.id)
    if out is None:
        raise _not_found("pull")
    return out


@router.post("/sql-pulls/{pid}/run", status_code=status.HTTP_202_ACCEPTED, summary="Refresh now (owner)")
async def run_pull(pid: str, conn: AsyncConnection = Depends(get_db), user: User = Depends(_require_publisher)) -> dict[str, Any]:
    await _own_pull(conn, pid, user)
    try:
        return await svc.enqueue_run(conn, UUID(pid), user.id)
    except svc.RunConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a run is already queued or running for this pull")


@router.get("/sql-pull-runs/{rid}", summary="Run status for polling (owner)")
async def get_run(rid: str, conn: AsyncConnection = Depends(get_db), user: User = Depends(_require_publisher)) -> dict[str, Any]:
    owner = await svc.run_owner(conn, _uuid_or_404(rid, "run"))
    if owner is None or not svc.owns(owner, user.id):
        raise _not_found("run")
    out = await svc.get_run(conn, UUID(rid))
    if out is None:
        raise _not_found("run")
    return out


@router.post("/sql-pull-runs/{rid}/cancel", summary="Cancel a queued or running run — the last good copy stays (owner, or admin)")
async def cancel_run(rid: str, conn: AsyncConnection = Depends(get_db), user: User = Depends(_require_publisher),
                     is_admin: bool = Depends(_is_admin)) -> dict[str, Any]:
    owner = await svc.run_owner(conn, _uuid_or_404(rid, "run"))
    if owner is None or not (is_admin or svc.owns(owner, user.id)):
        raise _not_found("run")
    out = await svc.cancel_run(conn, UUID(rid), user.id)
    if out is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found or already finished")
    return out
