"""Batch geocoding — the async worker loop (decision-batch-geocoding.md F5).

A lifespan-managed asyncio loop (started in api/app/main.py) that geocodes queued batch jobs. Reuses the
maintenance seam's PATTERN — a Postgres advisory lock so exactly ONE replica processes jobs even when the
api is scaled out, and fail-safe (a bad job is marked failed by the service, never crashes the loop) — but
with a DISTINCT lock key and as a SEPARATE subsystem: unlike maintenance (cruft-only, HARD-fenced from
authoritative data, decision-housekeeping §2), this worker legitimately writes features (the geocoded
output). Jobs run one at a time, FIFO (F5).

Resumable by construction: the output layer's `pending` features are the work queue (geocode_batch_service),
so a crash mid-job leaves them pending and the next tick resumes with nothing to replay.
"""
from __future__ import annotations

import asyncio

import structlog

from . import geocode_batch_service as svc
from .config import settings
from .db import get_pool

log = structlog.get_logger(__name__)

# A DISTINCT advisory-lock key from maintenance's 3_900_000_039 — the batch worker is its own subsystem,
# namespaced to "batch geocoding / V081" and documented here so nothing else reuses it.
_ADVISORY_LOCK_KEY = 8_100_000_081


async def _try_lock(conn) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        row = await cur.fetchone()
    return bool(row and row[0])


async def _unlock(conn) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))


async def _run_due() -> None:
    """One tick: under the advisory lock, claim ONE job and process it to completion (or a terminal state),
    then release. The next tick picks up the next job — one at a time, FIFO. If another replica holds the
    lock, skip. If the lock can't be released cleanly, CLOSE the connection rather than return a
    lock-holding one to the pool (mirrors the maintenance discipline)."""
    async with get_pool().connection() as conn:
        if not await _try_lock(conn):
            return  # another replica owns the worker this tick
        try:
            job = await svc.claim_next_job(conn)
            if job is not None:
                await svc.process_job(conn, job)
        finally:
            try:
                await _unlock(conn)
            except Exception:  # noqa: BLE001
                await conn.close()
                log.warning("geocode_worker_unlock_failed_conn_closed", exc_info=True)


async def run_geocode_worker_loop() -> None:
    """The lifespan loop. Sleep a tick, process one due job, repeat. A failed tick is logged and retried;
    the loop exits only on cancellation (app shutdown)."""
    tick = settings.geocode_worker_tick_seconds
    log.info("geocode_worker_started", tick_seconds=tick)
    try:
        while True:
            await asyncio.sleep(tick)
            try:
                await _run_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never let a bad tick kill the loop
                log.warning("geocode_worker_tick_failed", exc_info=True)
    except asyncio.CancelledError:
        log.info("geocode_worker_stopped")
        raise
