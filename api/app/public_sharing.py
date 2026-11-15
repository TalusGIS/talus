"""The public-sharing master switch — the ONE place its value is resolved
(decision-public-sharing-reachable.md).

Public map sharing, the public map embed, and the Open Data / DCAT-US feed all hang off a single
boolean. Until V092 that boolean was `PUBLIC_ANON_ACCESS_ENABLED` alone: env-only, default false,
and therefore unreachable by the administrator who needed it — three shipped features dark on every
fresh install with no path through the product. V092 adds the missing top layer,
`platform.org_settings.public_sharing_enabled`, and this module is the resolution:

    effective = COALESCE(org_settings.public_sharing_enabled, settings.public_anon_access_enabled)

the Talus 43 backup-settings pattern (V042, `backup_settings.py`), not a new mechanism. NULL means
"not decided here — use the deployment value", which is what carries an existing public deployment
across the migration unchanged (F2's upgrade row).

★ WHY EVERY GATE MUST COME THROUGH HERE (§4 threat 6). The switch gates the anonymous READ path as
well as the write path — an item still carrying a `public_shared` tuple on a deployment whose
switch was flipped off is DENIED ("OFF ⇒ token-less ⇒ deny", authz.py). If one call site resolved
the value differently from another, the read gate could keep serving what the write gate refuses,
and an administrator who believed they had closed the door would still be publishing. So there is
exactly one resolver, no call site reads `settings.public_anon_access_enabled` directly, and
`inv29-public-sharing-toggle.sh` asserts that by grep.

RESOLVED PER CALL, NEVER CACHED. That is the entire no-restart mechanism (the env var needed a
container recreate) AND what makes "turning it off stops serving immediately" literally true rather
than true-within-a-TTL. The cost is one cheap singleton SELECT on the connection the caller already
holds — which is why every consumer passes its request connection instead of this module reaching
for a pool: a request that holds a pool slot and then waits for a second one can starve the pool.

FAIL-CLOSED: any read failure resolves to False (denied), matching the fail-safe posture of every
other gate in authz.py. A DB that cannot answer is not a DB that should be publishing to the
internet.

No cycle: this imports only config + psycopg. authz / org_settings / the routers import IT.
"""
from __future__ import annotations

import structlog
from psycopg import AsyncConnection

from .config import settings

log = structlog.get_logger(__name__)


async def _read_override(conn: AsyncConnection) -> tuple[bool, bool | None]:
    """(read_ok, column_value). One SELECT of the singleton's override column.

    ★ The SELECT runs inside `conn.transaction()`, which issues a SAVEPOINT when the caller is
    already in a transaction — and that is not decoration. This resolver runs on the CALLER'S
    connection, at the top of a gate, with the caller's own queries still to come. A bare failing
    statement (the column absent before V092 is applied; any transient error) would leave that
    connection in a failed-transaction state and every later query in the request would die with
    InFailedSqlTransaction — one gate's fail-closed turning into a 500 for the whole request. The
    savepoint contains the damage: the failure rolls back to it and the connection stays usable."""
    try:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT public_sharing_enabled FROM platform.org_settings WHERE id = true"
                )
                row = await cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — fail-closed: a switch we cannot read is OFF
        log.warning("public_sharing_read_failed", detail=str(exc))
        return False, None
    # Row missing is defensive only — V028 seeds exactly one.
    return True, (None if row is None else row[0])


async def public_sharing_enabled(conn: AsyncConnection) -> bool:
    """The EFFECTIVE public-sharing switch: the org override when set, else the deployment default.

    Call this — never `settings.public_anon_access_enabled` — from any gate, read or write, so the
    whole feature family can never disagree with itself. Fails CLOSED (False) on any read error."""
    ok, value = await _read_override(conn)
    if not ok:
        return False
    # NULL (or a missing row) means "not decided here" -> the deployment value. That fallback IS
    # the F2 upgrade row: an existing public deployment keeps serving across V092 untouched.
    if value is None:
        return settings.public_anon_access_enabled
    return bool(value)


async def public_sharing_source(conn: AsyncConnection) -> str:
    """'override' when an admin has decided it in the portal, 'default' while it still follows the
    deployment value. Powers the admin panel's default/custom affordance only — every GATE uses the
    resolved boolean above. Reports 'default' when the column cannot be read, which is the honest
    answer: nothing here is overriding anything."""
    ok, value = await _read_override(conn)
    return "override" if ok and value is not None else "default"
