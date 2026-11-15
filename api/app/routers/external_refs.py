"""External catalog items (decision-external-catalog-items.md).

A referenced external service — the shipped add-from-url `ExternalLayerSpec` — promoted to a
shareable CATALOG ITEM: a GIS lead references the county/state service ONCE, shares it via the
existing org/group/public model, and every map author finds it in Browse catalog. One
`platform.external_refs` row (V060) + one OpenFGA object per item.

Load-bearing choices (decision §3):

  - **Read-only pointers.** No slot, no assets table, no editing, no proxy — the item stores a URL
    the VIEWER'S browser fetches directly. This server never fetches it: creation is probe-validated
    in the BROWSER form (`probeExternalUrl`), shape-validated HERE (the OperationalLayer external
    arm's bounds, mirrored).
  - **OpenFGA object = `layer:<xref_id>`** (fork §3.4). `xref_` ids are prefix-disjoint from every
    layer id, and `_openfga_check` defaults to object_type "layer" — so `view_allowed` / `is_owner`
    / `manage_allowed`, the share seam (`get/set_object_*`), favorites, and transfer all apply
    verbatim. No model reload.
  - **Hybrid resolution** (fork §3.1): a map op stores `externalItemId` + a spec SNAPSHOT. The map
    resolves the item via GET /external-refs/{id} when readable (central URL fixes propagate);
    deleted/unshared → 404 (indistinguishable BY DESIGN — the anon-deny posture) → the snapshot
    renders. Maps never break, so DELETE is plain (fork §3.3) — no usage scan of map jsonb.
  - **Publisher-gated creation** (fork §3.2), consistent with every other item-creating action.
  - Share endpoints mirror `layer_shares.set_layer_shares` one-for-one (the same one-for-one
    mirroring layer_shares itself did from web_maps) — incl. member-scoping, the public gate, and
    the Open-Data coupling/metadata gate (an undocumented ref can't slip into the public catalog;
    T31 metadata attaches to items LATER, fork §3.5).
"""
from typing import Any, Literal
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from pydantic import BaseModel, Field, field_validator, model_validator

from .. import admin_audit
from ..auth import _validated_claims, get_current_user
from ..authz import (
    OPEN_DATA_GROUP,
    PUBLIC_SYSTEM_GROUPS,
    _has_admin_role,
    _has_publish_role,
    _openfga_delete,
    _openfga_read_object_tuples,
    _openfga_read_user_groups,
    _openfga_write,
    assert_may_share_public,
    get_object_shares_and_owner,
    is_shared,
    manage_allowed,
    set_object_public,
    set_object_shares,
    view_allowed,
)
from ..db import get_db
from ..models.user import User
from .layer_shares import LayerShares, LayerShareUpdate
from .me import _favorited_item_ids, _resolve_user_names
from .metadata import metadata_complete
# ShareGroup + _clean_label are the canonical share model / label posture first defined for web
# maps; refs reuse them verbatim so the wire shape matches every other catalog item type.
from .web_maps import ShareGroup, _clean_label

log = structlog.get_logger(__name__)

router = APIRouter(tags=["external-refs"])

_ExternalKind = Literal["arcgis-feature", "geojson", "xyz", "arcgis-image"]
_GeometryType = Literal["point", "line", "polygon"]

# How many fresh-uuid attempts before conceding the insert (collisions in 16^8 are cosmic-ray
# territory; the loop is correctness, not expectation).
_ID_ATTEMPTS = 4


def _new_xref_id() -> str:
    return "xref_" + uuid4().hex[:8]


class _RefFields(BaseModel):
    """The mutable display + spec fields, bounds mirroring the OperationalLayer external arm
    (web_maps.py) so an item and a map-scoped external validate identically. url/where are never
    interpolated into HTML or SQL — they only feed the viewer's browser fetch/MapLibre against the
    REMOTE server."""

    title: str
    description: str | None = Field(default=None, max_length=2000)
    url: str = Field(max_length=2000)
    # Vector kinds: the drawn geometry family (from the browser-side probe) + display color.
    geometryType: _GeometryType | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    # arcgis-feature only: a where-clause carried from a pasted /query URL — sent as a bound query
    # param to the remote ArcGIS server. An item MAY be a curated filtered reference (fork §3.8).
    where: str | None = Field(default=None, max_length=2000)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        return _clean_label(v, 80, "reference title")

    @model_validator(mode="after")
    def _check_url(self) -> "_RefFields":
        if not (self.url.startswith("https://") or self.url.startswith("http://")):
            raise ValueError("a reference requires an http(s) url")
        return self


class ExternalRefCreate(_RefFields):
    externalKind: _ExternalKind


class ExternalRefUpdate(_RefFields):
    """PUT payload. `externalKind` is deliberately absent — the kind is immutable (a different
    protocol is a different reference); url/where ARE editable (central fixes propagating to every
    referencing map is the point of the hybrid model)."""


class ExternalRef(BaseModel):
    """One item, full spec — what the map resolution path (GET /external-refs/{id}) and the Browse
    picker seed an external op from. The spec is not secret to a viewer: anyone who can view the
    item may add it to a map, where the browser fetches the url directly anyway."""

    id: str
    title: str
    description: str | None = None
    externalKind: _ExternalKind
    url: str
    geometryType: _GeometryType | None = None
    color: str | None = None
    where: str | None = None
    updated_at: Any = None


class MyExternalRef(ExternalRef):
    """A listing row: the full spec + the caller's capabilities — the external-item analog of
    LayerCapability, kept its own shape (fork §3.7) so /me/layers stays clean of inapplicable
    fields (featureCount, renderMode, editable…)."""

    owner: bool = False
    sharedWithMe: bool = False
    favorite: bool = False
    canManage: bool = False
    shareLevel: str | None = None
    ownerId: str | None = None
    ownerName: str | None = None


class MyExternalRefsResponse(BaseModel):
    refs: list[MyExternalRef]


_ROW_COLS = (
    "id, title, description, external_kind, url, geometry_type, color, where_clause, updated_at"
)


def _row_to_ref(row: tuple) -> ExternalRef:
    return ExternalRef(
        id=row[0], title=row[1], description=row[2], externalKind=row[3], url=row[4],
        geometryType=row[5], color=row[6], where=row[7], updated_at=row[8],
    )


async def _require_ref(conn: AsyncConnection, ref_id: str) -> tuple:
    """The item's row, else 404 (binds ref_id as a parameter)."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_ROW_COLS} FROM platform.external_refs WHERE id = %s", (ref_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="reference not found")
    return row


@router.post(
    "/external-refs",
    status_code=status.HTTP_201_CREATED,
    summary="Create a referenced external service as a catalog item (publisher)",
)
async def create_external_ref(
    payload: ExternalRefCreate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> ExternalRef:
    # Publisher-gated (fork §3.2) — the same role that gates every item-creating action. A
    # non-publisher keeps the map-scoped add-from-URL flow unchanged.
    if not _has_publish_role(claims):
        log.info(
            "authz_rejected", capability="external_refs:create",
            reason="missing_publish_role", user_id=str(user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to create references",
        )
    # App-generated, collision-checked id (the hosted-table id pattern). Row first, owner tuple
    # second (the create_layer ordering): a mid-failure leaves an OWNERLESS row — admin-manageable
    # and deletable, never an orphaned grant.
    async with conn.cursor() as cur:
        for _ in range(_ID_ATTEMPTS):
            ref_id = _new_xref_id()
            await cur.execute("SELECT 1 FROM platform.external_refs WHERE id = %s", (ref_id,))
            if await cur.fetchone() is None:
                break
        else:  # pragma: no cover — 4 straight uuid4 collisions
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="could not allocate an id"
            )
        await cur.execute(
            "INSERT INTO platform.external_refs "
            "(id, owner_id, title, description, external_kind, url, geometry_type, color, where_clause) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                ref_id, user.id, payload.title, payload.description, payload.externalKind,
                payload.url, payload.geometryType, payload.color, payload.where,
            ),
        )
    await conn.commit()
    await _openfga_write([(f"user:{user.id}", "owner", f"layer:{ref_id}")])
    log.info(
        "external_ref_created", ref=ref_id, actor=str(user.id),
        kind=payload.externalKind, url=payload.url,
    )
    return ExternalRef(
        id=ref_id, title=payload.title, description=payload.description,
        externalKind=payload.externalKind, url=payload.url,
        geometryType=payload.geometryType, color=payload.color, where=payload.where,
    )


@router.get(
    "/me/external-refs",
    response_model=MyExternalRefsResponse,
    summary="References the caller may view, with per-item capabilities",
)
async def my_external_refs(
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> MyExternalRefsResponse:
    """The /me/layers analog for references — feeds the Catalog "References" rows AND the Browse
    picker group (a picker add seeds the op's snapshot straight from the listed spec, no second
    fetch). Admin sees all (god-mode) with sharedWithMe:false, mirroring my_layers exactly."""
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT {_ROW_COLS} FROM platform.external_refs ORDER BY title")
        rows = await cur.fetchall()
    favorited = await _favorited_item_ids(conn, user.id, "external")
    out: list[MyExternalRef] = []
    for row in rows:
        ref_id = row[0]
        genuine = await is_shared(user, ref_id)  # genuine can_view, no admin override
        if not genuine and not user.is_admin:
            continue  # absent, not canView:false
        # ONE tuple read per item: share badge + owner attribution (the my_layers pattern); the
        # tuple owner also answers is_owner without a second FGA round-trip.
        try:
            shares, owner_id = await get_object_shares_and_owner(f"layer:{ref_id}")
            if shares["publicAccess"]:
                share_level: str | None = "public"
            elif shares["organizationOnly"]:
                share_level = "organization"
            elif shares["groups"]:
                share_level = "groups"
            else:
                share_level = "owner"
        except HTTPException:
            shares, owner_id, share_level = None, None, None  # badge/owner just don't render
        owned = owner_id is not None and owner_id == str(user.id)
        out.append(
            MyExternalRef(
                **_row_to_ref(row).model_dump(),
                owner=owned,
                sharedWithMe=genuine and not owned,
                favorite=ref_id in favorited,
                canManage=_has_admin_role(claims) or owned,
                shareLevel=share_level,
                ownerId=owner_id,
            )
        )
    owner_names = await _resolve_user_names(conn, {r.ownerId for r in out if r.ownerId})
    for r in out:
        if r.ownerId:
            r.ownerName = owner_names.get(r.ownerId)
    return MyExternalRefsResponse(refs=out)


@router.get(
    "/external-refs/{ref_id}",
    summary="Resolve one reference to its current spec (view-gated; 404-on-deny)",
)
async def get_external_ref(
    ref_id: str,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExternalRef:
    # THE map-resolution endpoint (decision §2.2): a readable item's CURRENT spec wins over the
    # op's snapshot, so a central url/where fix propagates to every referencing map on open.
    # Denied == absent == 404 (the anon-deny posture) — deleted and unshared are deliberately
    # indistinguishable; either way the map falls back to its snapshot and never breaks.
    row = await _require_ref(conn, ref_id)
    if not await view_allowed(user, ref_id):
        log.info(
            "authz_rejected", capability="external_refs:view", reason="no_view_access",
            user_id=str(user.id), ref=ref_id,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="reference not found")
    return _row_to_ref(row)


@router.put(
    "/external-refs/{ref_id}",
    summary="Update a reference's display fields / url / where (owner or admin)",
)
async def update_external_ref(
    ref_id: str,
    payload: ExternalRefUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> ExternalRef:
    await _require_ref(conn, ref_id)
    if not await manage_allowed(claims, user, ref_id):
        log.info(
            "authz_rejected", capability="external_refs:update",
            reason="not_owner_not_admin", user_id=str(user.id), ref=ref_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to manage this reference",
        )
    # geometryType rides along with a url change (a repointed service may draw differently; the
    # browser form re-probes before submitting). external_kind stays immutable.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.external_refs SET title = %s, description = %s, url = %s, "
            "geometry_type = %s, color = %s, where_clause = %s, updated_at = now() "
            "WHERE id = %s",
            (
                payload.title, payload.description, payload.url,
                payload.geometryType, payload.color, payload.where, ref_id,
            ),
        )
    await conn.commit()
    log.info("external_ref_updated", ref=ref_id, actor=str(user.id), url=payload.url)
    return _row_to_ref(await _require_ref(conn, ref_id))


@router.delete(
    "/external-refs/{ref_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a reference (owner or admin; referencing maps heal via their snapshot)",
)
async def delete_external_ref(
    ref_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> None:
    # PLAIN delete (fork §3.3): no usage scan — every referencing op carries a spec snapshot and
    # renders from it once resolution 404s, so deletion is harmless to maps (better than AGO's
    # break-on-delete). Delete-protection is a layer/map affordance; refs don't carry it (v1).
    await _require_ref(conn, ref_id)
    if not await manage_allowed(claims, user, ref_id):
        log.info(
            "authz_rejected", capability="external_refs:delete",
            reason="not_owner_not_admin", user_id=str(user.id), ref=ref_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to delete this reference",
        )
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM platform.external_refs WHERE id = %s", (ref_id,))
        await cur.execute(
            "DELETE FROM platform.user_favorites WHERE item_type = 'external' AND item_id = %s",
            (ref_id,),
        )
    await conn.commit()
    # ALL tuples on the object (owner AND org/group/public shares) — the release_layer §Q4 rule.
    tuples = await _openfga_read_object_tuples(f"layer:{ref_id}")
    await _openfga_delete(tuples)
    log.info(
        "external_ref_deleted", ref=ref_id, actor=str(user.id), tuples_deleted=len(tuples)
    )


# --- shares: org / group / public ------------------------------------------------
#
# Mirrors layer_shares.get/set_layer_shares one-for-one over the ref's `layer:<xref_id>` object —
# the same mirroring layer_shares itself did from web_maps, so the wire shape and every gate
# (member-scoping, the public F6 switch + org policy, the Open-Data public coupling + metadata
# gate) are identical across catalog item types. NO authz.py change; NO OpenFGA model reload.


async def _ref_shares_response(conn: AsyncConnection, ref_id: str) -> LayerShares:
    shares, _owner = await get_object_shares_and_owner(f"layer:{ref_id}")
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
    "/external-refs/{ref_id}/shares",
    summary="A reference's full share state — org/group/public (owner or admin)",
)
async def get_external_ref_shares(
    ref_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerShares:
    await _require_ref(conn, ref_id)
    if not await manage_allowed(claims, user, ref_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to manage this reference",
        )
    return await _ref_shares_response(conn, ref_id)


@router.put(
    "/external-refs/{ref_id}/shares",
    summary="Set/replace a reference's share state — org/group/public (owner or admin)",
)
async def set_external_ref_shares(
    ref_id: str,
    payload: LayerShareUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> LayerShares:
    await _require_ref(conn, ref_id)
    if not await manage_allowed(claims, user, ref_id):
        log.info(
            "authz_rejected", capability="external_refs:share",
            reason="not_owner_not_admin", user_id=str(user.id), ref=ref_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to change sharing for this reference",
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
        # Member-scoping (the real gate; the picker only scopes the UI). Fails closed (503).
        if not _has_admin_role(claims):
            member_of = set(await _openfga_read_user_groups(user.id))
            forbidden = [g for g in requested if g not in member_of]
            if forbidden:
                log.info(
                    "authz_rejected", capability="external_refs:share",
                    reason="group_not_member", user_id=str(user.id),
                    ref=ref_id, groups=forbidden,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"not authorized to share to group(s): {', '.join(sorted(forbidden))}",
                )

    # Open Data public invariant (Talus 33 §F8) — bound at every generic share write.
    coupled = sorted(PUBLIC_SYSTEM_GROUPS & set(requested))
    if coupled and not payload.publicAccess:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"sharing to {', '.join(coupled)} requires public access",
        )
    # Open Data metadata gate (Talus 33 §F8). References have no metadata yet (T31 attaches to
    # items LATER, fork §3.5) — so this correctly refuses an undocumented ref into the public
    # catalog until that slice lands. Removing it from the group is never gated.
    if OPEN_DATA_GROUP in requested:
        complete, missing = await metadata_complete(conn, "layer", ref_id)
        if not complete:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "metadata_incomplete", "missing": missing},
            )

    if payload.publicAccess:
        await assert_may_share_public(conn, claims)
    await set_object_shares(
        f"layer:{ref_id}", organization_only=payload.organizationOnly, groups=requested
    )
    public_changed = await set_object_public(f"layer:{ref_id}", public=payload.publicAccess)
    log.info(
        "external_ref_shares_set", ref=ref_id, actor=str(user.id),
        organization_only=payload.organizationOnly, groups=requested, public=payload.publicAccess,
    )
    if public_changed:
        log.info(
            "object_public_changed", object=f"layer:{ref_id}", actor=str(user.id),
            public=payload.publicAccess,
        )
    await admin_audit.record(
        conn, actor_id=user.id, action="external_ref_shares_set", target_type="external_ref",
        target_id=ref_id,
        detail={"organizationOnly": payload.organizationOnly, "groups": requested,
                "public": payload.publicAccess, "publicChanged": public_changed},
    )
    return await _ref_shares_response(conn, ref_id)
