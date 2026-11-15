"""Persisting the recent-warnings ring (V114).

The ring in logging_config.py is the api's only "what went wrong recently" record, and an api restart
empties it — at exactly the moment a box is asked about. This module copies new ring records to
platform.recent_warnings a few seconds after they are captured, tagged with the process's BOOT_ID, and
offers the merged read the diagnostic snapshot uses: memory first, then the table for what memory lost
(rows from an earlier boot, or rows the 200-deep ring has already rotated past).

Design constraints, each deliberate:
  * NEVER written from inside a log call. A structlog processor that wrote to Postgres would, on a
    Postgres failure, try to persist the warning about that failure into the database that is failing.
    So: a lifespan loop drains the ring on a timer (settings.recent_warnings_flush_seconds).
  * Its own failures log at INFO, never WARNING — a WARNING here lands in the ring this loop drains
    (a feedback loop). Consequence, accepted: the last few seconds before a hard crash
    may be lost, and warnings about Postgres being down cannot be persisted while it is down.
  * Same bounds as the ring: the records are the ring's already-scrubbed dicts (500 chars per field, the
    query-secret scrub applied before capture); the table keeps ≤ TABLE_MAX_ROWS, pruned after every write.
  * No lock: each api replica flushes its own ring under its own boot id; (boot_id, seq) is unique.
Core: a generic operational facility (the register row in decision-code-boundary.md §4); it knows nothing
about who reads it. The paid Helper is today's only consumer.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import structlog
from psycopg.types.json import Jsonb

from .config import settings
from .db import get_pool
from .logging_config import BOOT_ID, oldest_recent_seq, recent_warnings, recent_warnings_since

log = structlog.get_logger(__name__)

TABLE_MAX_ROWS = 500          # the persisted bound (the memory ring is 200)
REQUEST_MIN_PREFIX = 8        # an error code is the request id's first 8 hex chars (what the UI shows) or the whole uuid
REQUEST_MAX_RECORDS = 50      # the per-request read's bound
READ_MAX_ROWS = 200           # how many table rows the merged read returns (the newest of what memory lost)
_FLUSH_MAX_BACKOFF = 60

_last_flushed_seq = 0


def record_to_row(rec: dict[str, Any]) -> tuple:
    """The ring record → the V114 row shape. `at` parses the ring's ISO timestamp (None when absent);
    request_id is lifted out of `fields` (the middleware's contextvar) for the item-4 index."""
    ts = rec.get("timestamp")
    at: datetime | None = None
    if isinstance(ts, str):
        try:
            at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            at = None
    fields = dict(rec.get("fields") or {})
    rid = fields.get("request_id")
    level = str(rec.get("level") or "warning")
    if level not in ("warning", "error", "critical"):
        level = "warning"
    return (
        at,
        level,
        (str(rec["logger"])[:200] if rec.get("logger") is not None else None),
        str(rec.get("event") or "")[:500],
        Jsonb(fields),
        (str(rid)[:128] if rid else None),
    )


def row_to_record(row: dict[str, Any]) -> dict[str, Any]:
    """The V114 row → the ring's record shape, marked as coming from the table."""
    at = row.get("at")
    return {
        "timestamp": at.isoformat() if isinstance(at, datetime) else None,
        "level": row.get("level"),
        "logger": row.get("logger"),
        "event": row.get("event"),
        "fields": dict(row.get("fields") or {}),
        "source": "table",
    }


async def flush_once(conn) -> int:
    """Copy every ring record newer than the watermark, then prune the table to TABLE_MAX_ROWS.
    Returns the number of rows written. Raises on a database failure (the loop decides what to log)."""
    global _last_flushed_seq
    pending = recent_warnings_since(_last_flushed_seq)
    if not pending:
        return 0
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO platform.recent_warnings (boot_id, seq, at, level, logger, event, fields, request_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (boot_id, seq) DO NOTHING",
            [(BOOT_ID, seq, *record_to_row(rec)) for seq, rec in pending],
        )
        await cur.execute(
            "DELETE FROM platform.recent_warnings WHERE id <= COALESCE("
            "(SELECT id FROM platform.recent_warnings ORDER BY id DESC OFFSET %s LIMIT 1), 0)",
            (TABLE_MAX_ROWS,),
        )
    await conn.commit()
    _last_flushed_seq = pending[-1][0]
    return len(pending)


async def run_recent_warnings_flush_loop() -> None:
    """The lifespan-managed loop. Sleeps a tick, flushes, repeats; backs off (to 60 s) while the
    database refuses it; exits only on cancellation."""
    tick = max(1, int(settings.recent_warnings_flush_seconds))
    log.info("recent_warnings_persist_started", tick_seconds=tick, boot_id=BOOT_ID)
    delay = tick
    try:
        while True:
            await asyncio.sleep(delay)
            try:
                async with get_pool().connection() as conn:
                    await flush_once(conn)
                delay = tick
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never WARNING here (see the module docstring)
                log.info("recent_warnings_flush_failed", error=exc.__class__.__name__, retry_in_seconds=delay)
                delay = min(delay * 2, _FLUSH_MAX_BACKOFF)
    except asyncio.CancelledError:
        log.info("recent_warnings_persist_stopped")
        raise


async def persisted_warnings(conn, *, boot_id: str, oldest_seq_in_memory: int | None,
                             limit: int = READ_MAX_ROWS) -> list[dict[str, Any]]:
    """What memory lost: rows from another boot, or from this boot but older than the ring's oldest
    record. The newest `limit` of them, returned oldest first in the ring's record shape."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT at, level, logger, event, fields FROM platform.recent_warnings "
            "WHERE boot_id <> %s OR seq < %s ORDER BY id DESC LIMIT %s",
            (boot_id, oldest_seq_in_memory if oldest_seq_in_memory is not None else 0, limit),
        )
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in await cur.fetchall()]
    rows.reverse()
    return [row_to_record(r) for r in rows]


def merge(table_records: list[dict[str, Any]], memory_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure: the table's (older) records first, then memory's, each marked with its source."""
    return [dict(r, source="table") for r in table_records] + [dict(r, source="memory") for r in memory_records]


def memory_records_for_request(request_id: str) -> list[dict[str, Any]]:
    """Pure over the ring: the records whose bound request_id starts with `request_id` (oldest first)."""
    rid = request_id.strip()
    if len(rid) < REQUEST_MIN_PREFIX:
        return []
    return [r for r in recent_warnings() if str((r.get("fields") or {}).get("request_id") or "").startswith(rid)]


def like_prefix(s: str) -> str:
    """A LIKE pattern matching `s` as a prefix, with LIKE's own metacharacters escaped (the id is opaque
    input from an admin's paste; it reaches SQL only as a bound parameter, and only as a prefix)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def warnings_for_request(conn, request_id: str) -> list[dict[str, Any]]:
    """Item 4 ("paste the error code"): every ring/table record bound to a request id — memory first, then
    what memory lost — bounded to REQUEST_MAX_RECORDS. A table failure degrades to memory only."""
    rid = request_id.strip()
    if len(rid) < REQUEST_MIN_PREFIX:
        return []
    mem = memory_records_for_request(rid)
    table: list[dict[str, Any]] = []
    try:
        oldest = oldest_recent_seq()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT at, level, logger, event, fields FROM platform.recent_warnings "
                "WHERE (boot_id <> %s OR seq < %s) AND request_id LIKE %s ORDER BY id DESC LIMIT %s",
                (BOOT_ID, oldest if oldest is not None else 0, like_prefix(rid), REQUEST_MAX_RECORDS),
            )
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in await cur.fetchall()]
        rows.reverse()
        table = [row_to_record(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.info("recent_warnings_request_read_failed", error=exc.__class__.__name__)
    return merge(table, mem)[-REQUEST_MAX_RECORDS:]


async def recent_warnings_merged(conn) -> list[dict[str, Any]]:
    """The snapshot's read: memory first, then the table for what memory lost. A table failure degrades
    to memory only (logged at INFO, for the same reason the flusher does) — never an empty section."""
    mem = recent_warnings()
    try:
        table = await persisted_warnings(conn, boot_id=BOOT_ID, oldest_seq_in_memory=oldest_recent_seq())
    except Exception as exc:  # noqa: BLE001
        log.info("recent_warnings_table_read_failed", error=exc.__class__.__name__)
        table = []
    return merge(table, mem)
