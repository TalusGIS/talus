"""Groups management UI — read surface (decision-groups-ui.md, commit 2).

The on-thesis Groups page (browse / create / membership / shared content) — the same self-service
thesis as Talus 27 user-admin, NOT AGO's collaboration hub. This module is the READ half:

  GET /me/groups      — the browse surface: { mine, org } group cards (name, summary, member
                        count, my role). `mine` = groups I own or belong to; `org` = other
                        (non-system) registry groups, discoverable org-wide (Talus is single-org;
                        group names are already non-sensitive, exposed by /me/shareable-groups).
  GET /groups/{id}    — group detail: owner + members (name-resolved from platform.users) + the
                        items (layers/maps/rasters) shared to the group, with per-item edit
                        legibility for layers. Gated to member/owner/admin; non-members get 404
                        (existence-hiding, mirroring require_view_or_public).

Authority stays where it lives: OpenFGA owns membership (`member`) + management (`owner`); the
platform.groups registry (V026 + V053) only NAMES + labels. Edit capability is NOT a group
concern — a feature write needs the layer's `editable` flag × the user's data-editor role ×
can_edit_access (the three-key model, authz.edit_allowed); this surface only SHOWS it.

Everything fails closed: the _openfga_read_* helpers raise 503 on any OpenFGA error, so a degraded
authz service never leaks a group's members or content.
"""
import re
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.alias_generators import to_camel

from .. import admin_audit
from ..auth import _validated_claims, get_current_user
from ..authz import (
    _has_admin_role,
    _has_publish_role,
    _objects_shared_with_group,
    _openfga_check,
    group_share_tuples,
    _openfga_delete,
    _openfga_read_object_tuples,
    _openfga_write,
    edit_allowed,
    transfer_object_owner,
)
from ..db import get_db
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["groups"])


# --- response models ------------------------------------------------------------
#
# All request + response bodies are camelCase on the wire (the API convention — cf. /me/layers,
# custom_domains). populate_by_name lets requests also accept the snake field names; FastAPI
# serializes responses by alias, so the JS client sees groupId / memberCount / canEdit etc.


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


GroupRole = Literal["owner", "member"]


class GroupCard(_CamelModel):
    group_id: str
    display_name: str
    summary: str | None
    member_count: int
    system: bool
    role: GroupRole | None  # my relationship: owner / member / None (org-discoverable only)


class MyGroups(_CamelModel):
    mine: list[GroupCard]
    org: list[GroupCard]


class GroupMember(_CamelModel):
    user_id: str
    display_name: str
    username: str | None
    disabled: bool
    is_owner: bool


class GroupContentItem(_CamelModel):
    id: str
    title: str
    # layers only: the per-layer editable flag + whether the REQUESTING member may edit (the
    # three-key decision). None for maps/rasters (always read-only to recipients by model design).
    editable: bool | None = None
    can_edit: bool | None = None


class GroupContent(_CamelModel):
    layers: list[GroupContentItem]
    maps: list[GroupContentItem]
    rasters: list[GroupContentItem]


class GroupDetail(_CamelModel):
    group_id: str
    display_name: str
    summary: str | None
    system: bool
    delete_protected: bool  # absolute guard (decision §8 follow-up): blocks delete for owner AND admin
    owner: GroupMember | None  # None for ownerless groups (system / pre-registry seeds)
    my_role: Literal["owner", "member", "admin"] | None
    members: list[GroupMember]
    content: GroupContent


# --- helpers --------------------------------------------------------------------


def _strip_user(subject: str) -> str | None:
    """`user:<uuid>` -> `<uuid>`; None for anything that isn't a concrete user subject
    (e.g. a `user:*` wildcard, which groups never carry on member/owner)."""
    if subject.startswith("user:") and not subject.endswith(":*"):
        return subject[len("user:") :]
    return None


def _read_membership(tuples: list[tuple[str, str, str]]) -> tuple[list[str], str | None]:
    """From a group's tuples, return (member_ids, owner_id). Owner is a single relation in our
    write paths; if several exist we take the first deterministically (sorted)."""
    members: list[str] = []
    owners: list[str] = []
    for subj, rel, _obj in tuples:
        uid = _strip_user(subj)
        if uid is None:
            continue
        if rel == "member":
            members.append(uid)
        elif rel == "owner":
            owners.append(uid)
    owner_id = sorted(owners)[0] if owners else None
    # The owner is conventionally also a member; surface them in the list even if (defensively)
    # only the owner tuple exists.
    if owner_id and owner_id not in members:
        members.append(owner_id)
    return members, owner_id


async def _resolve_users(
    conn: AsyncConnection, user_ids: list[str]
) -> dict[str, tuple[str, str | None, bool]]:
    """user_id -> (display_name, username, disabled). Display falls back username -> id so a row
    is never blank. Unknown ids (tuple with no platform.users row) get a graceful placeholder."""
    if not user_ids:
        return {}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, display_name, username, disabled_at FROM platform.users "
            "WHERE id = ANY(%s)",
            (user_ids,),
        )
        rows = await cur.fetchall()
    out: dict[str, tuple[str, str | None, bool]] = {}
    for uid, display_name, username, disabled_at in rows:
        sid = str(uid)
        out[sid] = (display_name or username or sid, username, disabled_at is not None)
    return out


def _member(uid: str, resolved: dict[str, tuple[str, str | None, bool]], owner_id: str | None) -> GroupMember:
    display, username, disabled = resolved.get(uid, (uid, None, False))
    return GroupMember(
        user_id=uid, display_name=display, username=username,
        disabled=disabled, is_owner=(uid == owner_id),
    )


async def _titles(conn: AsyncConnection, table: str, id_col: str, title_col: str, ids: list[str]) -> dict[str, str]:
    """{id: title} for the given registry rows. `table`/`id_col`/`title_col` are code-literal
    (never user input) — no injection surface."""
    if not ids:
        return {}
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {id_col}, {title_col} FROM platform.{table} WHERE {id_col} = ANY(%s)",
            (ids,),
        )
        rows = await cur.fetchall()
    return {str(r[0]): (r[1] or str(r[0])) for r in rows}


# --- endpoints ------------------------------------------------------------------


@router.get("/me/groups", summary="Groups the caller owns/belongs to + org-discoverable groups")
async def my_groups(
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MyGroups:
    """The browse surface. Reads every registry group and classifies each from its OpenFGA tuples
    (one read per group → member_count + owner + my role in a single pass). N reads is fine at
    municipal scale (dozens of groups); revisit with a cached count if an org grows large."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT group_id, display_name, summary, system FROM platform.groups "
            "ORDER BY display_name"
        )
        registry = await cur.fetchall()

    me = str(user.id)
    mine: list[GroupCard] = []
    org: list[GroupCard] = []
    for group_id, display_name, summary, system in registry:
        members, owner_id = _read_membership(
            await _openfga_read_object_tuples(f"group:{group_id}")
        )
        if me == owner_id:
            role: GroupRole | None = "owner"
        elif me in members:
            role = "member"
        else:
            role = None
        card = GroupCard(
            group_id=group_id, display_name=display_name, summary=summary,
            member_count=len(members), system=system, role=role,
        )
        if role is not None:
            mine.append(card)
        elif not system:  # org-discoverable; system groups stay internal
            org.append(card)
    return MyGroups(mine=mine, org=org)


@router.get("/groups/{group_id}", summary="Group detail: members + shared content (member/owner/admin only)")
async def group_detail(
    group_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> GroupDetail:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT display_name, summary, system, delete_protected "
            "FROM platform.groups WHERE group_id = %s",
            (group_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")
    display_name, summary, system, delete_protected = row

    members, owner_id = _read_membership(
        await _openfga_read_object_tuples(f"group:{group_id}")
    )
    me = str(user.id)
    is_admin = _has_admin_role(claims)
    # Gate: only a member/owner/admin may see a group's roster + content. Non-members get 404 —
    # existence-hiding, mirroring require_view_or_public (don't reveal a private group exists).
    if not (is_admin or me == owner_id or me in members):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")

    resolved = await _resolve_users(conn, members)
    member_objs = sorted(
        (_member(uid, resolved, owner_id) for uid in members),
        key=lambda m: (not m.is_owner, m.display_name.lower()),  # owner first, then by name
    )
    owner_obj = _member(owner_id, resolved, owner_id) if owner_id else None
    my_role: Literal["owner", "member", "admin"] | None = (
        "owner" if me == owner_id else "member" if me in members else "admin" if is_admin else None
    )

    # Content shared to the group (reuses the featured-gallery read). Resolve titles per type.
    layer_ids = await _objects_shared_with_group(group_id, "layer")
    map_ids = await _objects_shared_with_group(group_id, "web_map")
    raster_ids = await _objects_shared_with_group(group_id, "raster")

    layer_titles = await _titles(conn, "layers", "layer_id", "display_name", layer_ids)
    map_titles = await _titles(conn, "web_maps", "id", "title", map_ids)
    raster_titles = await _titles(conn, "rasters", "id", "title", raster_ids)

    # Per-layer edit legibility for the REQUESTING user (the three-key decision). This is what
    # makes "editors + viewers coexist in one group" visible instead of hidden.
    layers: list[GroupContentItem] = []
    for lid in layer_ids:
        allowed, _reason = await edit_allowed(conn, claims, user, lid)
        async with conn.cursor() as cur:
            await cur.execute("SELECT editable FROM platform.layers WHERE layer_id = %s", (lid,))
            er = await cur.fetchone()
        layers.append(GroupContentItem(
            id=lid, title=layer_titles.get(lid, lid),
            editable=(bool(er[0]) if er else None), can_edit=allowed,
        ))

    return GroupDetail(
        group_id=group_id, display_name=display_name, summary=summary, system=system,
        delete_protected=bool(delete_protected),
        owner=owner_obj, my_role=my_role, members=member_objs,
        content=GroupContent(
            layers=layers,
            maps=[GroupContentItem(id=i, title=map_titles.get(i, i)) for i in map_ids],
            rasters=[GroupContentItem(id=i, title=raster_titles.get(i, i)) for i in raster_ids],
        ),
    )


# --- create / settings / delete -------------------------------------------------
#
# Group MUTATIONS live here with the reads (the groups domain owns its lifecycle). Create lifts the
# former publisher.create_group into the Groups UI surface (decision §6: publisher/admin only) and
# fixes the slug-collision bug (§11.1). Settings honour the immutability rule (§8): the slug/group_id
# is NEVER mutated; only display_name + summary are editable.

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Lowercase; any run of non-alphanumerics -> single underscore; trim edge underscores. Result
    is [a-z0-9_]; may be empty (caller rejects). Identical to the former publisher._slugify so the
    OpenFGA object id stays the platform.groups primary key shape."""
    return _SLUG_RE.sub("_", name.strip().lower()).strip("_")


async def _require_publisher(
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> User:
    """403 unless the caller holds talus-publisher or talus-admin (decision §6 — minting groups
    stays with the GIS team; any user can be a MEMBER). Mirrors publisher._require_publisher."""
    if not _has_publish_role(claims):
        log.info("authz_rejected", capability="groups:create", reason="missing_publish_role",
                 user_id=str(user.id))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="not authorized to create groups")
    return user


class GroupCreate(_CamelModel):
    name: str
    summary: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        return v


class GroupUpdate(_CamelModel):
    """Editable metadata only. group_id/slug is intentionally absent — immutable (§8). delete_protected
    is the absolute deletion guard, toggled here as the Edit-details checkbox."""
    display_name: str | None = None
    summary: str | None = None
    delete_protected: bool | None = None

    @field_validator("display_name")
    @classmethod
    def _name_not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("display_name must not be empty")
        return v


class CreatedGroup(_CamelModel):
    group: str       # the OpenFGA object ("group:<slug>")
    group_id: str    # the slug / registry key
    display_name: str


async def _load_manageable(
    conn: AsyncConnection, claims: dict[str, Any] | None, user: User, group_id: str
) -> tuple[bool, bool, str | None]:
    """Shared gate for PATCH/DELETE: the group must exist (else 404) and the caller must be its
    OpenFGA `owner` OR a talus-admin (else 403). Ownerless groups (system / pre-registry seeds)
    have no owner tuple, so only an admin passes — mirroring manage_allowed's "no owner = admin
    only". Returns (system, delete_protected, owner_id)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT system, delete_protected FROM platform.groups WHERE group_id = %s", (group_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")
    system, protected = bool(row[0]), bool(row[1])
    _members, owner_id = _read_membership(await _openfga_read_object_tuples(f"group:{group_id}"))
    if not (_has_admin_role(claims) or str(user.id) == owner_id):
        log.info("authz_rejected", capability=f"group:{group_id}:manage",
                 reason="not_owner_or_admin", user_id=str(user.id))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="not authorized to manage this group")
    return system, protected, owner_id


_SYSTEM_GROUP_IDS = frozenset({"home", "open_data"})  # seeded by V032 / V034; their shares are real


async def _clear_leftover_group_shares(slug: str, *, actor: str) -> int:
    """Delete every `shared_group` tuple that points at `group:<slug>` and return how many. Called
    ONLY when no group with this id exists (create_group has just proved that), so any such tuple is
    an orphan from a group deleted earlier. Never runs for a system group id: Home and Open Data
    hold real shares, and their registry rows are what normally stops them being 'created'."""
    if slug in _SYSTEM_GROUP_IDS:
        return 0
    leftover = await group_share_tuples(slug)
    if not leftover:
        return 0
    await _openfga_delete(leftover)
    by_type: dict[str, int] = {}
    for _user, _rel, obj in leftover:
        by_type[obj.split(":", 1)[0]] = by_type.get(obj.split(":", 1)[0], 0) + 1
    log.warning("group_leftover_shares_cleared", group=f"group:{slug}", actor=actor,
                count=len(leftover), by_type=by_type)
    return len(leftover)


@router.post("/groups", status_code=status.HTTP_201_CREATED,
             summary="Create a group the caller owns (publisher/admin only)")
async def create_group(
    payload: GroupCreate,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(_require_publisher),
) -> CreatedGroup:
    actor = str(user.id)
    slug = _slugify(payload.name)
    if not slug:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="group name has no usable characters for an id")
    obj = f"group:{slug}"

    # §11.1 FIX: a pre-existing slug is a CONFLICT, not a silent join. The former ON CONFLICT DO
    # NOTHING returned 201 + added the caller as a member of someone else's similarly-named group.
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.groups WHERE group_id = %s", (slug,))
        if await cur.fetchone() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"a group with a similar name already exists (id '{slug}')",
            )

    # A group's id comes from its NAME, so this id may have been used before. A group deleted before
    # the once-over fix (A9) left its shares on four item types behind; clear anything still pointing
    # at this id BEFORE the new group exists, so its members inherit nothing. A failure raises 503
    # and no group is created.
    await _clear_leftover_group_shares(slug, actor=actor)

    # OpenFGA FIRST — the access authority. Write owner + member for the creator (decision §5: the
    # creator owns it and is the first member). A failure here raises 503 and NO registry row is
    # written — a group whose owning tuples can't be written must fail closed, not half-exist.
    await _openfga_write([(f"user:{actor}", "owner", obj), (f"user:{actor}", "member", obj)])

    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO platform.groups (group_id, display_name, summary, created_by, updated_by) "
            "VALUES (%s, %s, %s, %s, %s)",
            (slug, payload.name.strip(), (payload.summary or None), actor, actor),
        )
    await conn.commit()
    log.info("group_created", group=obj, actor=actor)
    return CreatedGroup(group=obj, group_id=slug, display_name=payload.name.strip())


@router.patch("/groups/{group_id}", summary="Edit a group's display name / summary (owner or admin)")
async def update_group(
    group_id: str,
    payload: GroupUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    await _load_manageable(conn, claims, user, group_id)  # 404 / 403 gate
    sets: list[str] = []
    vals: list[Any] = []
    if payload.display_name is not None:
        sets.append("display_name = %s")
        vals.append(payload.display_name.strip())
    if payload.summary is not None:
        # empty string clears the summary back to NULL
        sets.append("summary = %s")
        vals.append(payload.summary.strip() or None)
    if payload.delete_protected is not None:
        sets.append("delete_protected = %s")
        vals.append(payload.delete_protected)
    if not sets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="nothing to update")
    sets.append("updated_at = now()")
    sets.append("updated_by = %s")
    vals.append(str(user.id))
    vals.append(group_id)
    async with conn.cursor() as cur:
        await cur.execute(
            f"UPDATE platform.groups SET {', '.join(sets)} WHERE group_id = %s", vals
        )
    await conn.commit()
    log.info("group_updated", group=f"group:{group_id}", actor=str(user.id))
    return {"groupId": group_id, "status": "updated"}


@router.delete("/groups/{group_id}", summary="Delete a group (owner or admin; never a system group)")
async def delete_group(
    group_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Owner/admin only. Refuses system groups (home/open_data). Cascade order is OpenFGA-FIRST so
    no access ever dangles: (1) drop every shared_group tuple that points at this group — the items
    stay, they're just no longer reachable VIA this group; (2) drop the group's member + owner
    tuples; (3) delete the registry row. A mid-failure raises 503 and leaves a recoverable state
    (the registry row still labels the group), never a leaked allow."""
    system, protected, _owner = await _load_manageable(conn, claims, user, group_id)
    if system:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="system groups cannot be deleted")
    # ABSOLUTE delete protection (option B): blocks EVERYONE — owner AND admin. The
    # only path to delete is to turn protection off first (a deliberate two-step). No admin override
    # (unlike the layer/map paths today — that inconsistency is a tracked follow-up). Guards human error.
    if protected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This group is protected from deletion. Turn off delete protection in Edit details first.",
        )
    obj = f"group:{group_id}"

    # (1) shared_group tuples (group -> item), across EVERY shareable type — the one list in authz
    # (GROUP_SHAREABLE_TYPES), not a hand-written subset: this loop once named three of the seven,
    # and the shares it missed were inherited by the next group of the same name (once-over A9).
    share_tuples = await group_share_tuples(group_id)
    if share_tuples:
        await _openfga_delete(share_tuples)

    # (2) member + owner tuples (every tuple whose object is this group).
    member_tuples = await _openfga_read_object_tuples(obj)
    if member_tuples:
        await _openfga_delete(member_tuples)

    # (3) registry row.
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM platform.groups WHERE group_id = %s", (group_id,))
    await conn.commit()
    log.info("group_deleted", group=obj, actor=str(user.id),
             shared_unlinked=len(share_tuples), members_removed=len(member_tuples))
    return {"groupId": group_id, "status": "deleted",
            "sharedItemsUnlinked": len(share_tuples), "membersRemoved": len(member_tuples)}


# --- membership + ownership transfer --------------------------------------------
#
# Owner-managed (decision §7): the group owner OR an admin adds/removes members directly. No
# join-request / invite workflow in v1. Add validates the target is a real, ENABLED human principal
# (you can't seed a group with disabled accounts or service accounts via this UI). Diffs against the
# current roster so re-adds / removing a non-member are graceful, not OpenFGA duplicate-write errors.


class MembersAdd(_CamelModel):
    user_ids: list[str]


class TransferOwner(_CamelModel):
    user_id: str


@router.post("/groups/{group_id}/members", summary="Add member(s) to a group (owner or admin)")
async def add_members(
    group_id: str,
    payload: MembersAdd,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _load_manageable(conn, claims, user, group_id)  # 404 / 403 gate
    ids = list(dict.fromkeys(payload.user_ids))  # dedupe, preserve order
    if not ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no users supplied")

    # Validate: every target must be an ENABLED human principal. Service accounts get group
    # membership via the SA-admin flow, not this human-facing surface.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, principal_type, disabled_at FROM platform.users WHERE id = ANY(%s)",
            (ids,),
        )
        rows = {str(r[0]): (r[1], r[2]) for r in await cur.fetchall()}
    invalid = [
        i for i in ids
        if i not in rows or rows[i][0] != "human" or rows[i][1] is not None
    ]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"cannot add (unknown, disabled, or non-human): {invalid}",
        )

    obj = f"group:{group_id}"
    current, _owner = _read_membership(await _openfga_read_object_tuples(obj))
    to_add = [i for i in ids if i not in current]
    if to_add:
        await _openfga_write([(f"user:{i}", "member", obj) for i in to_add])
    log.info("group_members_added", group=obj, actor=str(user.id),
             added=len(to_add), requested=len(ids))
    return {"groupId": group_id, "added": to_add, "alreadyMembers": [i for i in ids if i in current]}


@router.delete("/groups/{group_id}/members/{user_id}", summary="Remove a member (owner or admin)")
async def remove_member(
    group_id: str,
    user_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    _system, _protected, owner_id = await _load_manageable(conn, claims, user, group_id)
    if user_id == owner_id:
        # The owner is structurally protected (mirrors T27's last-admin guard): removing them
        # would orphan the group. Transfer ownership or delete the group instead.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cannot remove the group owner; transfer ownership or delete the group first",
        )
    obj = f"group:{group_id}"
    current, _o = _read_membership(await _openfga_read_object_tuples(obj))
    if user_id not in current:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="user is not a member of this group")
    await _openfga_delete([(f"user:{user_id}", "member", obj)])
    log.info("group_member_removed", group=obj, actor=str(user.id), removed=user_id)
    return {"groupId": group_id, "removed": user_id, "status": "removed"}


@router.post("/groups/{group_id}/transfer", summary="Transfer group ownership to a member (owner or admin)")
async def transfer_group(
    group_id: str,
    payload: TransferOwner,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _load_manageable(conn, claims, user, group_id)  # 404 / 403 gate
    obj = f"group:{group_id}"
    members, _owner = _read_membership(await _openfga_read_object_tuples(obj))
    if payload.user_id not in members:
        # Mirrors AGO: you can only hand a group to someone already in it.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="the new owner must already be a member of the group",
        )
    previous = await transfer_object_owner(obj, payload.user_id)  # fail-safe; old owner stays a member
    log.info("group_transferred", group=obj, actor=str(user.id),
             new_owner=payload.user_id, previous_owner=previous)
    return {"groupId": group_id, "owner": payload.user_id, "previousOwner": previous}


# --- group content (share / unshare items) --------------------------------------
#
# Decision §9 (fork E): a group MEMBER shares items they OWN to the group; remove is item-owner,
# group-owner, or admin. This is the EXISTING share permission surfaced from the group page — it
# writes the same `shared_group` tuple the item's own share dialog does, additively (never the
# full-replace set_object_shares, which would clobber the item's other group shares). NOT a
# content-UPLOAD surface (drift fence) — the item must already exist and be owned by the caller.

ShareableType = Literal["layer", "web_map", "raster"]


class ItemRef(_CamelModel):
    item_type: ShareableType
    id: str


class ItemsAdd(_CamelModel):
    items: list[ItemRef]


async def _group_membership_or_404(
    conn: AsyncConnection, group_id: str
) -> tuple[list[str], str | None]:
    """(members, owner_id) for an EXISTING group; 404 if the registry has no such group."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.groups WHERE group_id = %s", (group_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")
    return _read_membership(await _openfga_read_object_tuples(f"group:{group_id}"))


async def _owns_item(user: User, claims: dict[str, Any] | None, item_type: str, item_id: str) -> bool:
    """admin OR the OpenFGA `owner` of the item. A non-existent item has no owner tuple → False
    (existence-hiding: 'you don't own it' rather than 'it doesn't exist')."""
    return _has_admin_role(claims) or await _openfga_check(
        user.id, "owner", item_id, object_type=item_type
    )


@router.post("/groups/{group_id}/items", summary="Share the caller's item(s) to a group (member; item owner/admin)")
async def add_items(
    group_id: str,
    payload: ItemsAdd,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    members, owner_id = await _group_membership_or_404(conn, group_id)
    me = str(user.id)
    # Any member (or the owner / an admin) may contribute their own items to the group.
    if not (_has_admin_role(claims) or me == owner_id or me in members):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="must be a member of the group to add items to it")
    if not payload.items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no items supplied")

    # Validate ownership of EVERY item before writing anything (all-or-nothing — no partial shares).
    forbidden = [
        f"{it.item_type}:{it.id}"
        for it in payload.items
        if not await _owns_item(user, claims, it.item_type, it.id)
    ]
    if forbidden:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"not the owner of: {forbidden}")

    grp = f"group:{group_id}"
    added: list[str] = []
    already: list[str] = []
    for it in payload.items:
        ref = f"{it.item_type}:{it.id}"
        existing = await _openfga_read_object_tuples(ref)
        if (grp, "shared_group", ref) in existing:
            already.append(ref)
        else:
            await _openfga_write([(grp, "shared_group", ref)])
            added.append(ref)
    log.info("group_items_added", group=grp, actor=me, added=len(added), already=len(already))
    await admin_audit.record(
        conn, actor_id=user.id, action="group_items_added", target_type="group", target_id=group_id,
        detail={"added": added, "alreadyShared": already},
    )
    return {"groupId": group_id, "added": added, "alreadyShared": already}


@router.delete("/groups/{group_id}/items/{item_type}/{item_id}",
               summary="Unshare an item from a group (item owner / group owner / admin)")
async def remove_item(
    group_id: str,
    item_type: ShareableType,
    item_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    _members, owner_id = await _group_membership_or_404(conn, group_id)
    me = str(user.id)
    # Either side of the share may sever it: the item's owner, the group's owner, or an admin.
    if not (_has_admin_role(claims) or me == owner_id
            or await _owns_item(user, claims, item_type, item_id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="not authorized to unshare this item from the group")
    grp = f"group:{group_id}"
    ref = f"{item_type}:{item_id}"
    existing = await _openfga_read_object_tuples(ref)
    if (grp, "shared_group", ref) not in existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="item is not shared to this group")
    await _openfga_delete([(grp, "shared_group", ref)])
    log.info("group_item_removed", group=grp, actor=me, item=ref)
    await admin_audit.record(
        conn, actor_id=user.id, action="group_item_removed", target_type="group", target_id=group_id,
        detail={"item": ref},
    )
    return {"groupId": group_id, "removed": ref, "status": "removed"}
