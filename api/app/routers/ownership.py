"""Talus 26 — catalog ownership transfer (decision-catalog-ownership.md).

Item-type-generic owner transfer + the transfer-target picker:

  - **`GET /me/transfer-targets`** — the human users an item may be transferred to (the picker).
    Open to any authenticated caller (a member directory isn't sensitive; the real gate is the PUT).
  - **`POST /catalog/{item_type}/{item_id}/transfer`** — reassign an item's owner. **Admin always**
    (the offboarding path). A **non-admin OWNER** may transfer their OWN item only when the org
    policy `nonadmin_can_transfer_ownership` is ON (fail-SAFE read: any error denies). The item must
    already HAVE an owner — ownerless system layers are admin-managed, not transferable (F3). The
    target must be an existing **human** user (never a service account). Owner-only: the item's
    org/group/public shares are untouched. Loudly audited (`object_owner_transferred`).

Storage: ownership is the OpenFGA `owner` tuple for BOTH item types (moved via
`authz.transfer_object_owner`); a **web_map** ALSO carries a mutable `owner_id` column (V030, which
drives the cheap "Owned" list) so that is updated in the same request. `created_by` is never touched.
"""
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from pydantic import BaseModel

from .. import admin_audit
from ..sqlsource import service as sql_pulls
from ..auth import _validated_claims, get_current_user
from ..authz import (
    _has_admin_role,
    get_object_shares_and_owner,
    transfer_object_owner,
)
from ..db import get_db
from ..models.user import User
from ..org_settings import read_org_settings

log = structlog.get_logger(__name__)

router = APIRouter(tags=["ownership"])

# (item_type -> (table, id_column)) — the registry tables the transfer authorizes over.
_ITEM_TABLES: dict[str, tuple[str, str]] = {
    "layer": ("platform.layers", "layer_id"),
    "web_map": ("platform.web_maps", "id"),
    "raster": ("platform.rasters", "id"),
    "external": ("platform.external_refs", "id"),
    "field_project": ("platform.field_projects", "id"),
}
_ITEM_NOUN = {
    "layer": "layer", "web_map": "map", "raster": "raster", "external": "reference",
    "field_project": "field project",
}
# Item types carrying a mutable owner_id column (V030 web_maps, V045 rasters, V060 external_refs,
# V085 field_projects) — the Owned list keys on it, so transfer updates it with the OpenFGA owner
# tuple. Layers have no DB owner column (FGA-only).
_OWNER_ID_TABLES = {
    "web_map": "platform.web_maps",
    "raster": "platform.rasters",
    "external": "platform.external_refs",
    "field_project": "platform.field_projects",
}
# item_type -> OpenFGA object TYPE, where they differ: external refs ride the existing `layer`
# object type (decision-external-catalog-items.md §3.4 — ids are prefix-disjoint, so the share/
# authz/transfer seams apply verbatim with no model reload).
_FGA_OBJECT_TYPE = {"external": "layer"}


class TransferTarget(BaseModel):
    id: str
    display_name: str


class TransferRequest(BaseModel):
    newOwnerId: str


class TransferResult(BaseModel):
    itemType: str
    itemId: str
    newOwnerId: str
    newOwnerName: str | None
    previousOwnerId: str | None


@router.get("/me/transfer-targets", summary="Human users an item may be transferred to")
async def my_transfer_targets(
    conn: AsyncConnection = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, list[TransferTarget]]:
    """The ownership-transfer picker. Lists every HUMAN principal (never service accounts) — all
    humans are org members (Talus 21 backfill; the user-admin slice enforces it at creation), so
    principal_type='human' is the org-member criterion. Open to any authenticated caller; the real
    gate is POST .../transfer. The dialog excludes the item's current owner."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, COALESCE(display_name, username, email) FROM platform.users "
            "WHERE principal_type = 'human' ORDER BY COALESCE(display_name, username, email)"
        )
        rows = await cur.fetchall()
    return {"users": [TransferTarget(id=str(r[0]), display_name=r[1]) for r in rows]}


async def _require_item(conn: AsyncConnection, item_type: str, item_id: str) -> None:
    """400 for an unknown item_type, 404 if the item isn't registered (binds item_id as a param)."""
    if item_type not in _ITEM_TABLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown item type {item_type!r}"
        )
    table, id_col = _ITEM_TABLES[item_type]
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT 1 FROM {table} WHERE {id_col} = %s", (item_id,))  # noqa: S608 — table/col from a fixed allowlist
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item not found")


@router.post(
    "/catalog/{item_type}/{item_id}/transfer",
    summary="Transfer an item's ownership (admin always; owner when org policy allows)",
)
async def transfer_ownership(
    item_type: str,
    item_id: str,
    payload: TransferRequest,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> TransferResult:
    await _require_item(conn, item_type, item_id)
    object_ref = f"{_FGA_OBJECT_TYPE.get(item_type, item_type)}:{item_id}"
    noun = _ITEM_NOUN[item_type]

    # Current owner from OpenFGA (fail-closed 503). An ownerless item (system layer) is admin-managed
    # and NOT transferable — transfer is REASSIGNMENT of an owned item, not first-assignment (F3).
    _shares, current_owner = await get_object_shares_and_owner(object_ref)
    if current_owner is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"this {noun} has no owner to transfer (system items are administrator-managed)",
        )

    # Authority: admin ALWAYS (offboarding); else the caller must BE the current owner AND the org
    # policy must allow non-admin transfer (fail-SAFE — any policy-read error denies).
    if not _has_admin_role(claims):
        if str(user.id) != current_owner:
            log.info(
                "authz_rejected", capability="catalog:transfer", reason="not_owner_not_admin",
                user_id=str(user.id), object=object_ref,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"only the owner or an administrator can transfer this {noun}",
            )
        try:
            allowed = (await read_org_settings(conn)).nonadminCanTransferOwnership
        except Exception as exc:  # noqa: BLE001 — fail-safe: ANY failure denies
            log.warning("transfer_policy_read_failed", detail=str(exc))
            allowed = False
        if not allowed:
            log.info(
                "authz_rejected", capability="catalog:transfer", reason="org_policy_forbids_nonadmin",
                user_id=str(user.id), object=object_ref,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="organization policy does not allow non-admins to transfer ownership",
            )

    # Validate the target: a well-formed uuid that names an existing HUMAN user (no service accounts).
    try:
        UUID(payload.newOwnerId)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid newOwnerId")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT principal_type, COALESCE(display_name, username, email) "
            "FROM platform.users WHERE id = %s",
            (payload.newOwnerId,),
        )
        target = await cur.fetchone()
    if target is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown target user")
    if target[0] != "human":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="can only transfer ownership to a human user",
        )
    new_owner_name = target[1]

    # Move the OpenFGA owner tuple (fail-safe: new owner written before the old is dropped). For a
    # web_map, also update the mutable owner_id column (drives the Owned list). created_by untouched.
    previous_owner = await transfer_object_owner(object_ref, payload.newOwnerId)
    if item_type == "layer":
        # decision-sql-source-pull.md §11.2 L4: a layer kept up to date from a database pull is DETACHED when it
        # changes hands — the pull ran under the previous owner's personal connection, which cannot follow the
        # layer. The successor receives an ordinary, editable copy (audited as sql_pull.deactivated_by_lifecycle).
        await sql_pulls.detach_layer_pull(conn, item_id, actor=user.id, previous_owner=previous_owner,
                                          new_owner=payload.newOwnerId)
    if item_type in _OWNER_ID_TABLES:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE {_OWNER_ID_TABLES[item_type]} SET owner_id = %s WHERE id = %s",
                (payload.newOwnerId, item_id),
            )
        await conn.commit()

    # Loud, distinct audit line for the ownership change (decision §7).
    log.info(
        "object_owner_transferred", object=object_ref, actor=str(user.id),
        previous_owner=previous_owner, new_owner=payload.newOwnerId,
        by_admin=_has_admin_role(claims),
    )
    await admin_audit.record(
        conn, actor_id=user.id, action="object_owner_transferred",
        target_type=item_type, target_id=item_id,
        detail={"previousOwner": previous_owner, "newOwner": payload.newOwnerId,
                "byAdmin": _has_admin_role(claims)},
    )
    return TransferResult(
        itemType=item_type, itemId=item_id, newOwnerId=payload.newOwnerId,
        newOwnerName=new_owner_name, previousOwnerId=previous_owner,
    )
