"""Talus 23 + 25 — layer sharing.

The layer analog of the web-map share endpoints. Two surfaces over the SAME general object seam
(`authz.*_object_*`, keyed on a `layer:<id>` ref) and the SAME layer-management gate
(`manage_allowed` = owner OR admin → an OWNERLESS bespoke/system layer is admin-only,
decision-public-access.md §2.7/F3):

  - **Public-only** (Talus 23): `GET/PUT /layers/{id}/public-share` — the single-tier path the
    web-map make-public **cascade** drives per referenced layer. Left untouched (decision-layer-
    sharing.md F1).
  - **Full share state** (Talus 25): `GET/PUT /layers/{id}/shares` — Owner Only · Organization ·
    Groups · Public, mirroring `web_maps.set_web_map_shares` exactly (validate groups → member-scope
    for non-admins → gate public-ON → write org/group + public INDEPENDENTLY, F5). This backs the
    layer's own Manage dialog (true parity with web maps).

Turning public ON (either surface) passes `assert_may_share_public` (the F6 deploy switch + non-admin
org policy). Turning it OFF (un-publish) only reduces exposure, so it is always allowed for an
owner/admin and is deliberately NOT routed through that gate.
"""
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from psycopg import errors as pg_errors
from pydantic import BaseModel

from .. import admin_audit
from ..auth import _validated_claims, get_current_user
from ..authz import (
    OPEN_DATA_GROUP,
    PUBLIC_SYSTEM_GROUPS,
    _has_admin_role,
    _openfga_read_user_groups,
    assert_may_share_public,
    get_object_shares,
    manage_allowed,
    set_object_public,
    set_object_shares,
)
from ..db import get_db
from ..models.edit_ops import EditOps
from ..models.user import User
from .metadata import metadata_complete
# ShareGroup is the canonical (group_id, display_name) share model first defined for web maps
# (Talus 21); layers reuse it verbatim so the wire shape is identical across catalog item types.
from .web_maps import ShareGroup

log = structlog.get_logger(__name__)

router = APIRouter(tags=["layer-shares"])


class LayerPublicShare(BaseModel):
    """A layer's Public Access state + whether THIS caller may change it (drives the UI; the
    server is the authority)."""

    publicAccess: bool = False
    canManage: bool = False


class LayerPublicShareUpdate(BaseModel):
    publicAccess: bool


async def _require_registered(conn: AsyncConnection, layer_id: str) -> None:
    """layer_id must be a row in platform.layers, else 404 (binds layer_id as a parameter)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.layers WHERE layer_id = %s", (layer_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")


@router.get(
    "/layers/{layer_id}/public-share",
    summary="A layer's Public Access state + whether the caller may change it",
)
async def get_layer_public_share(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerPublicShare:
    # Public status isn't secret — and the cascade review (c3) must list a map's layers including
    # ones the caller may NOT manage — so this is open to any authenticated caller; `canManage`
    # tells the UI whether to offer the toggle. The real gate is on PUT.
    await _require_registered(conn, layer_id)
    shares = await get_object_shares(f"layer:{layer_id}")
    can_manage = await manage_allowed(claims, user, layer_id)
    return LayerPublicShare(publicAccess=shares["publicAccess"], canManage=can_manage)


@router.put(
    "/layers/{layer_id}/public-share",
    summary="Set/clear a layer's Public Access (owner or admin)",
)
async def set_layer_public_share(
    layer_id: str,
    payload: LayerPublicShareUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerPublicShare:
    await _require_registered(conn, layer_id)
    # owner-OR-admin (manage_allowed) — an ownerless system layer is therefore admin-only. 403 else.
    if not await manage_allowed(claims, user, layer_id):
        log.info(
            "authz_rejected", capability="layer:share_public",
            reason="not_owner_not_admin", user_id=str(user.id), layer=layer_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to change sharing for this layer",
        )
    # Gate only when turning public ON; un-publish (OFF) always allowed (reduces exposure).
    if payload.publicAccess:
        await assert_may_share_public(conn, claims)
    changed = await set_object_public(f"layer:{layer_id}", public=payload.publicAccess)
    if changed:
        log.info(
            "object_public_changed", object=f"layer:{layer_id}", actor=str(user.id),
            public=payload.publicAccess,
        )
        # The same governance record the full Share door writes (`layer_shares_set`), so the
        # security overview's "sharing changes on layers … are written to a separate activity log"
        # holds whichever door was used (once-over 2026-11-15 R7).
        await admin_audit.record(
            conn, actor_id=user.id, action="layer_public_share_set", target_type="layer",
            target_id=layer_id, detail={"public": payload.publicAccess},
        )
    return LayerPublicShare(
        publicAccess=payload.publicAccess,
        canManage=await manage_allowed(claims, user, layer_id),
    )


# --- full share state: org / group / public (Talus 25) --------------------------
#
# The org/group write path for layers, bringing them to parity with web maps. Mirrors
# web_maps.set_web_map_shares one-for-one, swapping its `_load_for_manage` gate for the layer's
# `manage_allowed` (owner-or-admin; ownerless system layers admin-only). NO authz.py change — the
# general object seam is already type-generic; NO migration; NO OpenFGA model reload (the `layer`
# type already defines all three share relations).


class LayerShares(BaseModel):
    """A layer's current/updated share state — mirrors WebMapShares for parity. `organizationOnly`
    = shared to the whole org (org_shared); `groups` = the additive group shares; `publicAccess` =
    "Public Access" (the anonymous public_shared tier, Talus 23) — an INDEPENDENT tier (F5)."""

    organizationOnly: bool = False
    groups: list[ShareGroup] = []
    publicAccess: bool = False


class LayerShareUpdate(BaseModel):
    """Replace a layer's share set. Empty (all false / []) clears all sharing. `groups` are
    platform.groups ids the caller may share to (member-scoped; enforced server-side).
    `publicAccess` turns the anonymous Public Access tier on/off — gated by the F6 deploy switch
    + org policy when turned ON (assert_may_share_public); turning it OFF always allowed."""

    organizationOnly: bool = False
    groups: list[str] = []
    publicAccess: bool = False


async def _layer_shares_response(conn: AsyncConnection, layer_id: str) -> LayerShares:
    """Read the layer's current share state and resolve group display names for the UI (the layer
    analog of web_maps._shares_response)."""
    shares = await get_object_shares(f"layer:{layer_id}")
    slugs: list[str] = shares["groups"]
    names: dict[str, str] = {}
    if slugs:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT group_id, display_name FROM platform.groups WHERE group_id = ANY(%s)",
                (slugs,),
            )
            names = {r[0]: r[1] for r in await cur.fetchall()}
    return LayerShares(
        organizationOnly=shares["organizationOnly"],
        groups=[ShareGroup(group_id=s, display_name=names.get(s, s)) for s in slugs],
        publicAccess=shares["publicAccess"],
    )


@router.get(
    "/layers/{layer_id}/shares",
    summary="A layer's full share state — org/group/public (owner or admin)",
)
async def get_layer_shares(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerShares:
    # Owner-or-admin: only someone who may MANAGE the layer needs its share config (recipients
    # don't). Mirrors the web-map GET /shares gate. The separate GET /public-share stays OPEN for
    # the make-public cascade; this unified read is management-scoped. 404 absent, 403 if not owner/admin.
    await _require_registered(conn, layer_id)
    if not await manage_allowed(claims, user, layer_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to manage this layer",
        )
    return await _layer_shares_response(conn, layer_id)


@router.put(
    "/layers/{layer_id}/shares",
    summary="Set/replace a layer's share state — org/group/public (owner or admin)",
)
async def set_layer_shares(
    layer_id: str,
    payload: LayerShareUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerShares:
    await _require_registered(conn, layer_id)
    # owner-OR-admin (manage_allowed) — an ownerless system layer is therefore admin-only. 403 else.
    if not await manage_allowed(claims, user, layer_id):
        log.info(
            "authz_rejected", capability="layer:share",
            reason="not_owner_not_admin", user_id=str(user.id), layer=layer_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to change sharing for this layer",
        )

    # De-dupe, preserve order. Validate every requested group BEFORE writing anything.
    requested = list(dict.fromkeys(payload.groups))
    if requested:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT group_id FROM platform.groups WHERE group_id = ANY(%s)", (requested,)
            )
            known = {r[0] for r in await cur.fetchall()}
        unknown = [g for g in requested if g not in known]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown group(s): {', '.join(sorted(unknown))}",
            )
        # Member-scoping (the real gate; the picker only scopes the UI). Admin may share to any
        # group; everyone else only to groups they are a member of — so a Signs-team publisher
        # can't surface a layer to Street Lights. _openfga_read_user_groups fails closed (503).
        if not _has_admin_role(claims):
            member_of = set(await _openfga_read_user_groups(user.id))
            forbidden = [g for g in requested if g not in member_of]
            if forbidden:
                log.info(
                    "authz_rejected", capability="layer:share",
                    reason="group_not_member", user_id=str(user.id),
                    layer=layer_id, groups=forbidden,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"not authorized to share to group(s): {', '.join(sorted(forbidden))}",
                )

    # Open Data public invariant (Talus 33 §F8): the public catalog enumerates `group:open_data`, so
    # an item shared into it MUST be public — a request adding it must ALSO turn Public Access ON.
    # Bound at the generic write so it can't be bypassed; the curate endpoint is the metadata-gated
    # front door. (Home is NOT coupled — a featured item may be org-only or public; see authz.py.)
    coupled = sorted(PUBLIC_SYSTEM_GROUPS & set(requested))
    if coupled and not payload.publicAccess:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"sharing to {', '.join(coupled)} requires public access",
        )

    # Open Data metadata gate (Talus 33 §F8): the same `metadata_complete` check the curate endpoint
    # runs, bound HERE too so the generic share path can't slip an undocumented layer into the public
    # catalog. Enforced whenever `open_data` is in the requested set (not just on first add) so the
    # invariant holds on every write; removing it from the group is never gated.
    if OPEN_DATA_GROUP in requested:
        complete, missing = await metadata_complete(conn, "layer", layer_id)
        if not complete:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "metadata_incomplete", "missing": missing},
            )

    # Turning Public Access ON is gated (F6 switch + org policy for non-admins); turning it OFF
    # only reduces exposure, so it's always allowed for this owner/admin. The org/group write and
    # the public write are INDEPENDENT (F5): set_object_shares never touches public_shared.
    if payload.publicAccess:
        await assert_may_share_public(conn, claims)
    await set_object_shares(
        f"layer:{layer_id}", organization_only=payload.organizationOnly, groups=requested
    )
    public_changed = await set_object_public(f"layer:{layer_id}", public=payload.publicAccess)
    log.info(
        "layer_shares_set", layer=layer_id, actor=str(user.id),
        organization_only=payload.organizationOnly, groups=requested, public=payload.publicAccess,
    )
    if public_changed:
        # A distinct, loud audit line for the irreversible-exposure transition (decision §F3).
        log.info(
            "object_public_changed", object=f"layer:{layer_id}", actor=str(user.id),
            public=payload.publicAccess,
        )
    await admin_audit.record(
        conn, actor_id=user.id, action="layer_shares_set", target_type="layer", target_id=layer_id,
        detail={"organizationOnly": payload.organizationOnly, "groups": requested,
                "public": payload.publicAccess, "publicChanged": public_changed},
    )
    return await _layer_shares_response(conn, layer_id)


# --- delete protection (Talus 24) -----------------------------------------------
#
# The layer analog of the web-map delete-protection flag: an owner-settable guard-rail that blocks
# release_layer for a non-admin owner (admins always override). Read/write is owner-OR-admin
# (manage_allowed) — so an ownerless system layer is admin-only — surfaced in the catalog "Manage"
# dialog. release_layer (publisher.py) is where the flag is enforced on delete.


class LayerDeleteProtection(BaseModel):
    deleteProtected: bool = False


@router.get(
    "/layers/{layer_id}/delete-protection",
    summary="A layer's delete-protection flag (owner or admin)",
)
async def get_layer_delete_protection(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerDeleteProtection:
    await _require_registered(conn, layer_id)
    if not await manage_allowed(claims, user, layer_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to manage this layer",
        )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT delete_protected FROM platform.layers WHERE layer_id = %s", (layer_id,)
        )
        row = await cur.fetchone()
    return LayerDeleteProtection(deleteProtected=bool(row[0]))


@router.put(
    "/layers/{layer_id}/delete-protection",
    summary="Set a layer's delete-protection flag (owner or admin)",
)
async def set_layer_delete_protection(
    layer_id: str,
    payload: LayerDeleteProtection,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerDeleteProtection:
    await _require_registered(conn, layer_id)
    if not await manage_allowed(claims, user, layer_id):
        log.info(
            "authz_rejected", capability="layer:delete_protection",
            reason="not_owner_not_admin", user_id=str(user.id), layer=layer_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to manage this layer",
        )
    # Metadata flip only — does NOT touch updated_at (consistent with the web-map analog; toggling
    # protection isn't an edit to the layer's data).
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.layers SET delete_protected = %s WHERE layer_id = %s",
            (payload.deleteProtected, layer_id),
        )
    await conn.commit()
    log.info(
        "layer_delete_protection_set", layer=layer_id, actor=str(user.id),
        protected=payload.deleteProtected,
    )
    await admin_audit.record(
        conn, actor_id=user.id, action="layer_delete_protection_set",
        target_type="layer", target_id=layer_id, detail={"protected": payload.deleteProtected},
    )
    return LayerDeleteProtection(deleteProtected=payload.deleteProtected)


# --- editing toggle (decision-editing-toggle.md) --------------------------------
#
# The owner-controlled SOFT read-only switch, sibling to delete-protection in Manage → Settings. Read/write is
# owner-OR-admin (manage_allowed), exactly like delete-protection. The flag is enforced in authz.edit_allowed
# (after the hard `editable` gate): when off, every write path 403s for a non-admin (including the owner — the
# owner flips it back on to edit), while a talus-admin still edits via the admin override + the existing
# "restricted but you have admin privileges" notice. Unlike delete-protection (a HARD lock even admins obey),
# editing-off is intentionally admin-overridable (decision-editing-toggle.md F2).


class LayerEditing(BaseModel):
    editingEnabled: bool = True
    # Omitted on a PUT = leave the flags as they are (the master switch alone, the pre-V109 body shape).
    editOps: EditOps | None = None


async def _layer_editing(conn: AsyncConnection, layer_id: str) -> LayerEditing:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT editing_enabled, edit_add, edit_attributes, edit_geometry, edit_delete "
            "FROM platform.layers WHERE layer_id = %s", (layer_id,)
        )
        row = await cur.fetchone()
    return LayerEditing(
        editingEnabled=bool(row[0]),
        editOps=EditOps.from_row(row[1], row[2], row[3], row[4]),
    )


@router.get(
    "/layers/{layer_id}/editing",
    summary="A layer's editing-enabled flag (owner or admin)",
)
async def get_layer_editing(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerEditing:
    await _require_registered(conn, layer_id)
    if not await manage_allowed(claims, user, layer_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to manage this layer",
        )
    return await _layer_editing(conn, layer_id)


@router.put(
    "/layers/{layer_id}/editing",
    summary="Set a layer's editing-enabled flag (owner or admin)",
)
async def set_layer_editing(
    layer_id: str,
    payload: LayerEditing,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerEditing:
    await _require_registered(conn, layer_id)
    if not await manage_allowed(claims, user, layer_id):
        log.info(
            "authz_rejected", capability="layer:editing",
            reason="not_owner_not_admin", user_id=str(user.id), layer=layer_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to manage this layer",
        )
    # Metadata flip only — does NOT touch updated_at (consistent with the delete-protection analog;
    # toggling the editing switch isn't an edit to the layer's data). The four capability flags ride the
    # same door (decision-layer-edit-capabilities.md §10 Q2): one owner-or-admin gate, one settings screen.
    try:
        async with conn.cursor() as cur:
            if payload.editOps is None:
                await cur.execute(
                    "UPDATE platform.layers SET editing_enabled = %s WHERE layer_id = %s",
                    (payload.editingEnabled, layer_id),
                )
            else:
                ops = payload.editOps
                await cur.execute(
                    "UPDATE platform.layers SET editing_enabled = %s, edit_add = %s, edit_attributes = %s, "
                    "edit_geometry = %s, edit_delete = %s WHERE layer_id = %s",
                    (payload.editingEnabled, ops.add, ops.attributes, ops.geometry, ops.delete, layer_id),
                )
        await conn.commit()
    except pg_errors.CheckViolation as exc:
        # The SQL-source pull's structural lock (decision-sql-source-pull.md §11 S3-D, V113 trigger): a pulled copy
        # stays read-only while its source is attached — the database refuses the flip, this door says why.
        await conn.rollback()
        if (getattr(getattr(exc, "diag", None), "message_hint", "") or "") == "sql_pull_locked":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="this table is kept up to date from a database; detach the pull under Catalog → Databases to edit it",
            )
        raise
    log.info(
        "layer_editing_set", layer=layer_id, actor=str(user.id),
        editing_enabled=payload.editingEnabled,
        edit_ops=(payload.editOps.model_dump() if payload.editOps is not None else None),
    )
    return await _layer_editing(conn, layer_id)


# --- attachments toggle (decision-attachments.md A-1) ---------------------------
#
# The owner-controlled per-layer "allow attachments" switch, sibling to editing + delete-protection in
# Manage → Settings. Read/write is owner-OR-admin (manage_allowed), exactly like editing. DEFAULT false
# (opt-in, V068) — attachments are off until the owner turns them on, which keeps `hasAttachments` honest
# and the upload surface closed by default. Enforced HARD in routers/attachments.py's write gate (NOT
# admin-overridable — an admin turns it on HERE, they don't bypass it): with it off, every attachment
# upload/delete 403s, including for the owner and admin.


class LayerAttachments(BaseModel):
    attachmentsEnabled: bool = False


@router.get(
    "/layers/{layer_id}/attachments-enabled",
    summary="A layer's attachments-enabled flag (owner or admin)",
)
async def get_layer_attachments_enabled(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerAttachments:
    await _require_registered(conn, layer_id)
    if not await manage_allowed(claims, user, layer_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to manage this layer",
        )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT attachments_enabled FROM platform.layers WHERE layer_id = %s", (layer_id,)
        )
        row = await cur.fetchone()
    return LayerAttachments(attachmentsEnabled=bool(row[0]))


@router.put(
    "/layers/{layer_id}/attachments-enabled",
    summary="Set a layer's attachments-enabled flag (owner or admin)",
)
async def set_layer_attachments_enabled(
    layer_id: str,
    payload: LayerAttachments,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerAttachments:
    await _require_registered(conn, layer_id)
    if not await manage_allowed(claims, user, layer_id):
        log.info(
            "authz_rejected", capability="layer:attachments_enabled",
            reason="not_owner_not_admin", user_id=str(user.id), layer=layer_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to manage this layer",
        )
    # Metadata flip only — does NOT touch updated_at (consistent with the editing/delete-protection analogs).
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.layers SET attachments_enabled = %s WHERE layer_id = %s",
            (payload.attachmentsEnabled, layer_id),
        )
    await conn.commit()
    log.info(
        "layer_attachments_enabled_set", layer=layer_id, actor=str(user.id),
        attachments_enabled=payload.attachmentsEnabled,
    )
    return LayerAttachments(attachmentsEnabled=payload.attachmentsEnabled)


# --- display field (decision-layer-display-field.md) ----------------------------
#
# The layer's "which attribute names a feature" setting — the ArcGIS Display Field. RAW column
# platform.layers.display_field (V072): the attribute whose VALUE titles a feature everywhere it shows as one
# line (popup title, Locate/feature-search labels, identify, the ESRI/OGC displayField). Set by starring a
# column in the attribute table; a NULL means "unset" (the read path derives a first-text-field default —
# /me/layers.displayField carries the EFFECTIVE value, this endpoint the RAW one so the star reflects only an
# explicit choice). Write is MANAGE (owner/admin, fork F4) — moving the name field alters the layer for
# everyone in every map, a config act; a viewer/editor sees the filled star (GET is open, returns canManage)
# but can't move it (PUT 403s). The starred name is validated to a REAL field of the layer at set time (fork
# F6), mirroring the geocoder address-field check.


class LayerDisplayField(BaseModel):
    """A layer's RAW display field (None = unset → the read path derives a default) + whether THIS caller may
    change it (drives the star affordance; the server is the authority)."""

    displayField: str | None = None
    canManage: bool = False


class LayerDisplayFieldUpdate(BaseModel):
    displayField: str | None = None  # a real field of the layer, or null to clear the star


async def _is_real_layer_field(conn: AsyncConnection, layer_id: str, field: str) -> bool:
    """True iff `field` is a settable display field of this layer (fork F6): any real attribute-table column
    of assets.<layer_id> (incl. `notes`; `geom` excluded — it's geometry, not a label), OR an ACTIVE
    owner-defined slot field (value in props jsonb). layer_id is a validated registry row here; it names the
    table only as a bound parameter in information_schema (never interpolated)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'assets' AND table_name = %s AND column_name = %s "
            "  AND column_name <> 'geom'",
            (layer_id, field),
        )
        if await cur.fetchone() is not None:
            return True
        await cur.execute(
            "SELECT 1 FROM platform.layer_fields WHERE layer_id = %s AND field_key = %s AND active",
            (layer_id, field),
        )
        return await cur.fetchone() is not None


@router.get(
    "/layers/{layer_id}/display-field",
    summary="A layer's RAW display field + whether this caller may change it",
)
async def get_layer_display_field(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerDisplayField:
    # Open to any authenticated caller (like public-share): a viewer/editor SEES which column is starred and
    # that they can't move it (canManage=false). No manage gate on the read — the field name isn't sensitive
    # (the /columns schema already exposes every field name to viewers).
    await _require_registered(conn, layer_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT display_field FROM platform.layers WHERE layer_id = %s", (layer_id,)
        )
        row = await cur.fetchone()
    return LayerDisplayField(
        displayField=row[0],
        canManage=await manage_allowed(claims, user, layer_id),
    )


@router.put(
    "/layers/{layer_id}/display-field",
    summary="Set or clear a layer's display field (owner or admin)",
)
async def set_layer_display_field(
    layer_id: str,
    payload: LayerDisplayFieldUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerDisplayField:
    await _require_registered(conn, layer_id)
    if not await manage_allowed(claims, user, layer_id):
        log.info(
            "authz_rejected", capability="layer:display_field",
            reason="not_owner_not_admin", user_id=str(user.id), layer=layer_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to manage this layer",
        )
    field = payload.displayField
    if field is not None and not await _is_real_layer_field(conn, layer_id, field):
        # Mirrors the geocoder address-field validation: a bad field is a 400, not a silent no-op.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{field}' is not a field of this layer",
        )
    # Metadata flip only — does NOT touch updated_at (consistent with the editing/delete-protection analogs;
    # relabeling isn't an edit to the layer's data).
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.layers SET display_field = %s WHERE layer_id = %s",
            (field, layer_id),
        )
    await conn.commit()
    log.info("layer_display_field_set", layer=layer_id, actor=str(user.id), display_field=field)
    return LayerDisplayField(displayField=field, canManage=True)
