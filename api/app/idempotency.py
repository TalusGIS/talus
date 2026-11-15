"""Create-idempotency for the offline edit-queue replay (Talus 35, Phase A).

A queued offline CREATE replayed after a crash-mid-send would DUPLICATE a feature —
POST is not `If-Match`-guarded (unlike UPDATE/DELETE). The client sends its `clientOpId`
(a uuid) as an `Idempotency-Key` header on create; the create endpoint records the key in
`platform.applied_op_keys` (V036) in the SAME transaction as the insert, and on replay
returns the already-created feature instead of inserting again.

update/move/delete need no key — re-applying carries the now-stale `row_version` -> 409,
not a silent double-apply (the V035 `If-Match` guard makes replay self-protecting). See
docs/decision-disconnected-editing.md §7.

The header is OPTIONAL: absent -> the create behaves exactly as before (a plain insert, no
key recorded), so the API is safe even before V036 is applied; only offline-create replay
needs the table.
"""
from uuid import UUID

from fastapi import Header, HTTPException, status
from psycopg import AsyncConnection
from psycopg.rows import dict_row


def optional_idempotency_key(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str | None:
    """Parse the optional `Idempotency-Key` create header (FastAPI dependency).

    Absent/blank -> None. Present-but-not-a-UUID -> 400 (the client only ever sends a uuid
    `clientOpId`)."""
    if idempotency_key is None or not idempotency_key.strip():
        return None
    key = idempotency_key.strip()
    try:
        UUID(key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="malformed Idempotency-Key (must be a UUID)",
        ) from exc
    return key


async def _keys_table_present(conn: AsyncConnection) -> bool:
    """Is the V036 idempotency table applied yet? `to_regclass` returns NULL (no exception, so the
    open transaction stays clean) when the table is absent — letting the feature degrade gracefully to
    a plain create before the migration lands, rather than 500-ing on a missing table."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT to_regclass('platform.applied_op_keys')")
        row = await cur.fetchone()
    return row is not None and row[0] is not None


async def lookup_applied_op(
    conn: AsyncConnection, op_key: str | None, item_type: str
) -> str | None:
    """The feature id a prior create recorded under this key, or None if the key is unset, the V036
    table isn't applied yet, or the key is unseen. Runs on the caller's open transaction (a second
    cursor shares it), so it sees a record_applied_op write from an earlier op in the same request."""
    if op_key is None or not await _keys_table_present(conn):
        return None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT item_id FROM platform.applied_op_keys WHERE op_key = %s AND item_type = %s",
            (op_key, item_type),
        )
        row = await cur.fetchone()
    return row["item_id"] if row else None


async def record_applied_op(
    conn: AsyncConnection, op_key: str | None, item_type: str, item_id: str
) -> None:
    """Record a freshly-created feature's id under its idempotency key, in the caller's transaction
    (so it commits atomically with the insert). No-op when the key is unset or V036 isn't applied yet.
    `ON CONFLICT DO NOTHING` is a backstop for a pathological concurrent double-drain."""
    if op_key is None or not await _keys_table_present(conn):
        return
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO platform.applied_op_keys (op_key, item_type, item_id) "
            "VALUES (%s, %s, %s) ON CONFLICT (op_key) DO NOTHING",
            (op_key, item_type, str(item_id)),
        )
