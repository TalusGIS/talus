"""Current-user authorization view.

GET /me/layers tells the frontend which layers the caller may see and edit. It is the
seam the map fills canView/canEdit/editable from (replacing the static 7a fill). It
computes the three keys with the SAME helpers require_capability uses (app/authz), so
the UI never offers an edit the api then refuses.

Only layers the caller can VIEW are returned (matches AGO: you don't see items not
shared to you). An inaccessible layer is absent, not present-with-canView:false.
"""
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection, sql
from pydantic import BaseModel, Field

from ..auth import _validated_claims, get_current_user
from ..display_field import derive_display_fields
from ..authz import (
    _layer_editable,
    _openfga_check,
    edit_allowed,
    get_object_shares_and_owner,
    is_owner,
    is_shared,
    manage_allowed,
    view_allowed,
)
from ..db import get_db
from .. import keycloak_admin
from ..models.edit_ops import EditOps
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["me"])


class LayerCapability(BaseModel):
    id: str
    canView: bool
    canEdit: bool
    # editViaAdmin: canEdit holds ONLY because the caller is a talus-admin (no genuine edit access). The map
    # editing form shows a "editing is restricted but you have admin privileges" notice when true. False for a
    # caller with genuine edit access (owner / shared-for-edit) — no notice then. (admin-edit-all, 2026-06-30.)
    editViaAdmin: bool = False
    # editOps (V109, decision-layer-edit-capabilities.md §3.4): the owner's four capability flags — the same
    # set for everyone with edit access. The web editor hides Draw / Move-or-reshape / Delete / Import where a
    # flag is off; the doors decide (a flag can only refuse). Independent of canEdit: a layer the caller cannot
    # edit still reports the flags (they describe the layer, not the caller).
    editOps: EditOps = EditOps()
    # canManage (Talus 13): may set/clear the layer-default symbology — owner or admin.
    # Distinct from canEdit (feature edits); gates the Styles panel affordance.
    canManage: bool
    # owner (Talus 18): does the caller hold the OpenFGA `owner` relation on this layer —
    # the exact "Owned by me" signal. Distinct from canManage (owner OR admin).
    owner: bool
    # sharedWithMe: viewable via a GENUINE share (group/org/public/direct) and NOT owned — the catalog/
    # add-layers "Shared with me" bucket. For a regular user every returned layer is viewable, so this == !owner.
    # For an ADMIN (who now sees ALL layers) it is true ONLY for real shares; layers an admin sees solely via
    # admin override are owner:false + sharedWithMe:false → they land in "All" only, never "Shared with me".
    sharedWithMe: bool = False
    # favorite (Talus 18): has the caller starred this layer? Per-caller, from the
    # platform.user_favorites LEFT JOIN (V023). Drives the ★ Favorites filter on the Catalog.
    favorite: bool
    # shareLevel (Talus 24): the layer's current sharing level for the catalog share-status badge —
    # 'public' | 'organization' | 'groups' | 'owner' (owner = private). None on an OpenFGA hiccup
    # (the badge simply doesn't render). Mirrors the web-map badge for catalog parity.
    shareLevel: str | None = None
    # updated_at (Talus 24): the layer's last-modified timestamp (platform.layers.updated_at) —
    # surfaced for the catalog's "modified date" column + the Modified sort (parity with maps,
    # whose WebMapSummary already carries updated_at). ISO-8601 over the wire.
    updated_at: Any = None
    # deleteProtected (Talus 24 c4): the layer's delete-protection flag, surfaced so the catalog row
    # can show the at-a-glance padlock on the delete affordance (parity with maps). The dedicated
    # GET/PUT /layers/{id}/delete-protection is still the read/write surface for the Manage dialog.
    deleteProtected: bool = False
    # owner attribution (Talus 26): the layer owner's id + display name (the OpenFGA `owner` tuple's
    # subject, resolved against platform.users). None when no owner tuple resolves (e.g. an
    # unclaimed slot). Drives the catalog "owner" column + the "ask the owner" permission notice.
    # Mirrors maps.
    ownerId: str | None = None
    ownerName: str | None = None
    # featureCount (Talus 36): live row count of the layer's backing assets.<id> table, for the
    # catalog's zero-features health tell (count == 0 -> orange layer icon). None when the layer has
    # no backing table (unclaimed slot / not yet provisioned) -> no color, which also reserves the
    # deferred broken->red state. Drives a COLOR only; the number itself is not shown (drift guard).
    featureCount: int | None = None
    editable: bool
    displayName: str | None = None
    displayColor: str | None = None
    # The layer-default symbology spec (V022). NULL means "no stored renderer — fall back
    # to single-symbol from displayColor, then the frontend LayerDef constant". The client
    # resolves paint AND the read-only legend from this one field; the future per-user
    # saved-map override stores the SAME shape, resolved ahead of it.
    renderer: dict | None = None
    # renderMode (Talus 38): how the SPA fetches this layer's geometry — 'tiles' (MVT, the default
    # for every pre-V038 layer, unchanged) or 'features' (a view layer, rendered as a GeoJSON source
    # off /layers/{id}/features). Derived purely from platform.layer_views membership.
    renderMode: str = "tiles"
    # View lineage (Talus 38, F-itemtype): for a view layer, the source it filters + the source's
    # display name, for the catalog "view of <source>" indicator. BOTH are None unless the caller can
    # also VIEW the source — never leak a private source's identity to someone who only holds the view.
    sourceLayerId: str | None = None
    sourceName: str | None = None
    # Native-CRS storage (decision-crs-native-slice1.md): nativeStorage layers physically store geometry in
    # workingSrid (byte-exact). For the catalog/Manage "native storage" indicator. Default layers: false / 4326.
    nativeStorage: bool = False
    workingSrid: int = 4326
    # Geometry family (decision-line-polygon-layers.md, V055): 'point' | 'line' | 'polygon'. The friendly enum
    # (storage may be Multi* for line/polygon) the map branches on to add a circle / line / fill+outline layer,
    # and the wizard/catalog show. Default 'point' (every pre-V055 layer and the slots).
    geomType: str = "point"
    # displayField (decision-layer-display-field.md, V072): the layer's EFFECTIVE label field — the attribute
    # that names a feature in the popup title + Locate/feature-search result labels. The RAW display_field when
    # set, else a read-time first-text-field derivation (fork F2) so a brand-new layer reads well with zero
    # config (never `id`). None only when a layer has literally no text field to fall back to. Distinct from the
    # RAW value the attribute-table star reflects (GET /layers/{id}/display-field) — that shows a filled star
    # ONLY on an explicit choice, so it must NOT surface the derived default.
    displayField: str | None = None
    # attachmentsEnabled (V068 toggle, surfaced here for decision-attachments-in-edit-form.md): whether the
    # layer accepts feature attachments. Gates the edit form's attachment-staging control (upload lives in the
    # edit interface now, not the read drawer). Default False (every attachments-off layer / view).
    attachmentsEnabled: bool = False
    # formConfig (V104, decision-talus-field-f2.md): the layer's declarative form document — groups, visibleIf /
    # requiredIf, readOnly, templates, photo. None = the default form. Carried here so the desk form and the
    # field app get it in the read they already make; GET/PUT /layers/{id}/form is the authoring surface.
    formConfig: dict | None = None


class MyLayersResponse(BaseModel):
    layers: list[LayerCapability]


async def _registered_layers(
    conn: AsyncConnection,
) -> list[tuple[str, str | None, str | None, dict | None, Any, bool, bool, int, str, str | None, bool, dict | None, EditOps]]:
    """(layer_id, display_name, display_color, renderer, updated_at, delete_protected, native_storage,
    working_srid, geom_type, display_field, attachments_enabled, form_config, edit_ops) from the
    platform.layers registry — the set the api authorizes over. display_name/display_color are NULL
    for unclaimed slots; renderer is NULL until a layer default has been set; updated_at backs the catalog
    modified date + Modified sort, delete_protected backs the row's delete padlock (Talus 24);
    geom_type (V055) is the layer's geometry family; display_field (V072) is the layer's RAW label
    field (NULL = unset, derived at read time below). psycopg returns jsonb pre-parsed."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT layer_id, display_name, display_color, renderer, updated_at, delete_protected, "
            "native_storage, working_srid, geom_type, display_field, attachments_enabled, form_config, "
            "edit_add, edit_attributes, edit_geometry, edit_delete "
            "FROM platform.layers ORDER BY layer_id"
        )
        return [
            (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11],
             EditOps.from_row(r[12], r[13], r[14], r[15]))
            for r in await cur.fetchall()
        ]


async def _favorited_item_ids(conn: AsyncConnection, user_id: Any, item_type: str) -> set[str]:
    """The set of item_ids of `item_type` the caller has starred (platform.user_favorites, now
    polymorphic — V029/Talus 24). One query for the whole list — the per-item `favorite` flag is a
    set membership test, not a round-trip each. A favorite to a now-unviewable item is simply never
    tested (it isn't in the returned set), so orphans stay harmless (decision-18 §3 Q5)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT item_id FROM platform.user_favorites WHERE user_id = %s AND item_type = %s",
            (user_id, item_type),
        )
        return {r[0] for r in await cur.fetchall()}


async def _layer_share_and_owner(layer_id: str) -> tuple[str | None, str | None]:
    """The layer's catalog share level (broadest tier wins: public ⊃ organization ⊃ groups ⊃ owner)
    AND its owner user-id — both from ONE OpenFGA object-tuple read (Talus 24 badge + Talus 26 owner
    attribution). (None, None) on an OpenFGA error — the badge/owner just don't render (never fails
    the list). Owner is None for ownerless system layers."""
    try:
        shares, owner = await get_object_shares_and_owner(f"layer:{layer_id}")
    except HTTPException:
        return None, None
    if shares["publicAccess"]:
        level = "public"
    elif shares["organizationOnly"]:
        level = "organization"
    elif shares["groups"]:
        level = "groups"
    else:
        level = "owner"
    return level, owner


async def _layer_feature_counts(conn: AsyncConnection, layer_ids: set[str]) -> dict[str, int]:
    """Live feature counts for the catalog's zero-features health color (Talus 36). One COUNT(*)
    per layer, batched into ONE UNION ALL over the assets.<layer_id> tables (one round-trip, not
    N). Table name == layer_id (matches layer_query._assets_table). A registered layer with NO
    backing table (unclaimed slot / not yet provisioned) is simply absent from the result — its
    row gets featureCount=None (no health color), which also reserves the deferred broken->red
    state. Counts ONLY the ids passed in (already the caller's viewable set), so no new read
    surface. Exact (not pg_class.reltuples) — the zero test must not false-flag on a stale estimate."""
    if not layer_ids:
        return {}
    async with conn.cursor() as cur:
        # Only count tables that actually exist — a missing one would error the whole UNION ALL.
        await cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'assets' AND table_name = ANY(%s)",
            (list(layer_ids),),
        )
        existing = [r[0] for r in await cur.fetchall()]
        if not existing:
            return {}
        # layer_id is a validated registry id proven to be a real assets table here; sql.Identifier
        # quotes it regardless. sql.Literal carries the id back as the grouping label.
        union = sql.SQL(" UNION ALL ").join(
            sql.SQL("SELECT {lid} AS lid, count(*) AS n FROM {tbl}").format(
                lid=sql.Literal(lid), tbl=sql.Identifier("assets", lid)
            )
            for lid in existing
        )
        await cur.execute(union)
        return {r[0]: r[1] for r in await cur.fetchall()}


async def _view_layer_sources(conn: AsyncConnection) -> dict[str, str]:
    """view_layer_id -> source_layer_id for every view layer (Talus 38). Drives renderMode='features'
    (the SPA renders a view as a GeoJSON source, not MVT tiles) + the catalog 'view of <source>'
    lineage. Empty before any view exists, so non-view layers are wholly unaffected."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT view_layer_id, source_layer_id FROM platform.layer_views")
        return {r[0]: r[1] for r in await cur.fetchall()}


async def _resolve_user_names(conn: AsyncConnection, ids: set[str]) -> dict[str, str]:
    """Batch-resolve user uuids -> a display name (COALESCE(display_name, username, email)) in ONE
    query — avoids an N+1 when naming per-row owners. Missing ids simply aren't in the result."""
    if not ids:
        return {}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, COALESCE(display_name, username, email) FROM platform.users "
            "WHERE id = ANY(%s)",
            (list(ids),),
        )
        return {str(r[0]): r[1] for r in await cur.fetchall()}


@router.get(
    "/me/layers",
    response_model=MyLayersResponse,
    summary="Layers the caller may view, with per-layer view/edit/editable flags",
)
async def my_layers(
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> MyLayersResponse:
    out: list[LayerCapability] = []
    favorited = await _favorited_item_ids(conn, user.id, "layer")
    registered = await _registered_layers(conn)
    view_sources = await _view_layer_sources(conn)  # view_layer_id -> source_layer_id (Talus 38)
    # An ADMIN sees EVERY layer (god-mode, via User.is_admin); a regular caller sees only what's genuinely
    # shared with / owned by them. `is_shared` is the genuine can_view (NO admin override) — both the gate's
    # share-signal and the sharedWithMe flag. An admin's non-shared layers pass the gate via user.is_admin with
    # sharedWithMe:false (→ "All" only). (Talus: admin-sees-all, 2026-06-30.)
    for layer_id, display_name, display_color, renderer, updated_at, delete_protected, native_storage, working_srid, geom_type, display_field, attachments_enabled, form_config, edit_ops in registered:
        genuine = await is_shared(user, layer_id)  # genuine can_view (owner or a real share), no admin override
        if not genuine and not user.is_admin:
            continue  # absent, not canView:false
        editable = bool(await _layer_editable(conn, layer_id))
        can_edit, edit_reason = await edit_allowed(conn, claims, user, layer_id)
        # Resolve owner ONCE and feed it to manage_allowed — canManage = owner OR admin, so
        # the precomputed owner avoids a second OpenFGA round-trip for non-admins.
        owner = await is_owner(user, layer_id)
        shared_with_me = genuine and not owner  # genuine share; false for admin-override-only layers
        can_manage = await manage_allowed(claims, user, layer_id, owner=owner)
        share_level, owner_id = await _layer_share_and_owner(layer_id)
        out.append(
            LayerCapability(
                id=layer_id,
                canView=True,
                canEdit=can_edit,
                editViaAdmin=(edit_reason == "admin"),
                editOps=edit_ops,
                canManage=can_manage,
                owner=owner,
                sharedWithMe=shared_with_me,
                favorite=layer_id in favorited,
                shareLevel=share_level,
                updated_at=updated_at,
                deleteProtected=bool(delete_protected),
                ownerId=owner_id,  # ownerName filled in after the loop (batched)
                editable=editable,
                displayName=display_name,
                displayColor=display_color,
                renderer=renderer,
                renderMode="features" if layer_id in view_sources else "tiles",
                nativeStorage=bool(native_storage),
                workingSrid=int(working_srid),
                geomType=geom_type or "point",
                displayField=display_field,  # RAW here; the derived fallback is stamped in below
                attachmentsEnabled=bool(attachments_enabled),
                formConfig=form_config,
            )
        )
    # Batch-resolve owner display names + feature counts in ONE query each (no N+1), then stamp.
    owner_names = await _resolve_user_names(conn, {lc.ownerId for lc in out if lc.ownerId})
    counts = await _layer_feature_counts(conn, {lc.id for lc in out})
    # displayField (V072): where the RAW column was NULL, derive a first-text-field default at read time so
    # the layer labels well with zero config (fork F2). Two batched queries, only for the unset layers; the
    # SAME derivation the ESRI door uses (app/display_field.py) so web + ESRI agree on an unstarred layer.
    derived_fields = await derive_display_fields(conn, {lc.id for lc in out if lc.displayField is None})
    # View lineage (Talus 38): expose "view of <source>" ONLY when the caller can also view the source
    # (every layer in `out` is viewable by this caller) — never leak a private source's name to a
    # holder of just the view. display_names maps a layer id -> its label for the lineage text.
    viewable_ids = {lc.id for lc in out}
    display_names = {r[0]: r[1] for r in registered}
    for lc in out:
        if lc.ownerId:
            lc.ownerName = owner_names.get(lc.ownerId)
        lc.featureCount = counts.get(lc.id)
        if lc.displayField is None:
            lc.displayField = derived_fields.get(lc.id)  # effective = derived when the raw column was unset
        src = view_sources.get(lc.id)
        if src and src in viewable_ids:
            lc.sourceLayerId = src
            # Fall back to the source id when it has no display_name (defensive) — matches
            # how the SPA names layers (displayName ?? id).
            lc.sourceName = display_names.get(src) or src
    return MyLayersResponse(layers=out)


# --- favorites (Talus 18; generalized to any catalog item type — Talus 24) ------
#
# A favorite is a personal UI preference: "star this item in the Catalog." Generic over
# item_type ('layer' | 'web_map'). The ADD write is VIEW-GATED — you may only favorite an item
# you can view (rejects an unknown/unviewable id with 403; fails CLOSED 503 if OpenFGA is down).
# REMOVAL is ungated so you can always clean up a star (incl. one to an item you've since lost
# access to). Both are IDEMPOTENT, 204: PUT-twice stays favorited (ON CONFLICT DO NOTHING),
# DELETE-twice stays gone.

_FAVORITABLE_ITEM_TYPES = frozenset({"layer", "web_map", "raster", "external", "field_project"})


async def _assert_can_view_item(user: User, item_type: str, item_id: str, op: str) -> None:
    """View-gate a favorite ADD: 400 for an unknown item_type, else 403 unless the caller can view
    the item (layer: can_view on the layer; web_map: can_view on the web_map). Fails closed (503)."""
    if item_type not in _FAVORITABLE_ITEM_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown item type {item_type!r}"
        )
    if item_type in ("layer", "external"):
        # An external ref's OpenFGA object rides the `layer` type (decision-external-catalog-
        # items.md §3.4), so view_allowed (default object_type "layer") answers for both.
        allowed = await view_allowed(user, item_id)
    else:  # web_map / raster / field_project — generic OpenFGA can_view (object_type == item_type)
        allowed = await _openfga_check(user.id, "can_view", item_id, object_type=item_type)
    if not allowed:
        log.info(
            "favorite_rejected", op=op, item_type=item_type, item=item_id,
            reason="no_view_access", user_id=str(user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to view this item"
        )


@router.put(
    "/me/favorites/{item_type}/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Star a catalog item for the caller (idempotent; view-gated)",
)
async def add_favorite(
    item_type: str,
    item_id: str,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    await _assert_can_view_item(user, item_type, item_id, "add")
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO platform.user_favorites (user_id, item_type, item_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, item_type, item_id) DO NOTHING",
            (user.id, item_type, item_id),
        )
    await conn.commit()


@router.delete(
    "/me/favorites/{item_type}/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unstar a catalog item for the caller (idempotent)",
)
async def remove_favorite(
    item_type: str,
    item_id: str,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    # Ungated: unstarring is always safe and lets you clear an orphaned star.
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM platform.user_favorites "
            "WHERE user_id = %s AND item_type = %s AND item_id = %s",
            (user.id, item_type, item_id),
        )
    await conn.commit()


# --- self-service password change (decision-first-ten-minutes.md F3) -------------------------


class PasswordChange(BaseModel):
    """The whole request surface: two opaque strings.

    ★ There is deliberately NO user id / username / realm field here. The account affected is
    resolved ONLY from the server-validated token, so the body cannot steer WHO is changed — the
    A.1 trap ("name an input untrusted, then trust a value out of it") is closed structurally
    rather than by a check that could later be edited away. Cross-user resets remain the admin
    endpoint's job (POST /admin/users/{id}/reset-password).
    """

    currentPassword: str = Field(min_length=1, max_length=256)
    newPassword: str = Field(min_length=1, max_length=256)


@router.post(
    "/me/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change the caller's own password (proves possession of the current one)",
)
async def change_my_password(
    payload: PasswordChange,
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> None:
    """Change your own password.

    Two steps, never merged: prove possession of the CURRENT password, then write the new one.
    Skipping the first would make a lifted bearer token sufficient to set a new password — a full
    account takeover that also locks the real owner out. The re-authentication IS the security
    value of this endpoint.

    Reachability: `get_current_user` is Bearer-only, so a personal access token (which rides
    `get_optional_user`) structurally cannot reach this path. An app password must never be able
    to rotate the account credential. Pinned by inv27, not left implicit.
    """
    if user.principal_type == "service":
        # A service account authenticates with a client secret; "password" is not its credential.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="service accounts do not have a password — rotate the client secret instead",
        )

    # The username for the grant comes from the validated token (never the body).
    username = ""
    if claims:
        username = str(claims.get("preferred_username") or "")
    username = username or user.username
    if not username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="this account has no username to authenticate with",
        )

    # SSO users first: they have no local password, and a grant attempt would fail with the same
    # message a wrong password gives — an unhelpful lie that also burns a brute-force attempt.
    idps = await keycloak_admin.user_federated_identities(str(user.id))
    if idps:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"this account signs in through {idps[0]} — change your password with that "
                "identity provider, not in Talus"
            ),
        )

    ok, reason = await keycloak_admin.verify_user_password(username, payload.currentPassword)
    if not ok:
        if reason == "locked":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "too many incorrect attempts — this account is temporarily locked. "
                    "Wait for the lockout to clear, or ask an administrator to reset it."
                ),
            )
        log.info("self_password_denied", sub=str(user.id), reason=reason)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the current password is not correct",
        )

    if payload.newPassword == payload.currentPassword:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="the new password must differ from the current one",
        )

    # Keycloak validates the realm password policy on this write and 400s with its own reason,
    # which set_user_password surfaces. temporary=False so the user is not immediately forced to
    # change the password they just chose.
    await keycloak_admin.set_user_password(str(user.id), payload.newPassword)
    log.info("self_password_changed", sub=str(user.id))
