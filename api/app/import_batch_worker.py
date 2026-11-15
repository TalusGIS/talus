"""Bulk import — the async worker loop (decision-9d-bulk-import.md §4).

Mirrors the batch-geocoding worker exactly (geocode_batch_worker.py): a lifespan-managed
asyncio loop under its OWN Postgres advisory lock so exactly one replica processes import
jobs, fail-safe (a bad job is marked failed by the service, never crashes the loop), one
job at a time, FIFO. Like the geocode worker — and unlike maintenance — it legitimately
writes authoritative features (the imported rows), so it lives outside the cruft fence.

Resumable by construction: batches are the atomicity unit (INSERT + counters in one
transaction), so a reclaimed 'running' job skips processed_rows source rows and continues.
"""
from __future__ import annotations

import asyncio

import structlog

from . import import_jobs_service as svc
from .config import settings
from .db import get_pool

log = structlog.get_logger(__name__)

# A DISTINCT advisory-lock key (maintenance 3_900_000_039 · geocode 8_100_000_081) —
# namespaced to "bulk import / V089" and documented here so nothing else reuses it.
_ADVISORY_LOCK_KEY = 9_100_000_089


async def _try_lock(conn) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        row = await cur.fetchone()
    return bool(row and row[0])


async def _unlock(conn) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))


async def _run_due() -> None:
    """One tick: under the lock, claim ONE job and drive it to a terminal state, then
    release. Lock-release failure closes the connection rather than returning a
    lock-holding one to the pool (the maintenance discipline)."""
    async with get_pool().connection() as conn:
        if not await _try_lock(conn):
            return
        try:
            job = await svc.claim_next_job(conn)
            if job is not None:
                await svc.process_job(conn, job)
        finally:
            try:
                await _unlock(conn)
            except Exception:  # noqa: BLE001
                await conn.close()
                log.warning("import_worker_unlock_failed_conn_closed", exc_info=True)


async def run_import_worker_loop() -> None:
    """Sleep a tick, process one due job, repeat. A failed tick is logged and retried;
    the loop exits only on cancellation (app shutdown)."""
    tick = settings.import_worker_tick_seconds
    log.info("import_worker_started", tick_seconds=tick)
    try:
        while True:
            await asyncio.sleep(tick)
            try:
                await _run_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never let a bad tick kill the loop
                log.warning("import_worker_tick_failed", exc_info=True)
    except asyncio.CancelledError:
        log.info("import_worker_stopped")
        raise
