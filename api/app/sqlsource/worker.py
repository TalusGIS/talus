"""SQL-pull run worker (decision-sql-source-pull.md §3) — the FOURTH job class, beside maintenance,
batch geocoding and the bulk import: an asyncio loop under its OWN Postgres advisory lock so
exactly one replica processes runs. It writes authoritative rows (a pulled copy), so it lives
outside the housekeeping cruft fence by construction, like the other two feature workers.

A DISTINCT advisory-lock key (maintenance 3_900_000_039 · geocode 8_100_000_081 ·
import 9_100_000_089 · basemap 8_400_000_084). Lock-release failure closes the connection
rather than returning a lock-holding one to the pool (the maintenance discipline).
"""
from __future__ import annotations

import asyncio

import structlog

from ..config import settings
from ..db import get_pool
from . import service as svc

log = structlog.get_logger(__name__)

_ADVISORY_LOCK_KEY = 7_700_000_110


async def _try_lock(conn) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        row = await cur.fetchone()
    return bool(row and row[0])


async def _unlock(conn) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))


async def _run_due() -> None:
    """One tick: under the lock, claim ONE run and drive it to a terminal state, then release."""
    async with get_pool().connection() as conn:
        if not await _try_lock(conn):
            return
        try:
            # S3-B: the scheduler half of the tick — due pulls get a run under their owner's identity.
            try:
                await svc.enqueue_due(conn)
            except Exception:  # noqa: BLE001 — a scheduling hiccup never blocks the run half
                await conn.rollback()
                log.warning("sql_pull_schedule_tick_failed", exc_info=True)
            run = await svc.claim_next_run(conn)
            if run is not None:
                await svc.process_run(conn, run)
        finally:
            try:
                await _unlock(conn)
            except Exception:  # noqa: BLE001
                await conn.close()
                log.warning("sql_pull_worker_unlock_failed_conn_closed", exc_info=True)


async def run_sql_pull_worker_loop() -> None:
    """Sleep a tick, process one due run, repeat. A failed tick is logged and retried; the loop
    exits only on cancellation (app shutdown)."""
    tick = settings.sql_pull_worker_tick_seconds
    log.info("sql_pull_worker_started", tick_seconds=tick)
    try:
        while True:
            await asyncio.sleep(tick)
            try:
                await _run_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never let a bad tick kill the loop
                log.warning("sql_pull_worker_tick_failed", exc_info=True)
    except asyncio.CancelledError:
        log.info("sql_pull_worker_stopped")
