"""Content last-accessed stamp — the cheap stale-content signal (admin-reports-round2 §5.4, V076).

Call `touch()` at the END of an API READ handler (layer features / export, web-map GET) to record that
a content item was accessed. THROTTLED to at most once/hour per item (a no-op UPDATE via the WHERE guard
otherwise) and BEST-EFFORT — any failure rolls back and logs; it must never break the read it rides on.
Mirrors auth._touch_last_seen.

NORTH-STAR FENCE (decision §1): one timestamp = "when was this last touched via the API". Deliberately
NOT a counter, NOT per-user, NOT a time series, NEVER surfaced as "views". Tiles are served by
pg_tileserv (SELECT-only under S-1) and cannot stamp — this is honestly "last API read", not "last tile".
"""
from __future__ import annotations

import structlog
from psycopg import AsyncConnection

log = structlog.get_logger(__name__)

# kind -> (table, key column). FIXED internal constants — never user input (safe to interpolate).
_TARGETS: dict[str, tuple[str, str]] = {
    "layer": ("platform.layers", "layer_id"),
    "web_map": ("platform.web_maps", "id"),
}


async def touch(conn: AsyncConnection, kind: str, item_id: str) -> None:
    """Best-effort, throttled (<=1/hour) 'last accessed' stamp for a content item. Never raises."""
    target = _TARGETS.get(kind)
    if target is None:
        return
    table, key = target
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE {table} SET last_accessed_at = now() "  # noqa: S608 — table/key are fixed constants
                f"WHERE {key} = %s AND (last_accessed_at IS NULL "
                f"OR last_accessed_at < now() - interval '1 hour')",
                (item_id,),
            )
        await conn.commit()
    except Exception as exc:  # noqa: BLE001 — non-critical side effect; never break the read
        log.warning("last_accessed_touch_failed", kind=kind, item=item_id, detail=str(exc))
        try:
            await conn.rollback()
        except Exception:  # noqa: BLE001
            pass
