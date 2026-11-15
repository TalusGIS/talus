"""Governance-action audit log — the write side of platform.admin_audit (V074).

The Admin -> Reports Activity report (decision-admin-reports-round2.md) needs a queryable record of
governance actions (role change, share/unshare, ownership transfer, disable/enable user, public-curate,
delete-protection toggle, backup-settings change). Those actions are API-level and span Keycloak/OpenFGA,
so PG triggers can't see them — every site calls `record()` explicitly, at the spot its existing
structlog line sits, AFTER the action's durable effect (KC/OpenFGA/PG).

Design (decision §F6): BEST-EFFORT. Most governance actions mutate Keycloak or OpenFGA, not PG, so
there is no shared transaction to be atomic with and no way to roll the external change back — failing
the request because an audit INSERT failed would return a 500 for an action that already took effect.
So `record()` inserts + commits its own row and, on failure, logs `admin_audit_write_failed` and returns
without disturbing the already-applied action. This mirrors auth._touch_last_seen and is strictly better
than the structlog-only trail it augments.

GOVERNANCE-WIDE (§F1): the caller passes the server-derived actor (`_admin.id` / `user.id`) — NEVER a
client field — whoever performed the action. `detail` carries shapes/counts/flags only, NEVER secret
values (tokens, keys, recipient addresses); see decision §6.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

log = structlog.get_logger(__name__)


async def record(
    conn: AsyncConnection,
    *,
    actor_id: UUID | str,
    action: str,
    actor_type: str = "human",
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one governance-action row (best-effort). Call AFTER the action has taken effect, at the
    handler's existing structlog site. Never raises — a failed insert is logged, never propagated, so it
    can't fail the already-applied action."""
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO platform.admin_audit "
                "(actor_id, actor_type, action, target_type, target_id, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    str(actor_id),
                    actor_type,
                    action,
                    target_type,
                    target_id,
                    Jsonb(detail or {}),
                ),
            )
        await conn.commit()
    except Exception as exc:  # noqa: BLE001 — audit is non-critical to the applied action (§F6)
        log.warning(
            "admin_audit_write_failed",
            action=action,
            actor=str(actor_id),
            target_type=target_type,
            target_id=target_id,
            detail=str(exc),
        )
        try:
            await conn.rollback()
        except Exception:  # noqa: BLE001
            pass
