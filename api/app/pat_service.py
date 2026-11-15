"""Shared PAT mint/revoke primitives (decision-generate-field-project.md §F1).

Extracted from routers/pats.py so the owner-facing routes and the server-side flows
(field-project generation, item-delete auto-revoke) share ONE implementation and one
audit-event family. Contract:

  * Both functions operate on an OPEN CURSOR and never commit — the caller owns the
    transaction. Generation rides its mint uncommitted so a failed store rolls the token
    back (no cleanup-revoke to forget); item delete revokes transactionally with the row.
  * Callers emit the structlog events (pat_minted / pat_revoked_*) AFTER their commit,
    matching the original routes' ordering — never log a mint that didn't happen.
  * After committing a revoke, callers MUST pass the returned secret_hash to
    ratelimit.forget_validated_pat — the auth seam re-checks the DB per request either
    way, but forgetting the hash collapses the rate-bucket TTL window to zero (the
    documented posture in ratelimit.py).
"""
from __future__ import annotations

import hashlib
import secrets

# A sane per-user ceiling — enough for every device + a script or two, small enough that a
# runaway minter is obvious. Revoked tokens don't count against it.
MAX_LIVE_TOKENS = 25

SUMMARY_COLS = "id, label, scope, created_at, expires_at, last_used_at, revoked_at"


class TokenCapReached(Exception):
    """The owner already holds MAX_LIVE_TOKENS live tokens — callers map this to a 409."""


async def mint_pat(
    cur,
    owner_id,
    label: str,
    scope: str = "read_only",
    expires_in_days: int | None = None,
) -> tuple[str, tuple]:
    """Insert a new PAT for owner_id; returns (plaintext, summary_row). The plaintext exists
    only in the return value — storage is sha256-hex, unrecoverable by design. Raises
    TokenCapReached at the live-token ceiling. Does NOT commit and does NOT log."""
    if scope not in ("read_only", "read_write"):
        raise ValueError(f"bad PAT scope {scope!r}")
    await cur.execute(
        "SELECT count(*) FROM platform.personal_access_tokens "
        "WHERE owner_id = %s AND revoked_at IS NULL",
        (owner_id,),
    )
    live = (await cur.fetchone())[0]
    if live >= MAX_LIVE_TOKENS:
        raise TokenCapReached(
            f"token limit reached ({MAX_LIVE_TOKENS}); revoke one you no longer use"
        )
    # The secret: prefix + 32 urlsafe bytes. Stored ONLY as sha256 hex; plaintext returned once.
    token = "tpat_" + secrets.token_urlsafe(32)
    await cur.execute(
        "INSERT INTO platform.personal_access_tokens (owner_id, label, secret_hash, scope, expires_at) "
        "VALUES (%(owner)s, %(label)s, %(hash)s, %(scope)s, "
        "        CASE WHEN %(days)s::int IS NULL THEN NULL ELSE now() + make_interval(days => %(days)s::int) END) "
        f"RETURNING {SUMMARY_COLS}",
        {
            "owner": owner_id, "label": label,
            "hash": hashlib.sha256(token.encode()).hexdigest(),
            "scope": scope, "days": expires_in_days,
        },
    )
    row = await cur.fetchone()
    return token, row


async def revoke_pat(cur, token_id, owner_id=None) -> tuple[str, str] | None:
    """Soft-revoke (idempotent: an already-revoked token keeps its original revoked_at).
    owner_id scopes the match when given (the owner-facing route); None trusts the caller's
    own authorization (the item-delete path, gated by _load_for_manage upstream). Returns
    (token_id, secret_hash) or None if no row matched. Does NOT commit and does NOT log."""
    if owner_id is not None:
        await cur.execute(
            "UPDATE platform.personal_access_tokens SET revoked_at = COALESCE(revoked_at, now()) "
            "WHERE id::text = %s AND owner_id = %s RETURNING id, secret_hash",
            (str(token_id), owner_id),
        )
    else:
        await cur.execute(
            "UPDATE platform.personal_access_tokens SET revoked_at = COALESCE(revoked_at, now()) "
            "WHERE id::text = %s RETURNING id, secret_hash",
            (str(token_id),),
        )
    row = await cur.fetchone()
    if row is None:
        return None
    return str(row[0]), row[1]
