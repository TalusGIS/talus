"""Talus 39 — Admin → Maintenance (housekeeping run history, admin-gated, READ-ONLY).

A read-only operational view of the self-maintenance scheduler (app/maintenance.py): "what cleanup jobs
exist, when did each last run, and what did it free?" — the self-tidying story
made legible to an admin. Read-only by design in v1 (decision-housekeeping.md F-config): cadence +
retention are config-driven (MAINTENANCE_* env), so there is NO write surface here — an editable Admin
"Maintenance" sub-section is a deferred fast-follow. The single endpoint is behind _require_admin (the
same gate as Reports / Settings); UI gating in the SPA is cosmetic — this is the authority.
"""
import structlog
from fastapi import APIRouter, Depends, Query
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel

from ..db import get_db
from ..deps import _require_admin
from ..maintenance import registered_job_names
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["maintenance"])


class JobSummary(BaseModel):
    """Latest run for one registered job (nulls if it has never run yet)."""

    jobName: str
    lastRunAt: str | None
    lastStatus: str | None
    lastItemsAffected: int | None
    lastBytesFreed: int | None
    lastDetail: str | None


class RunRow(BaseModel):
    """One historical run row (most-recent-first list)."""

    id: int
    jobName: str
    startedAt: str
    finishedAt: str
    status: str
    itemsAffected: int
    bytesFreed: int
    detail: str | None


class MaintenanceRuns(BaseModel):
    jobs: list[JobSummary]    # one per registered job (incl. never-run), in registry order
    recent: list[RunRow]      # recent history across all jobs, newest first


@router.get(
    "/admin/maintenance/runs",
    response_model=MaintenanceRuns,
    summary="Housekeeping scheduler run history (admin-only, read-only)",
)
async def get_maintenance_runs(
    limit: int = Query(default=50, ge=1, le=200),
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(_require_admin),
) -> MaintenanceRuns:
    """The latest run per registered job + recent run history. Counts/timestamps only — no GIS data,
    no write surface (read-only v1)."""
    names = registered_job_names()
    async with conn.cursor(row_factory=dict_row) as cur:
        # Latest run per job (DISTINCT ON), so the summary shows the freshest status/outcome.
        await cur.execute(
            "SELECT DISTINCT ON (job_name) job_name, started_at, status, items_affected, "
            "       bytes_freed, detail "
            "FROM platform.maintenance_runs ORDER BY job_name, started_at DESC"
        )
        latest = {r["job_name"]: r for r in await cur.fetchall()}
        await cur.execute(
            "SELECT id, job_name, started_at, finished_at, status, items_affected, bytes_freed, detail "
            "FROM platform.maintenance_runs ORDER BY started_at DESC LIMIT %s",
            (limit,),
        )
        recent_rows = await cur.fetchall()

    jobs = [
        JobSummary(
            jobName=name,
            lastRunAt=latest[name]["started_at"].isoformat() if name in latest else None,
            lastStatus=latest[name]["status"] if name in latest else None,
            lastItemsAffected=latest[name]["items_affected"] if name in latest else None,
            lastBytesFreed=latest[name]["bytes_freed"] if name in latest else None,
            lastDetail=latest[name]["detail"] if name in latest else None,
        )
        for name in names
    ]
    recent = [
        RunRow(
            id=r["id"],
            jobName=r["job_name"],
            startedAt=r["started_at"].isoformat(),
            finishedAt=r["finished_at"].isoformat(),
            status=r["status"],
            itemsAffected=r["items_affected"],
            bytesFreed=r["bytes_freed"],
            detail=r["detail"],
        )
        for r in recent_rows
    ]
    return MaintenanceRuns(jobs=jobs, recent=recent)
