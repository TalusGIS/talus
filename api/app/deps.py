"""Shared FastAPI dependencies.

`_require_admin` lives here (lifted from routers/service_accounts.py in Talus 22) so every
admin-gated router — service accounts, org settings, and the future user-admin slice — reuses
the EXACT same gate. UI gating in the SPA is cosmetic; this dependency is the real authority.
"""
from typing import Any

import structlog
from fastapi import Depends, HTTPException, status

from .auth import _validated_claims, get_current_user
from .authz import _has_admin_role
from .models.user import User

log = structlog.get_logger(__name__)


async def _require_admin(
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> User:
    """403 unless the caller holds talus-admin SPECIFICALLY (not merely publisher). Admin-gated
    capabilities (service-account + org-settings administration; later user-admin) reuse this;
    mirrors release-layer's admin gate. UI gating in the SPA is cosmetic — this is the authority."""
    if not _has_admin_role(claims):
        log.info(
            "authz_rejected",
            capability="admin",
            reason="missing_admin_role",
            user_id=str(user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized: admin role required",
        )
    return user
