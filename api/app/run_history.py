"""Failed pull and import runs, as a diagnostic read.

"What is wrong with the data?" is answered from the rows that already record it — platform.sql_pull_runs
and platform.import_jobs — not from whether a WARNING happened to be in the ring at the time. Both tables
store a bounded, admin-visible reason (≤ 300 chars: a SourceError's fixed sentence, or the first line of
an exception; the never-list keeps a password out of a SourceError by construction) and a bounded
row-level error sample. This read returns the last `limit_each` failed of each, newest first, in one
flat list of {kind, name, layer, when, reason, …}. Nothing here is new data: every value is what the
Catalog's run history already shows the owner.

Core: a generic read over core tables (the register row in decision-code-boundary.md §4); it knows
nothing about who consumes it. The paid Helper's diagnose snapshot is today's only consumer.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

DEFAULT_LIMIT_EACH = 10
SAMPLE_MAX = 3            # row-level error-sample entries carried per run
REASON_CHARS = 300        # the tables' own cap, re-applied on read

_PULLS_SQL = """
SELECT r.id, r.trigger, r.error, r.failed_rows, r.processed_rows, r.total_rows, r.error_sample,
       COALESCE(r.finished_at, r.updated_at) AS at,
       c.name AS connection, p.source_schema, p.source_table, l.display_name AS layer
  FROM platform.sql_pull_runs r
  JOIN platform.sql_pulls p ON p.id = r.pull_id
  JOIN platform.sql_connections c ON c.id = p.connection_id
  LEFT JOIN platform.layers l ON l.layer_id = p.target_layer_id
 WHERE r.status = 'failed'
 ORDER BY COALESCE(r.finished_at, r.updated_at) DESC
 LIMIT %s
"""

_IMPORTS_SQL = """
SELECT j.id, j.format, j.original_name, j.error, j.failed_rows, j.processed_rows, j.total_rows, j.error_sample,
       COALESCE(j.finished_at, j.updated_at) AS at,
       l.display_name AS layer
  FROM platform.import_jobs j
  LEFT JOIN platform.layers l ON l.layer_id = j.target_layer_id
 WHERE j.status = 'failed'
 ORDER BY COALESCE(j.finished_at, j.updated_at) DESC
 LIMIT %s
"""


def _sample(raw: Any) -> list[dict[str, Any]]:
    """The first SAMPLE_MAX row-level entries, each bounded — [{row, reason}] as the workers store it."""
    out: list[dict[str, Any]] = []
    for e in (raw or [])[:SAMPLE_MAX] if isinstance(raw, list) else []:
        if isinstance(e, dict):
            out.append({"row": e.get("row"), "reason": str(e.get("reason") or "")[:REASON_CHARS]})
    return out


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def pull_row_to_entry(r: dict[str, Any]) -> dict[str, Any]:
    """A failed sql_pull_runs row (joined) → the section entry. Pure."""
    return {
        "kind": "sql_pull",
        "name": f"{r.get('connection')} → {r.get('source_schema')}.{r.get('source_table')}",
        "layer": r.get("layer"),
        "when": _iso(r.get("at")),
        "trigger": r.get("trigger"),
        "reason": str(r.get("error") or "")[:REASON_CHARS] or None,
        "total_rows": r.get("total_rows"),
        "processed_rows": r.get("processed_rows"),
        "failed_rows": r.get("failed_rows"),
        "error_sample": _sample(r.get("error_sample")),
    }


def import_row_to_entry(r: dict[str, Any]) -> dict[str, Any]:
    """A failed import_jobs row (joined) → the section entry. `original_name` is the client's display
    filename (never a path — the V089 rule). Pure."""
    return {
        "kind": "import",
        "name": f"{r.get('format')}: {r.get('original_name')}",
        "layer": r.get("layer"),
        "when": _iso(r.get("at")),
        "reason": str(r.get("error") or "")[:REASON_CHARS] or None,
        "total_rows": r.get("total_rows"),
        "processed_rows": r.get("processed_rows"),
        "failed_rows": r.get("failed_rows"),
        "error_sample": _sample(r.get("error_sample")),
    }


def merge_newest_first(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure: one list across kinds, newest first (an entry without a time sorts last)."""
    return sorted(entries, key=lambda e: e.get("when") or "", reverse=True)


async def recent_failed_runs(conn: AsyncConnection, limit_each: int = DEFAULT_LIMIT_EACH) -> list[dict[str, Any]]:
    """The last `limit_each` failed SQL-pull runs and the last `limit_each` failed import jobs, newest first."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_PULLS_SQL, (limit_each,))
        pulls = [pull_row_to_entry(r) for r in await cur.fetchall()]
        await cur.execute(_IMPORTS_SQL, (limit_each,))
        imports = [import_row_to_entry(r) for r in await cur.fetchall()]
    return merge_newest_first(pulls + imports)
