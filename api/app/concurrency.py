"""Optimistic-concurrency primitives (Talus 34).

Same-feature multi-editor safety. A feature write must carry the row version the
client read — the row's durable `row_version` (bigint, V035; stamped from
`pg_current_xact_id()`), surfaced on reads as an opaque `version` (ETag) string.
The guarded write adds `... AND row_version = %(expected)s::bigint`; a stale write
touches 0 rows and we answer **409** with the CURRENT server row so the UI can show
"changed by X" and refresh/merge. A write that omits the token is rejected **428** —
there is no silent last-write-wins path left.

`row_version` is chosen over `xmin` (32-bit, wraps, VACUUM FREEZE rewrites it — unfit
for a token a field device holds offline) and `updated_at` (not universal; timestamp
round-trip/collision fragility): it is durable, monotonic, non-wrapping, and DOUBLES
AS the future disconnected-editing sync watermark (`WHERE row_version > :watermark`).
See docs/decision-optimistic-concurrency.md (§3a).
"""
from typing import Any

from fastapi import Header, HTTPException, status
from fastapi.encoders import jsonable_encoder
from psycopg import AsyncConnection
from psycopg.rows import dict_row

# row_version is a signed bigint (PostgreSQL bigint upper bound).
_VERSION_MAX = 9223372036854775807


def require_if_match(
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> str:
    """Mandatory row-version token for a guarded UPDATE/DELETE (FastAPI dependency).

    Returns the version as a string ready to bind as `%(expected)s::bigint`. Tolerates
    ETag quoting (`"838"`, `W/"838"`). Missing/blank -> 428 Precondition Required;
    non-numeric / out-of-range -> 400. `*` (ETag wildcard) is intentionally NOT
    supported — it would be a silent force-overwrite, defeating the guard the slice
    exists to provide; a deliberate overwrite re-submits with the server's new
    version instead."""
    if if_match is None or not if_match.strip():
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match (row version) is required for this write",
        )
    token = if_match.strip()
    if token.startswith("W/"):  # weak-validator prefix
        token = token[2:].strip()
    token = token.strip('"')
    if not token.isdigit() or int(token) > _VERSION_MAX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="malformed If-Match version token",
        )
    return token


async def version_conflict_or_404(
    conn: AsyncConnection,
    by_id_sql: Any,
    params: dict[str, Any],
    entity_label: str,
) -> None:
    """Called when a version-guarded write touched 0 rows: re-read the row by id
    ALONE to distinguish a vanished row (404) from a stale-version collision
    (409 + the current server row). Always raises; never returns.

    `by_id_sql` is the entity's GET-one query (string or psycopg Composed); it must
    SELECT the row's current `version` so the client can retry against it. Runs on
    the still-open (uncommitted) transaction — the raise rolls it back."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(by_id_sql, params)
        current = await cur.fetchone()
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{entity_label} not found",
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": f"{entity_label} changed since you loaded it",
            "current": jsonable_encoder(current),
        },
    )
