"""Personal access tokens (interop I-1, decision-desktop-interop-i1.md §2).

The owner-facing lifecycle for the desktop/script credential the auth seam resolves
(auth._pat_user): mint (plaintext shown ONCE), list (metadata only — the secret is
unrecoverable by design), revoke (soft — the row stays as an audit breadcrumb and the
auth seam rejects it immediately). Managed with a BEARER session only (you administer
tokens in the portal; a PAT cannot mint or revoke PATs — get_current_user never
resolves PATs). Any authenticated HUMAN may mint: viewers consuming read-only OGC in
QGIS is exactly the use case; the sharing model, not the token, is the access control.

Scope is recorded now ('read_only' default) so I-3's FeatureServer editing needs no
reshape; in I-1 PATs only ride the read surfaces, so read_write grants nothing yet.
"""
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from pydantic import BaseModel, Field, field_validator

from ..auth import get_current_user
from ..db import get_db
from ..models.user import User
from ..pat_service import SUMMARY_COLS, TokenCapReached, mint_pat, revoke_pat
from ..ratelimit import forget_validated_pat

log = structlog.get_logger(__name__)

router = APIRouter(tags=["personal-access-tokens"])


class TokenCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    scope: str = Field(default="read_only", pattern=r"^(read_only|read_write)$")
    # Optional lifetime in days (revoke-first posture: default = no expiry).
    expiresInDays: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("label")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("label must not be blank")
        return v


class TokenSummary(BaseModel):
    id: str
    label: str
    scope: str
    created_at: Any
    expires_at: Any = None
    last_used_at: Any = None
    revoked_at: Any = None


class TokenMinted(TokenSummary):
    """The mint response — the ONLY place the plaintext token ever appears."""

    token: str


_SUMMARY_COLS = SUMMARY_COLS


def _to_summary(row: tuple) -> TokenSummary:
    return TokenSummary(
        id=str(row[0]), label=row[1], scope=row[2], created_at=row[3],
        expires_at=row[4], last_used_at=row[5], revoked_at=row[6],
    )


@router.post(
    "/me/tokens",
    status_code=status.HTTP_201_CREATED,
    summary="Mint a personal access token (plaintext shown once)",
)
async def mint_token(
    payload: TokenCreate,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TokenMinted:
    # Humans only: a service account already has its own credential; a PAT minted by one would
    # be a credential begetting a credential with no human owner to revoke it.
    if user.principal_type == "service":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="service accounts cannot mint personal access tokens",
        )
    # The mint itself lives in pat_service (shared with field-project generation) — this
    # route stays the owner-facing shell: cap -> 409, commit, then the audit event.
    async with conn.cursor() as cur:
        try:
            token, row = await mint_pat(
                cur, user.id, payload.label, payload.scope, payload.expiresInDays
            )
        except TokenCapReached as exc:
            await conn.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    await conn.commit()
    log.info(  # the token itself is NEVER logged
        "pat_minted", token_id=str(row[0]), owner=str(user.id),
        scope=payload.scope, expires=payload.expiresInDays is not None,
    )
    return TokenMinted(**_to_summary(row).model_dump(), token=token)


@router.get("/me/tokens", summary="List the caller's personal access tokens (metadata only)")
async def list_tokens(
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, list[TokenSummary]]:
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_SUMMARY_COLS} FROM platform.personal_access_tokens "
            "WHERE owner_id = %s ORDER BY created_at DESC",
            (user.id,),
        )
        rows = await cur.fetchall()
    return {"tokens": [_to_summary(r) for r in rows]}


@router.delete(
    "/me/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a token (default), or permanently remove an already-revoked one (?purge=true)",
)
async def revoke_token(
    token_id: str,
    purge: bool = False,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    # Two owner-scoped actions on one route (both are "DELETE"), by state:
    #  - default: SOFT-revoke — kills access immediately, keeps the row + logs the event.
    #    Idempotent (re-revoking is a no-op 204).
    #  - ?purge=true: HARD-delete the record — list cleanup, allowed ONLY on a DEAD token (revoked
    #    OR expired; 409 on a live one → "revoke it first"), so access is always already cut BEFORE
    #    the record is removed. The durable audit trail lives in the structured logs (pat_minted /
    #    pat_revoked_by_owner / pat_purged), not this row, so purging loses no audit-critical
    #    breadcrumb. Expiry is judged on the DB clock (no host skew), matching the auth arm.
    # Someone else's token id is indistinguishable from a missing one (404 either way — no oracle).
    if purge:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM platform.personal_access_tokens "
                "WHERE id::text = %s AND owner_id = %s "
                "AND (revoked_at IS NOT NULL OR (expires_at IS NOT NULL AND expires_at < now())) "
                "RETURNING id",
                (token_id, user.id),
            )
            deleted = await cur.fetchone()
            if deleted is None:
                # Nothing removed: either the token doesn't exist (→404), or it's still LIVE (→409).
                await cur.execute(
                    "SELECT 1 FROM platform.personal_access_tokens "
                    "WHERE id::text = %s AND owner_id = %s",
                    (token_id, user.id),
                )
                exists = await cur.fetchone() is not None
                await conn.rollback()
                if exists:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="revoke the token before removing it",
                    )
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
        await conn.commit()
        log.info("pat_purged", token_id=token_id, owner=str(user.id))
        return

    async with conn.cursor() as cur:
        revoked = await revoke_pat(cur, token_id, owner_id=user.id)
    await conn.commit()
    if revoked is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    # Collapse the rate-bucket TTL window too (the auth seam re-checks the DB per request
    # regardless — this is the documented ratelimit.py posture, previously missed here).
    forget_validated_pat(revoked[1])
    log.info("pat_revoked_by_owner", token_id=token_id, owner=str(user.id))
