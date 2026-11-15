"""Talus 19 c4 — service-account administration (admin-gated).

A thin in-portal flow that does what scripts/bootstrap-19-service-accounts.sh +
grant-19-demo-sa.sh do by hand: create / list / rotate / disable a non-interactive service
account and scope it by GROUP MEMBERSHIP (the AGO model — decision-19 Q3/Q5). The portal
ORCHESTRATES + RECORDS; Keycloak ISSUES/ROTATES the actual secret (shown to the admin ONCE,
never stored by Talus — Q5).

Scope stays emergent: OpenFGA group membership × Keycloak realm role (Q4). A read-only SA gets
membership + NO edit role; a read-write SA also gets talus-data-editor (never publisher/admin —
no god tokens, Q8). There is deliberately NO group-management UI, NO per-permission editor, and
NO scope-DSL here — groups are PICKED from the platform.groups registry (V026); creating groups
stays on the existing publisher path. (North-Star drift guard: this is a provisioning flow, not
an identity console.)

Create ordering (mirrors publisher.py's "visible, recoverable state" discipline): Keycloak
client (the identity) -> platform.users principal row (the forced FK target) -> realm role ->
OpenFGA group grants. A failure partway leaves a fail-closed result — the SA's token is admitted
only once its row exists, and reads nothing until its group tuples land (default-deny). The
one-time secret is returned only on full success.

auth.py is untouched: a provisioned SA is admitted because its row EXISTS, exactly as today; an
unprovisioned sub is still rejected (no_platform_user_row). This endpoint adds NO bypass.
"""
import re
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from pydantic import BaseModel, field_validator

from .. import keycloak_admin
from ..authz import _openfga_read_user_groups, _openfga_write
from ..config import settings
from ..db import get_db
from ..deps import _require_admin
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["service-accounts"])

_MAX_NAME_LEN = 200

AccessMode = Literal["read-only", "read-write"]


# The admin gate (_require_admin) now lives in app.deps so service accounts, org settings, and the
# future user-admin slice share one authority. Imported above; behavior is unchanged here.


def _slug(name: str) -> str:
    """Lowercase; any run of non-alphanumeric -> single underscore; trim edges. Result is
    [a-z0-9_] (may be empty — caller rejects). Same discipline as publisher._slugify so a
    derived clientId / group ref never carries raw user text."""
    s = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return s.strip("_")


# --- request / response models --------------------------------------------------


class GroupOut(BaseModel):
    group_id: str
    display_name: str


class ServiceAccountCreate(BaseModel):
    name: str
    access_mode: AccessMode
    # The groups (platform.groups ids) whose layers this SA may touch. May be empty (a parked
    # SA that can reach nothing yet — fail-closed by default). Each must exist in the registry.
    groups: list[str] = []

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        if len(v) > _MAX_NAME_LEN:
            raise ValueError(f"name must be at most {_MAX_NAME_LEN} characters")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            raise ValueError("name must not contain control characters")
        return v


class ServiceAccountCreated(BaseModel):
    client_id: str
    # The client secret — shown ONCE. Talus never stores or can re-display it (Q5). The caller
    # must copy it now; a lost secret is recovered by rotate, not retrieval.
    secret: str
    sa_sub: str
    name: str
    access_mode: AccessMode
    groups: list[str]


class ServiceAccountOut(BaseModel):
    client_id: str
    name: str
    created_at: Any
    access_mode: AccessMode
    # None when the identity provider couldn't be reached for this row (list degrades rather
    # than failing); True/False otherwise.
    enabled: bool | None
    groups: list[GroupOut]


class SecretRotated(BaseModel):
    client_id: str
    secret: str


# --- groups registry (the picker source) ----------------------------------------


@router.get("/groups", summary="List groups the admin can scope a service account to")
async def list_groups(
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> dict[str, list[GroupOut]]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT group_id, display_name FROM platform.groups ORDER BY display_name"
        )
        rows = await cur.fetchall()
    return {"groups": [GroupOut(group_id=r[0], display_name=r[1]) for r in rows]}


# --- service accounts -----------------------------------------------------------


@router.get("/service-accounts", summary="List service accounts with their grants")
async def list_service_accounts(
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> dict[str, list[ServiceAccountOut]]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, kc_client_id, display_name, created_at FROM platform.users "
            "WHERE principal_type = 'service' ORDER BY created_at"
        )
        rows = await cur.fetchall()
        await cur.execute("SELECT group_id, display_name FROM platform.groups")
        group_names = {r[0]: r[1] for r in await cur.fetchall()}

    client_ids = [r[1] for r in rows if r[1]]
    # Two best-effort Keycloak reads for the whole list (resilient — {} on failure): which
    # clients are enabled, and which subs hold the data-editor role (=> read-write).
    enabled = await keycloak_admin.enabled_map(client_ids)
    rw_subs = await keycloak_admin.subs_with_realm_role(settings.keycloak_data_editor_role)

    out: list[ServiceAccountOut] = []
    for sa_id, client_id, name, created_at in rows:
        try:
            slugs = await _openfga_read_user_groups(sa_id)
        except HTTPException:
            slugs = []  # OpenFGA down — show the SA without its groups, don't fail the list
        groups = [GroupOut(group_id=s, display_name=group_names.get(s, s)) for s in slugs]
        out.append(
            ServiceAccountOut(
                client_id=client_id,
                name=name,
                created_at=created_at,
                access_mode="read-write" if str(sa_id) in rw_subs else "read-only",
                enabled=enabled.get(client_id),
                groups=groups,
            )
        )
    return {"service_accounts": out}


@router.post(
    "/service-accounts",
    status_code=status.HTTP_201_CREATED,
    summary="Create + scope a service account (secret shown once)",
)
async def create_service_account(
    payload: ServiceAccountCreate,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> ServiceAccountCreated:
    slug = _slug(payload.name)
    if not slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="name has no usable characters for a client id",
        )
    client_id = f"talus-sa-{slug}"

    # Reject unknown groups up front (before any Keycloak side effect): every picked group must
    # exist in the registry, so we never write a membership tuple to a phantom group.
    if payload.groups:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT group_id FROM platform.groups WHERE group_id = ANY(%s)",
                (payload.groups,),
            )
            known = {r[0] for r in await cur.fetchall()}
        unknown = [g for g in payload.groups if g not in known]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown group(s): {', '.join(sorted(unknown))}",
            )

    # 1. Keycloak: the identity + the one-time secret + the audience scope (409 if the id is taken).
    description = f"Talus service account: {payload.name} (provisioned via portal admin)."
    kc = await keycloak_admin.create_sa_client(client_id, payload.name, description)
    sa_sub = kc["sa_sub"]

    # 2. platform.users principal row — the forced NOT-NULL FK target every write needs (Q2).
    #    ON CONFLICT keeps re-provisioning idempotent (e.g. a retry after a downstream failure).
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO platform.users "
            "(id, username, email, display_name, principal_type, kc_client_id) "
            "VALUES (%s, %s, %s, %s, 'service', %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "username = EXCLUDED.username, email = EXCLUDED.email, "
            "display_name = EXCLUDED.display_name, principal_type = 'service', "
            "kc_client_id = EXCLUDED.kc_client_id",
            (
                sa_sub,
                f"service-account-{client_id}",
                f"service-account-{client_id}@talusgis.invalid",
                payload.name,
                client_id,
            ),
        )
    await conn.commit()

    # 3. Role: read-write gets talus-data-editor; read-only gets NO edit role (the three-key
    #    model then makes a write physically impossible — defense in depth, Q4).
    if payload.access_mode == "read-write":
        await keycloak_admin.assign_realm_role(sa_sub, settings.keycloak_data_editor_role)

    # 4. OpenFGA: content scope = group membership. Fails CLOSED (503) — a grant that can't be
    #    written must not report success. The SA reads nothing until these land (default-deny).
    if payload.groups:
        await _openfga_write(
            [(f"user:{sa_sub}", "member", f"group:{g}") for g in payload.groups]
        )

    log.info(
        "service_account_created",
        client_id=client_id,
        sa_sub=sa_sub,
        access_mode=payload.access_mode,
        groups=payload.groups,
        actor=str(_admin.id),
    )
    return ServiceAccountCreated(
        client_id=client_id,
        secret=kc["secret"],
        sa_sub=sa_sub,
        name=payload.name,
        access_mode=payload.access_mode,
        groups=payload.groups,
    )


async def _assert_is_service_principal(conn: AsyncConnection, client_id: str) -> None:
    """404 unless client_id names one of OUR service principals — so rotate/disable can never
    touch an arbitrary Keycloak client (e.g. talus-portal), only accounts this flow minted."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM platform.users "
            "WHERE kc_client_id = %s AND principal_type = 'service'",
            (client_id,),
        )
        if await cur.fetchone() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="service account not found"
            )


@router.post(
    "/service-accounts/{client_id}/rotate-secret",
    summary="Rotate a service account's secret (new value shown once)",
)
async def rotate_service_account_secret(
    client_id: str,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> SecretRotated:
    await _assert_is_service_principal(conn, client_id)
    secret = await keycloak_admin.regenerate_secret(client_id)
    log.info("service_account_secret_rotated", client_id=client_id, actor=str(_admin.id))
    return SecretRotated(client_id=client_id, secret=secret)


@router.post(
    "/service-accounts/{client_id}/disable",
    summary="Disable a service account (revokes new tokens)",
)
async def disable_service_account(
    client_id: str,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> dict[str, Any]:
    # Disabling the Keycloak client is the primary revocation (Q8): no new tokens issue, and the
    # short-lived (~5 min) access token expires fast. Group tuples are deliberately LEFT in place
    # (re-enable would otherwise silently lose scope); immediate FGA revocation is a later option.
    await _assert_is_service_principal(conn, client_id)
    await keycloak_admin.set_enabled(client_id, False)
    log.info("service_account_disabled", client_id=client_id, actor=str(_admin.id))
    return {"client_id": client_id, "enabled": False}
