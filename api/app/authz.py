"""Layer 7b authorization — the three-key capability model.

A write succeeds iff: the layer is **editable** (platform.layers) AND the user has
**edit access** to it (OpenFGA can_edit_access) AND the user's **role** is
data-editor or higher (Keycloak realm role). A read succeeds iff the user has
**view access** (OpenFGA can_view) — role-independent.

The three keys live in three places on purpose (see
docs/layer-7b-openfga-authorization-decision.md):
  - editable  -> platform.layers.editable        (a property of the layer)
  - access    -> OpenFGA can_view/can_edit_access (relational: user/group/org -> layer)
  - role      -> Keycloak realm role              (a property of the person)

`require_capability` is the ONE place all three combine, and GET /me/layers calls the
same `view_allowed`/`edit_allowed` helpers — so the UI can never offer an edit the api
then refuses (single source of truth).

OpenFGA is reached over the internal compose network with plain httpx (already a
dependency; same shape as the tile proxy). If OpenFGA is unreachable, checks fail
CLOSED (503) — never allow on error.
"""
import asyncio
from typing import Any
from uuid import UUID

import httpx
import structlog
from fastapi import Depends, HTTPException, status
from psycopg import AsyncConnection

from .auth import _optional_claims, _validated_claims, get_current_user, get_optional_user
from .config import settings
from .db import get_db
from .models.user import User
from .public_sharing import public_sharing_enabled

log = structlog.get_logger(__name__)

# The role key: data-editor or higher. `talus-editor` (the retired 7a boolean) is
# deliberately NOT here — Step 3 replaces it with this ladder.
_EDIT_ROLES = frozenset({"talus-data-editor", "talus-publisher", "talus-admin"})

# The CREATE key: stricter than _EDIT_ROLES. A plain talus-data-editor may EDIT an
# existing layer but must NOT mint new ones — claiming a slot is publisher/admin only.
_PUBLISH_ROLES = frozenset({"talus-publisher", "talus-admin"})

_OPENFGA_TIMEOUT = httpx.Timeout(5.0)

# Release reads every tuple on an object then deletes them in batches (decision-11 §Q4).
# Page size for the paginated Read; batch size for deletes (kept <= the store's per-write
# transaction cap so a slot that has accumulated shares still releases in whole batches).
_OPENFGA_READ_PAGE_SIZE = 50
_OPENFGA_DELETE_BATCH = 50

# Boot-guard retry policy (boot guards — shared by store and model probes). BOUNDED — never infinite: a
# transiently-unhealthy OpenFGA gets a few short retries, then startup fails loudly.
_BOOT_OPENFGA_RETRIES = 5
_BOOT_OPENFGA_RETRY_SLEEP = 2.0  # seconds between attempts

# OpenFGA relations from the loaded model (db/openfga/model.fga).
_VIEW = "can_view"
_EDIT_ACCESS = "can_edit_access"


def _no_claims_is_authorized() -> bool:
    """What the three role helpers answer for `claims is None`: True in STUB mode only.

    ★ `claims is None` means two different things, and only one of them may pass
    (decision-once-over-security-fixes.md A3). In stub mode there is no token at all and every
    caller is the dev user: authorized. In keycloak mode it is what an OPTIONAL-auth door sees for a
    caller who came with a personal access token (a real principal from HTTP Basic, no JWT) or with
    nothing — and answering True there made any token an administrator to every gate that asked:
    a viewer's read-only token downloaded the metadata of items the viewer could not see. A caller
    without claims holds NO role; a door that wants a token caller's roles resolves them explicitly
    (auth.pat_owner_claims), as the GeoServices and native write doors do."""
    return settings.auth_mode == "stub"


def _has_edit_role(claims: dict[str, Any] | None) -> bool:
    """True if the token carries a data-editor-or-higher realm role.

    No claims: authorized in stub mode only (see _no_claims_is_authorized).
    """
    if claims is None:
        return _no_claims_is_authorized()
    roles = (claims.get("realm_access") or {}).get("roles") or []
    return any(r in _EDIT_ROLES for r in roles)


def _has_publish_role(claims: dict[str, Any] | None) -> bool:
    """True if the token carries a publisher-or-admin realm role — the gate for
    CREATING layers/groups (stricter than _has_edit_role).

    No claims: authorized in stub mode only (see _no_claims_is_authorized).
    """
    if claims is None:
        return _no_claims_is_authorized()
    roles = (claims.get("realm_access") or {}).get("roles") or []
    return any(r in _PUBLISH_ROLES for r in roles)


def _has_admin_role(claims: dict[str, Any] | None) -> bool:
    """True if the token carries the `talus-admin` realm role SPECIFICALLY — the admin
    release/recovery path (decision-11 §Q1). Narrower than _has_publish_role, which also
    accepts talus-publisher; admin-release must not be reachable by a mere publisher.

    No claims: authorized in stub mode only (see _no_claims_is_authorized).
    """
    if claims is None:
        return _no_claims_is_authorized()
    roles = (claims.get("realm_access") or {}).get("roles") or []
    return "talus-admin" in roles


async def _openfga_check(
    user_id: UUID, relation: str, object_id: str, *, object_type: str = "layer"
) -> bool:
    """Resolve one OpenFGA check: does `user:<id>` have `relation` on `<object_type>:<id>`?

    `object_type` defaults to "layer" so every existing layer caller is unchanged; the web_map
    router passes object_type="web_map" (Talus 20). Fails closed: any transport/HTTP error
    becomes a 503 (deny), never an allow.
    """
    if not settings.openfga_store_id or not settings.openfga_model_id:
        log.error("openfga_unconfigured", store=bool(settings.openfga_store_id))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service not configured",
        )
    url = f"{settings.openfga_api_url}/stores/{settings.openfga_store_id}/check"
    body = {
        "authorization_model_id": settings.openfga_model_id,
        "tuple_key": {
            "user": f"user:{user_id}",
            "relation": relation,
            "object": f"{object_type}:{object_id}",
        },
    }
    try:
        async with httpx.AsyncClient(timeout=_OPENFGA_TIMEOUT) as client:
            resp = await client.post(url, json=body)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning(
            "openfga_unreachable",
            relation=relation,
            object=f"{object_type}:{object_id}",
            error=exc.__class__.__name__,
            detail=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service unavailable",
        ) from exc
    return bool(resp.json().get("allowed", False))


async def _openfga_write(tuples: list[tuple[str, str, str]]) -> None:
    """Write relationship tuples to OpenFGA. Each item is (user, relation, object),
    e.g. ("user:<sub>", "owner", "layer:<id>"). Body shape mirrors
    scripts/seed-county-boundary-tuples.sh.

    Fails CLOSED, same discipline as _openfga_check: any transport/HTTP error becomes
    a 503, never a silent success — the caller must not proceed as if access was
    granted. An "already exists" rejection is idempotent SUCCESS (re-claim / re-run),
    so it returns normally.
    """
    if not settings.openfga_store_id or not settings.openfga_model_id:
        log.error("openfga_unconfigured", store=bool(settings.openfga_store_id))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service not configured",
        )
    url = f"{settings.openfga_api_url}/stores/{settings.openfga_store_id}/write"
    body = {
        "authorization_model_id": settings.openfga_model_id,
        "writes": {
            "tuple_keys": [
                {"user": u, "relation": r, "object": o} for (u, r, o) in tuples
            ]
        },
    }
    try:
        async with httpx.AsyncClient(timeout=_OPENFGA_TIMEOUT) as client:
            resp = await client.post(url, json=body)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # OpenFGA returns 400 when a tuple already exists; that is idempotent success,
        # not a failure (mirrors the seed script's "already exists" -> skip).
        if "already exists" in exc.response.text.lower():
            log.info("openfga_write_exists", count=len(tuples))
            return
        log.warning(
            "openfga_write_rejected",
            upstream_status=exc.response.status_code,
            detail=exc.response.text[:500],
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service unavailable",
        ) from exc
    except httpx.HTTPError as exc:
        log.warning(
            "openfga_unreachable",
            op="write",
            error=exc.__class__.__name__,
            detail=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service unavailable",
        ) from exc


async def _openfga_read_object_tuples(object_id: str) -> list[tuple[str, str, str]]:
    """Read EVERY tuple whose object is `object_id` (e.g. "layer:<id>"), following the
    Read API's continuation token until the store is exhausted. Returns (user, relation,
    object) triples — the exact shape _openfga_delete consumes — across ALL relations
    (owner AND any org_shared/shared_group), per decision-11 §Q4. The Read API takes NO
    authorization_model_id (tuples are model-independent), so this body omits it.

    Fails CLOSED, same discipline as _openfga_check: any transport/HTTP error becomes a
    503 (deny), never a partial or empty list the caller might mistake for "no tuples"
    and proceed to flip the registry on.
    """
    if not settings.openfga_store_id or not settings.openfga_model_id:
        log.error("openfga_unconfigured", store=bool(settings.openfga_store_id))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service not configured",
        )
    url = f"{settings.openfga_api_url}/stores/{settings.openfga_store_id}/read"
    found: list[tuple[str, str, str]] = []
    continuation = ""
    try:
        async with httpx.AsyncClient(timeout=_OPENFGA_TIMEOUT) as client:
            while True:
                body: dict[str, Any] = {
                    "tuple_key": {"object": object_id},
                    "page_size": _OPENFGA_READ_PAGE_SIZE,
                }
                if continuation:
                    body["continuation_token"] = continuation
                resp = await client.post(url, json=body)
                resp.raise_for_status()
                data = resp.json()
                for t in data.get("tuples", []):
                    key = t.get("key") or {}
                    found.append((key["user"], key["relation"], key["object"]))
                continuation = data.get("continuation_token") or ""
                if not continuation:
                    break
    except httpx.HTTPError as exc:
        log.warning(
            "openfga_unreachable",
            op="read",
            object=object_id,
            error=exc.__class__.__name__,
            detail=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service unavailable",
        ) from exc
    return found


async def _openfga_read_user_groups(user_id: UUID) -> list[str]:
    """Every group slug the user is a `member` of — a service account's content-scope grants
    for the admin list view (Talus 19 c4). Uses the Read API's "object type, empty id" form
    (object="group:") to enumerate all group objects for the one (user, member) pair, following
    the continuation token until exhausted.

    Returns the BARE slugs (e.g. "parks_team" — the platform.groups PK), stripped of the
    "group:" prefix. Fails CLOSED like the other OpenFGA reads (transport/HTTP error -> 503); the
    list endpoint catches that and degrades that row's groups to empty rather than failing the
    whole list — this is admin display, not an access decision.
    """
    if not settings.openfga_store_id or not settings.openfga_model_id:
        log.error("openfga_unconfigured", store=bool(settings.openfga_store_id))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service not configured",
        )
    url = f"{settings.openfga_api_url}/stores/{settings.openfga_store_id}/read"
    prefix = "group:"
    slugs: list[str] = []
    continuation = ""
    try:
        async with httpx.AsyncClient(timeout=_OPENFGA_TIMEOUT) as client:
            while True:
                body: dict[str, Any] = {
                    "tuple_key": {
                        "user": f"user:{user_id}",
                        "relation": "member",
                        "object": prefix,
                    },
                    "page_size": _OPENFGA_READ_PAGE_SIZE,
                }
                if continuation:
                    body["continuation_token"] = continuation
                resp = await client.post(url, json=body)
                resp.raise_for_status()
                data = resp.json()
                for t in data.get("tuples", []):
                    obj = (t.get("key") or {}).get("object", "")
                    if obj.startswith(prefix):
                        slugs.append(obj[len(prefix):])
                continuation = data.get("continuation_token") or ""
                if not continuation:
                    break
    except httpx.HTTPError as exc:
        log.warning(
            "openfga_unreachable",
            op="read_user_groups",
            user_id=str(user_id),
            error=exc.__class__.__name__,
            detail=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service unavailable",
        ) from exc
    return slugs


# Every object type the authorization model lets a GROUP be shared with (`define shared_group` in
# db/openfga/model.fga). ONE list, used wherever a group's shares must be found — deleting a group
# cleaned three of these seven by hand and left the rest pointing at a dead id that a new group of
# the same name then inherited (decision-once-over-security-fixes.md A9).
# tests/test_group_share_cleanup.py reads the model file and fails if a type is missing here.
GROUP_SHAREABLE_TYPES: tuple[str, ...] = (
    "layer", "web_map", "raster", "field_project", "story_map", "dashboard", "report",
)


async def group_share_tuples(group_slug: str) -> list[tuple[str, str, str]]:
    """Every `shared_group` tuple that points at `group:<group_slug>`, across ALL shareable types,
    as (user, relation, object) ready for _openfga_delete. Fails closed (503) with the read."""
    obj = f"group:{group_slug}"
    tuples: list[tuple[str, str, str]] = []
    for object_type in GROUP_SHAREABLE_TYPES:
        for item_id in await _objects_shared_with_group(group_slug, object_type):
            tuples.append((obj, "shared_group", f"{object_type}:{item_id}"))
    return tuples


async def _objects_shared_with_group(group_slug: str, object_type: str) -> list[str]:
    """Every object of `object_type` shared to `group:<group_slug>` via the `shared_group` relation —
    the home page's featured set (items shared to group:home, Talus 30). Read API "object type, empty
    id" form (object="<type>:") for the one (group, shared_group) pair, following the continuation
    token. Returns BARE ids. Fails CLOSED (503) like the other reads; the home endpoint catches that
    and degrades to an empty gallery (less content, never a leak)."""
    if not settings.openfga_store_id or not settings.openfga_model_id:
        log.error("openfga_unconfigured", store=bool(settings.openfga_store_id))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service not configured",
        )
    url = f"{settings.openfga_api_url}/stores/{settings.openfga_store_id}/read"
    prefix = f"{object_type}:"
    ids: list[str] = []
    continuation = ""
    try:
        async with httpx.AsyncClient(timeout=_OPENFGA_TIMEOUT) as client:
            while True:
                body: dict[str, Any] = {
                    "tuple_key": {
                        "user": f"group:{group_slug}",
                        "relation": "shared_group",
                        "object": prefix,
                    },
                    "page_size": _OPENFGA_READ_PAGE_SIZE,
                }
                if continuation:
                    body["continuation_token"] = continuation
                resp = await client.post(url, json=body)
                resp.raise_for_status()
                data = resp.json()
                for t in data.get("tuples", []):
                    obj = (t.get("key") or {}).get("object", "")
                    if obj.startswith(prefix):
                        ids.append(obj[len(prefix):])
                continuation = data.get("continuation_token") or ""
                if not continuation:
                    break
    except httpx.HTTPError as exc:
        log.warning(
            "openfga_unreachable",
            op="objects_shared_with_group",
            group=group_slug,
            object_type=object_type,
            error=exc.__class__.__name__,
            detail=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service unavailable",
        ) from exc
    return ids


async def _public_shared_objects(object_type: str) -> list[str]:
    """Every object of `object_type` that is PUBLIC (`user:* public_shared <type>:`) — the governance
    lens for the admin public-content view (Talus 33 c6): "show me everything public," broader than
    the curated Open Data catalog (which is public ∩ curated). Same Read-API "object type, empty id"
    form + continuation paging as `_objects_shared_with_group`. Returns BARE ids. Fails CLOSED (503)
    — a public-exposure report that silently under-reports would be worse than an honest error."""
    if not settings.openfga_store_id or not settings.openfga_model_id:
        log.error("openfga_unconfigured", store=bool(settings.openfga_store_id))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service not configured",
        )
    url = f"{settings.openfga_api_url}/stores/{settings.openfga_store_id}/read"
    prefix = f"{object_type}:"
    ids: list[str] = []
    continuation = ""
    try:
        async with httpx.AsyncClient(timeout=_OPENFGA_TIMEOUT) as client:
            while True:
                body: dict[str, Any] = {
                    "tuple_key": {"user": _PUBLIC_USER, "relation": "public_shared", "object": prefix},
                    "page_size": _OPENFGA_READ_PAGE_SIZE,
                }
                if continuation:
                    body["continuation_token"] = continuation
                resp = await client.post(url, json=body)
                resp.raise_for_status()
                data = resp.json()
                for t in data.get("tuples", []):
                    obj = (t.get("key") or {}).get("object", "")
                    if obj.startswith(prefix):
                        ids.append(obj[len(prefix):])
                continuation = data.get("continuation_token") or ""
                if not continuation:
                    break
    except httpx.HTTPError as exc:
        log.warning(
            "openfga_unreachable", op="public_shared_objects", object_type=object_type,
            error=exc.__class__.__name__, detail=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service unavailable",
        ) from exc
    return ids


async def _openfga_delete(tuples: list[tuple[str, str, str]]) -> None:
    """Delete relationship tuples from OpenFGA in batches of _OPENFGA_DELETE_BATCH. Each
    item is (user, relation, object). No-op on an empty list (the half-released recovery
    path reads zero tuples and lands here).

    Fails CLOSED, same discipline as _openfga_write. A tuple already gone is idempotent
    SUCCESS — mirrors _openfga_write's "already exists" handling — so a re-run after a
    partial delete completes instead of erroring.
    """
    if not tuples:
        return
    if not settings.openfga_store_id or not settings.openfga_model_id:
        log.error("openfga_unconfigured", store=bool(settings.openfga_store_id))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service not configured",
        )
    url = f"{settings.openfga_api_url}/stores/{settings.openfga_store_id}/write"
    try:
        async with httpx.AsyncClient(timeout=_OPENFGA_TIMEOUT) as client:
            for start in range(0, len(tuples), _OPENFGA_DELETE_BATCH):
                batch = tuples[start : start + _OPENFGA_DELETE_BATCH]
                body = {
                    "authorization_model_id": settings.openfga_model_id,
                    "deletes": {
                        "tuple_keys": [
                            {"user": u, "relation": r, "object": o}
                            for (u, r, o) in batch
                        ]
                    },
                }
                resp = await client.post(url, json=body)
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    if "does not exist" in exc.response.text.lower():
                        log.info("openfga_delete_absent", count=len(batch))
                        continue
                    log.warning(
                        "openfga_delete_rejected",
                        upstream_status=exc.response.status_code,
                        detail=exc.response.text[:500],
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="authorization service unavailable",
                    ) from exc
    except httpx.HTTPError as exc:
        log.warning(
            "openfga_unreachable",
            op="delete",
            error=exc.__class__.__name__,
            detail=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service unavailable",
        ) from exc


async def _openfga_list_objects(
    user_id: UUID, relation: str, object_type: str
) -> list[str]:
    """Enumerate the object ids of `object_type` on which `user:<id>` has `relation`
    (POST /list-objects). The "Shared with me" path (Talus 21): which web_maps a user may
    `can_view`. Returns BARE ids (the "web_map:" prefix stripped).

    Fails CLOSED like the other reads (transport/HTTP error -> 503). Callers that merely
    DISPLAY a list (not gate access) may catch the 503 and degrade to an empty set, since
    an empty list shows LESS access, never more — but the helper itself never silently
    returns [] on error.
    """
    if not settings.openfga_store_id or not settings.openfga_model_id:
        log.error("openfga_unconfigured", store=bool(settings.openfga_store_id))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service not configured",
        )
    url = f"{settings.openfga_api_url}/stores/{settings.openfga_store_id}/list-objects"
    body = {
        "authorization_model_id": settings.openfga_model_id,
        "type": object_type,
        "relation": relation,
        "user": f"user:{user_id}",
    }
    prefix = f"{object_type}:"
    try:
        async with httpx.AsyncClient(timeout=_OPENFGA_TIMEOUT) as client:
            resp = await client.post(url, json=body)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning(
            "openfga_unreachable",
            op="list_objects",
            relation=relation,
            object_type=object_type,
            error=exc.__class__.__name__,
            detail=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization service unavailable",
        ) from exc
    objects = resp.json().get("objects", []) or []
    return [o[len(prefix):] for o in objects if o.startswith(prefix)]


# --- general per-object SHARE seam (Talus 21, F6) -------------------------------
# Object-type-agnostic get/set of the AGO sharing tiers on ANY object ref ("web_map:<id>",
# and "layer:<id>" when the layer-share UI reuses this). The MANAGED tiers are the
# org/group shares; `owner` is never touched (it's not a share), and `public_shared` is
# left untouched by this slice (Talus 21 never writes it — "Public Access" is its own
# slice), so an existing public tuple is preserved rather than clobbered.

# The single organization object. One org today (single-tenant deploy); when multi-org
# ever matters this becomes a parameter. "Organization Only" = a share to this object.
_ORG_OBJECT = "org:talus"
_GROUP_PREFIX = "group:"
# Share-tier relations this seam manages. public_shared is modeled but deliberately NOT
# managed here (see above).
_MANAGED_SHARE_RELATIONS = frozenset({"org_shared", "shared_group"})

# Reserved SYSTEM groups whose membership REQUIRES the item be public (Talus 33 §F8). Curating into
# Open Data is an inherently public act — the public catalog enumerates this group, so an item in it
# MUST be public. The generic share endpoints enforce that (a request adding `open_data` must also
# turn Public Access ON), so the anon catalog can treat group membership as a public guarantee.
#
# `home` is deliberately NOT here: a Home-featured item may be org-only OR public.
# The featured gallery already filters per-viewer (anon -> public_shared; staff -> can_view), so an
# org-only featured map reaches staff on their home but never the public landing page (zero leak) —
# preserving "pin this important map org-wide for staff" alongside genuinely-public featuring.
PUBLIC_SYSTEM_GROUPS = frozenset({"open_data"})

# The Open Data catalog group ALSO requires COMPLETE T31 metadata on anything shared into it (Talus
# 33): the public catalog enumerates it, and "open data without metadata is bad open data". Enforced
# wherever an item enters the group — the dedicated curate endpoint AND the generic share path — so
# the invariant "everything in the catalog is documented" holds no matter how the tuple is written.
# (`home`, the other public system group, has NO metadata gate — featuring isn't publishing data.)
OPEN_DATA_GROUP = "open_data"


_USER_PREFIX = "user:"


async def get_object_shares_and_owner(object_ref: str) -> tuple[dict[str, Any], str | None]:
    """Current share state of `object_ref` AND its owner user-id, from ONE object-tuple read
    (fail-closed 503). Shares = {organizationOnly, groups: [slug...], publicAccess}; owner = the
    `owner` relation's subject uuid (`user:<uuid>` -> `<uuid>`), or None for an ownerless object
    (the seeded/system layers). Lets a per-row caller (GET /me/layers) get the share badge AND the
    owner attribution in a single round-trip. (The `user:*` public wildcard has relation
    public_shared, never owner, so it can't be mistaken for an owner.)"""
    tuples = await _openfga_read_object_tuples(object_ref)
    organization_only = any(r == "org_shared" for (_u, r, _o) in tuples)
    groups = sorted(
        u[len(_GROUP_PREFIX):]
        for (u, r, _o) in tuples
        if r == "shared_group" and u.startswith(_GROUP_PREFIX)
    )
    public_access = any(r == "public_shared" for (_u, r, _o) in tuples)
    owner = next(
        (u[len(_USER_PREFIX):] for (u, r, _o) in tuples if r == "owner" and u.startswith(_USER_PREFIX)),
        None,
    )
    shares = {"organizationOnly": organization_only, "groups": groups, "publicAccess": public_access}
    return shares, owner


async def get_object_shares(object_ref: str) -> dict[str, Any]:
    """Current share state of `object_ref`: which AGO tiers grant view. Returns
    {organizationOnly: bool, groups: [slug...], publicAccess: bool}. Reads ALL tuples on
    the object (fail-closed 503) and classifies by relation; `owner` is ignored."""
    shares, _owner = await get_object_shares_and_owner(object_ref)
    return shares


async def set_object_shares(
    object_ref: str, *, organization_only: bool, groups: list[str]
) -> None:
    """Replace the MANAGED (org + group) share set of `object_ref` to exactly the desired
    state — write the additions, delete the removals, and NEVER touch `owner` or
    `public_shared`. Diff-based so unchanged tiers cause no write churn. Each helper fails
    CLOSED (503); a partial failure leaves the object over-shared at worst the prior state,
    never silently under-reporting success.

    Caller is responsible for authorization (owner-or-admin) and for validating that the
    requested groups exist and are ones the caller may share to (member-scoping)."""
    desired: set[tuple[str, str, str]] = set()
    if organization_only:
        desired.add((_ORG_OBJECT, "org_shared", object_ref))
    for slug in groups:
        desired.add((f"{_GROUP_PREFIX}{slug}", "shared_group", object_ref))

    current = await _openfga_read_object_tuples(object_ref)
    current_managed = {
        (u, r, o) for (u, r, o) in current if r in _MANAGED_SHARE_RELATIONS
    }
    to_add = sorted(desired - current_managed)
    to_delete = sorted(current_managed - desired)
    if to_add:
        await _openfga_write(to_add)
    if to_delete:
        await _openfga_delete(to_delete)


# --- "Public Access" (anonymous) write — an INDEPENDENT per-object tier (Talus 23) -------------
# Deliberately NOT folded into set_object_shares / _MANAGED_SHARE_RELATIONS: a public write must
# touch ONLY the public_shared tuple (never org_shared/shared_group/owner), so editing org/group
# sharing can never clobber public and vice-versa, and a layer's existing org/group shares survive
# a public toggle. This is the F5 "independent per object" model.

_PUBLIC_USER = "user:*"  # the typed public wildcard the model defines: public_shared: [user:*]


async def set_object_public(object_ref: str, *, public: bool) -> bool:
    """Set or clear ONLY the `public_shared` tuple on `object_ref` (`user:* public_shared <ref>`),
    leaving owner / org_shared / shared_group untouched. Idempotent; returns True iff the public
    state actually changed (so the caller can audit a transition). Each OpenFGA call fails CLOSED
    (503) — never a silent success. Un-publish (public=False) deletes any existing public tuple."""
    current = await _openfga_read_object_tuples(object_ref)
    public_tuples = [(u, r, o) for (u, r, o) in current if r == "public_shared"]
    if public and not public_tuples:
        await _openfga_write([(_PUBLIC_USER, "public_shared", object_ref)])
        return True
    if not public and public_tuples:
        await _openfga_delete(public_tuples)
        return True
    return False


async def transfer_object_owner(object_ref: str, new_owner_id: str) -> str | None:
    """Move the OpenFGA `owner` relation on `object_ref` to `user:<new_owner_id>` (Talus 26).
    Returns the PREVIOUS owner uuid (or None if it was ownerless). Touches ONLY the `owner` tuple —
    never org_shared / shared_group / public_shared, so an item's shares survive a transfer. Each
    OpenFGA call fails CLOSED (503).

    FAIL-SAFE ordering: write the NEW owner FIRST, then drop the old — so a mid-failure leaves the
    object OWNED (by both, briefly; harmless + admin-recoverable) rather than ownerless and
    unmanageable. Idempotent: transferring to the current owner is a no-op."""
    current = await _openfga_read_object_tuples(object_ref)
    old_owner_tuples = [(u, r, o) for (u, r, o) in current if r == "owner"]
    new_tuple = (f"{_USER_PREFIX}{new_owner_id}", "owner", object_ref)
    if new_tuple not in old_owner_tuples:
        await _openfga_write([new_tuple])
    stale = [t for t in old_owner_tuples if t != new_tuple]
    if stale:
        await _openfga_delete(stale)
    return next(
        (u[len(_USER_PREFIX):] for (u, _r, _o) in old_owner_tuples if u.startswith(_USER_PREFIX)),
        None,
    )


async def assert_may_share_public(
    conn: AsyncConnection, claims: dict[str, Any] | None
) -> None:
    """Gate a request that TURNS public sharing ON. Raises 403 if public access is disabled for
    the deployment (the F6 master switch) OR a non-admin attempts it while org policy forbids it.
    Admin always passes the policy gate. FAIL-SAFE: any policy-read error denies (restrictive).

    Call ONLY when public=True is being requested. Turning public OFF (un-publish) only REDUCES
    exposure, so it stays allowed for any owner/admin and must NOT be routed through this gate."""
    if not await public_sharing_enabled(conn):
        log.info("authz_rejected", capability="share:public", reason="public_access_disabled")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="public sharing is turned off for this organization",
        )
    if _has_admin_role(claims):
        return
    # Non-admin: the org policy (nonadmin_can_share_public) must allow it. Lazy import keeps
    # org_settings a leaf module (no import cycle). Any read failure => deny (fail-safe).
    from .org_settings import read_org_settings

    try:
        allowed = (await read_org_settings(conn)).nonadminCanSharePublic
    except Exception as exc:  # noqa: BLE001 — fail-safe: ANY failure denies
        log.warning("share_public_policy_read_failed", detail=str(exc))
        allowed = False
    if not allowed:
        log.info("authz_rejected", capability="share:public", reason="org_policy_forbids_nonadmin")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="organization policy does not allow non-admins to share publicly",
        )


class OpenFGABootError(RuntimeError):
    """Raised at startup when the configured OpenFGA store cannot be confirmed live.

    Propagating this out of the FastAPI lifespan aborts startup — the app refuses to
    serve rather than silently 503-ing on the first gated request. Fail-closed; there
    is deliberately no env flag to bypass it.
    """


# Shared remedy for every boot-guard failure: the .env store/model ids are written by
# load-openfga-model.sh and go stale on a `down -v` (the openfga volume is wiped).
_OPENFGA_BOOT_REMEDY = (
    "re-run scripts/load-openfga-model.sh after `docker compose down -v`, "
    "then recreate the api container: docker compose up -d --force-recreate api"
)


async def _probe_openfga_resource(
    url: str, *, kind: str, ident: str, absent_message: str
) -> None:
    """Shared boot probe: GET `url` (200 = the resource exists, 404 = it does not),
    under the SHARED bounded-retry budget (_BOOT_OPENFGA_RETRIES / _RETRY_SLEEP). `kind`
    is 'store' or 'model' and drives the log event names so a boot failure names WHICH
    id is stale (openfga_store_* vs openfga_model_*).

    Returns on 200. Raises OpenFGABootError on 404 (authoritative — NOT retried) with
    `absent_message`, or after the retry budget is exhausted (unreachable).
    """
    noun = "store" if kind == "store" else "authorization model"
    last_error: str | None = None
    for attempt in range(1, _BOOT_OPENFGA_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_OPENFGA_TIMEOUT) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            # Transient: OpenFGA not up yet / connection refused. Bounded retry.
            last_error = f"{exc.__class__.__name__}: {exc}"
            log.warning(
                "openfga_unreachable_at_boot",
                kind=kind,
                ident=ident,
                attempt=attempt,
                max_attempts=_BOOT_OPENFGA_RETRIES,
                error=exc.__class__.__name__,
                detail=str(exc),
            )
            if attempt < _BOOT_OPENFGA_RETRIES:
                await asyncio.sleep(_BOOT_OPENFGA_RETRY_SLEEP)
            continue

        if resp.status_code == 200:
            log.info(f"openfga_{kind}_confirmed", **{f"{kind}_id": ident})
            return

        if resp.status_code == 404:
            # Absent: a REACHABLE OpenFGA says this resource does not exist — the classic
            # stale-id-after-`down -v` case. 404 is authoritative, so do NOT retry.
            log.error(
                f"openfga_{kind}_absent",
                **{f"{kind}_id": ident},
                remedy=_OPENFGA_BOOT_REMEDY,
            )
            raise OpenFGABootError(absent_message)

        # Reachable but an unexpected status — treat as transient and retry (bounded).
        last_error = f"unexpected HTTP {resp.status_code}"
        log.warning(
            "openfga_resource_check_unexpected_status",
            kind=kind,
            ident=ident,
            attempt=attempt,
            max_attempts=_BOOT_OPENFGA_RETRIES,
            upstream_status=resp.status_code,
        )
        if attempt < _BOOT_OPENFGA_RETRIES:
            await asyncio.sleep(_BOOT_OPENFGA_RETRY_SLEEP)

    # Retries exhausted without a definitive 200/404 — OpenFGA unreachable at boot.
    log.error(
        "openfga_unreachable_at_boot_giving_up",
        kind=kind,
        ident=ident,
        attempts=_BOOT_OPENFGA_RETRIES,
        last_error=last_error,
    )
    raise OpenFGABootError(
        f"OpenFGA unreachable at boot after {_BOOT_OPENFGA_RETRIES} attempts probing "
        f"the {noun} (last error: {last_error}). Ensure the openfga service is healthy, "
        f"then recreate the api container: docker compose up -d --force-recreate api."
    )


async def assert_openfga_store_live() -> None:
    """Boot guard #1: confirm settings.openfga_store_id resolves to a LIVE OpenFGA
    store, or fail startup. Probe: GET {openfga_api_url}/stores/{store_id}.

    Three outcomes:
      - healthy   : 200 -> return; boot continues to the model check.
      - absent    : blank store_id, or 404 -> raise OpenFGABootError (fail-closed).
      - transient : OpenFGA unreachable -> bounded retry, then raise OpenFGABootError.
    """
    store_id = settings.openfga_store_id
    # Absent (misconfigured): a blank id can never resolve — fail now, loudly, without
    # even a round-trip. This is the post-`down -v` "stale .env" trap.
    if not store_id.strip():
        log.error(
            "openfga_store_unconfigured", store_id=store_id, remedy=_OPENFGA_BOOT_REMEDY
        )
        raise OpenFGABootError(
            f"OPENFGA_STORE_ID is empty/blank — cannot confirm a live OpenFGA store. "
            f"Remedy: {_OPENFGA_BOOT_REMEDY}."
        )
    await _probe_openfga_resource(
        f"{settings.openfga_api_url}/stores/{store_id}",
        kind="store",
        ident=store_id,
        absent_message=(
            f"OPENFGA_STORE_ID '{store_id}' does not resolve to a live store "
            f"(OpenFGA returned 404). Remedy: {_OPENFGA_BOOT_REMEDY}."
        ),
    )


async def assert_openfga_model_live() -> None:
    """Boot guard #2: confirm settings.openfga_model_id resolves to a LIVE authorization
    model UNDER the store. MUST run only AFTER assert_openfga_store_live() passes — a
    model check is meaningless if the store is gone. Probe:
    GET {openfga_api_url}/stores/{store_id}/authorization-models/{model_id}.

    Three outcomes:
      - healthy   : 200 -> return; the app boots.
      - absent    : blank model_id, or 404 (store is live but the model did NOT load)
                    -> raise OpenFGABootError (fail-closed).
      - transient : OpenFGA unreachable -> bounded retry, then raise OpenFGABootError.
    """
    store_id = settings.openfga_store_id
    model_id = settings.openfga_model_id
    if not model_id.strip():
        log.error(
            "openfga_model_unconfigured", model_id=model_id, remedy=_OPENFGA_BOOT_REMEDY
        )
        raise OpenFGABootError(
            f"OPENFGA_MODEL_ID is empty/blank — cannot confirm a live authorization "
            f"model. Remedy: {_OPENFGA_BOOT_REMEDY}."
        )
    await _probe_openfga_resource(
        f"{settings.openfga_api_url}/stores/{store_id}/authorization-models/{model_id}",
        kind="model",
        ident=model_id,
        absent_message=(
            f"OPENFGA_MODEL_ID '{model_id}' does not resolve to a live authorization "
            f"model under store '{store_id}' (OpenFGA returned 404) — the store is live "
            f"but the model did not load. Remedy: {_OPENFGA_BOOT_REMEDY}."
        ),
    )


async def _layer_editable(conn: AsyncConnection, layer_id: str) -> bool | None:
    """platform.layers.editable for `layer_id`; None if the layer isn't registered."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT editable FROM platform.layers WHERE layer_id = %s", (layer_id,)
        )
        row = await cur.fetchone()
    return None if row is None else bool(row[0])


async def _layer_editing_enabled(conn: AsyncConnection, layer_id: str) -> bool:
    """platform.layers.editing_enabled for `layer_id` — the SOFT owner on/off switch
    (decision-editing-toggle.md), distinct from the hard `editable` gate above. Defaults true (V057);
    a missing row reads False (fail-closed — though edit_allowed's `editable` gate rejects an
    unregistered layer before this is reached)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT editing_enabled FROM platform.layers WHERE layer_id = %s", (layer_id,)
        )
        row = await cur.fetchone()
    return False if row is None else bool(row[0])


# Per-layer edit capabilities (V109, decision-layer-edit-capabilities.md §3.2): the op a write door
# performs, matched to the owner's four flags. `EDIT_OPS` is the closed vocabulary; a door names one
# (or several — an update carrying geometry AND props needs both).
EDIT_OPS = ("add", "attributes", "geometry", "delete")  # column order in layer_edit_ops' SELECT
# The user-facing sentence per refused op — the doors put it in the 403 detail so the web editor and
# Talus Field (which holds the queued op with it) show the policy, not "not authorized".
EDIT_OP_DENIED = {
    "add": "This layer does not allow adding features.",
    "attributes": "This layer does not allow editing attributes.",
    "geometry": "This layer does not allow moving or reshaping features.",
    "delete": "This layer does not allow deleting features.",
}


def edit_deny_sentence(reason: str) -> str | None:
    """The friendly sentence for an `op_not_allowed:<op>` reason, else None (the caller keeps its own
    wording for the other reasons)."""
    if reason.startswith("op_not_allowed:"):
        return EDIT_OP_DENIED.get(reason.split(":", 1)[1])
    return None


async def layer_edit_ops(conn: AsyncConnection, layer_id: str) -> dict[str, bool]:
    """The layer's four capability flags as {op: allowed}. A missing row reads all-False (fail-closed —
    though edit_allowed's `editable` gate rejects an unregistered layer before this is reached)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT edit_add, edit_attributes, edit_geometry, edit_delete "
            "FROM platform.layers WHERE layer_id = %s",
            (layer_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return {op: False for op in EDIT_OPS}
    return {op: bool(v) for op, v in zip(EDIT_OPS, row)}


async def _refused_edit_op(
    conn: AsyncConnection, layer_id: str, op: str | tuple[str, ...]
) -> str | None:
    """The first of `op` (one op, or several) the layer's flags refuse — None when all are allowed.
    An unknown op name is a programming error, refused loudly rather than silently allowed."""
    ops = (op,) if isinstance(op, str) else tuple(op)
    for o in ops:
        if o not in EDIT_OPS:
            raise ValueError(f"unknown edit op {o!r}")
    flags = await layer_edit_ops(conn, layer_id)
    for o in ops:
        if not flags[o]:
            return o
    return None


# --- the two access decisions both /me/layers and require_capability call --------


async def view_allowed(user: User, layer_id: str) -> bool:
    """The view key: OpenFGA can_view, OR talus-admin god-mode (admins READ every layer; User.is_admin is set
    at auth from the token). Stub mode authorizes everything (dev-only). Every layer read gate (tiles, features,
    export, metadata, symbology) funnels through here, so this one override grants admins access everywhere.
    NOTE: because of the admin override, a genuine "shared with me" signal MUST use `is_shared` below."""
    if settings.auth_mode == "stub":
        return True
    if user.is_admin:
        return True
    return await _openfga_check(user.id, _VIEW, layer_id)


async def is_shared(user: User, layer_id: str) -> bool:
    """Genuine can_view via a real owner/share relation — NO admin override (unlike view_allowed). The exact
    "shared with me" signal for the catalog/add-layers scope (an admin's admin-override-only layers are NOT
    shared with them). Stub authorizes (dev). Owned layers also return true (owner ⇒ can_view), so callers
    AND with `not owner` to get the pure shared-not-owned bucket."""
    if settings.auth_mode == "stub":
        return True
    return await _openfga_check(user.id, _VIEW, layer_id)


async def edit_allowed(
    conn: AsyncConnection,
    claims: dict[str, Any] | None,
    user: User,
    layer_id: str,
    op: str | tuple[str, ...] | None = None,
) -> tuple[bool, str]:
    """The edit decision. Returns (allowed, reason) — reason is logged AND surfaced (the UI shows a
    "editing is restricted but you have admin privileges" notice when reason == 'admin').

    GENUINE edit = editable (DB) AND editing_enabled (DB, the owner switch) AND data-editor role (token) AND
    can_edit_access (OpenFGA). An admin who lacks genuine access — or for whom editing is switched off — may
    STILL edit any *editable* layer (admin-edit-all, 2026-06-30) — reason 'admin' so the UI flags it. The
    `editable` gate is NEVER bypassed (a non-editable layer / view has no editable table, so even an admin
    can't edit it); the `editing_enabled` gate IS admin-overridable (decision-editing-toggle.md) — when off,
    a non-admin is denied with reason 'editing_disabled' (incl. the owner — "off means off").

    `op` (V109, decision-layer-edit-capabilities.md §3.2): the operation a WRITE door is about to perform —
    one of EDIT_OPS, or several when one request needs more than one (a PATCH carrying geometry and props).
    Checked AFTER the master switch and BEFORE the role/access keys; a flag that is off refuses with
    'op_not_allowed:<op>' for EVERYONE, admins included (F1 — the flags are the owner's data policy, like
    `editable`, not an availability switch like `editing_enabled`). None = "may this caller edit the layer
    at all" (the capability readers: /me/layers, the field door, the record-detail flags).
    """
    if settings.auth_mode == "stub":
        return True, "stub"
    editable = await _layer_editable(conn, layer_id)
    if not editable:
        return False, "layer_not_editable"
    # The SOFT owner switch (decision-editing-toggle.md): when off, the genuine `ok` path fails for
    # EVERYONE (including the owner), but the admin override below still applies.
    editing_enabled = await _layer_editing_enabled(conn, layer_id)
    if not editing_enabled and not user.is_admin:
        return False, "editing_disabled"  # owner turned editing off; distinct from a lack of access
    if op is not None:
        refused = await _refused_edit_op(conn, layer_id, op)
        if refused is not None:
            return False, f"op_not_allowed:{refused}"  # policy — no admin override (F1)
    has_role = _has_edit_role(claims)
    has_access = await _openfga_check(user.id, _EDIT_ACCESS, layer_id)
    if editing_enabled and has_role and has_access:
        return True, "ok"  # genuine edit access — no notice
    if user.is_admin:
        return True, "admin"  # restricted / editing-off layer, but admin override → UI shows the notice
    return False, "missing_edit_role" if not has_role else "no_edit_access"


async def is_owner(user: User, layer_id: str) -> bool:
    """The OWNER signal: does the user hold the OpenFGA `owner` relation on the layer?

    This is the exact "Owned by me" signal the catalog scope filter needs (Talus 18) —
    distinct from canManage, which ALSO grants to admins, so an admin viewing a layer they
    do not own has canManage:true but is_owner:false. Stub mode authorizes (dev-only),
    mirroring view_allowed/manage_allowed.
    """
    if settings.auth_mode == "stub":
        return True
    return await _openfga_check(user.id, "owner", layer_id)


async def manage_allowed(
    claims: dict[str, Any] | None,
    user: User,
    layer_id: str,
    *,
    owner: bool | None = None,
) -> bool:
    """The MANAGE key (set/clear the layer-default symbology): talus-admin OR the OpenFGA
    `owner` of the layer — the same decision release-layer and the renderer endpoints
    enforce. Distinct from edit_allowed (a shared editor edits FEATURES but must not
    restyle the layer for every viewer). Admin first so it short-circuits the FGA call and
    so bespoke layers (no owner tuple) are admin-only. Stub mode authorizes (dev-only).

    `owner` may be supplied by a caller that already resolved is_owner() for this
    (user, layer) — e.g. GET /me/layers surfaces `owner` per layer — so we reuse it
    instead of a second OpenFGA round-trip. None means "not precomputed; check now."
    """
    if settings.auth_mode == "stub":
        return True
    if _has_admin_role(claims):
        return True
    if owner is not None:
        return owner
    return await is_owner(user, layer_id)


# --- the dependency the routers use ---------------------------------------------


# --- anonymous public-read seam (Talus 23) --------------------------------------
# The ONLY authorization path open to anonymous callers. It checks the `public_shared` relation
# DIRECTLY (never can_view), so an anon caller can pass ONLY via public_shared — never via
# owner/org_shared/shared_group. Fail-closed by construction.

# public_shared is modeled as [user:*], so an OpenFGA Check for ANY concrete user returns allowed
# iff the public tuple exists. The anonymous caller has no real identity, so we probe with this
# fixed sentinel — its identity is irrelevant (it holds no other tuples; it can match ONLY the
# public wildcard).
_ANON_PROBE_USER = UUID("00000000-0000-0000-0000-0000000000a0")


async def public_object_allowed(
    conn: AsyncConnection, object_id: str, *, object_type: str
) -> bool:
    """Does `<object_type>:<object_id>` grant anonymous (public) view? A DIRECT public_shared
    check — the heart of the anon seam (decision §F1). Stub mode authorizes (dev). Fails closed
    (503) on any OpenFGA error — never allow on error.

    F6 master switch (Talus 23 §F6, enforced here Talus 33): this is the SINGLE chokepoint every
    anonymous read funnels through (features / columns / export / tiles / metadata-download), so
    gating it here makes the WHOLE anon read path inert when public sharing is off — an item that
    still carries a `public_shared` tuple (published while the switch was on, then turned off) is
    denied, matching the decision doc's "OFF ⇒ token-less ⇒ deny". The Open Data discovery
    endpoints gate the switch themselves; this closes the raw data path.

    `conn` is the caller's own request connection, and it is a REQUIREMENT rather than a
    convenience: since V092 the switch is `COALESCE(org_settings.public_sharing_enabled,
    PUBLIC_ANON_ACCESS_ENABLED)` (decision-public-sharing-reachable.md F2), resolved per call so an
    admin turning it off stops serving already-public items immediately, with no restart and no
    cache window. Resolving from the request's connection — rather than reaching into the pool —
    keeps a request from holding one pool slot while waiting for a second."""
    if settings.auth_mode == "stub":
        return True
    if not await public_sharing_enabled(conn):
        return False
    return await _openfga_check(
        _ANON_PROBE_USER, "public_shared", object_id, object_type=object_type
    )


def require_view_or_public(layer: str):
    """FastAPI dependency for the public read+tile surface. An AUTHENTICATED caller passes via
    normal view access (can_view); an ANONYMOUS caller passes ONLY if the layer is public_shared.
    Returns the principal (User | None). Denies an authed-without-access with 403 (mirrors
    require_capability); denies anon-on-non-public with 404 (existence-hiding — don't reveal a
    private layer exists). Authenticated behavior is unchanged from require_capability(view)."""

    async def _dependency(
        conn: AsyncConnection = Depends(get_db),
        principal: User | None = Depends(get_optional_user),
    ) -> User | None:
        # `conn` rides in so the anon branch can resolve the public-sharing switch (V092). FastAPI
        # caches get_db per request, so a route body that also takes get_db shares this connection.
        #
        # The tile routes' bodies take NO conn, so on them this connection IS held across the
        # upstream proxy fetch (~tens of ms typical; _UPSTREAM_TIMEOUT 10s worst case). That hold
        # is a KNOWN, ACCEPTED cost: under burst the pool queues briefly — it does not fail. The
        # obvious optimization (borrow a slot via get_pool() for just this check and release it
        # before the fetch) was BUILT AND DISPROVEN 2026-08-25: under a 20-40-way anon burst it
        # leaked every checked-out connection and froze the pool into 30s PoolTimeouts. The full
        # account lives on db.get_pool's docstring — do not reintroduce it.
        if principal is not None:
            if not await view_allowed(principal, layer):
                log.info(
                    "authz_rejected", capability=f"{layer}:view",
                    reason="no_view_access", user_id=str(principal.id),
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"not authorized to view {layer}",
                )
            return principal
        # anonymous: ONLY public_shared layers are reachable
        if not await public_object_allowed(conn, layer, object_type="layer"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        return None

    return _dependency


def require_capability(layer: str, action: str):
    """FastAPI dependency factory. `action` is "view"/"read" (access only) or "write"
    (the full three keys). The dependency still returns the `User`, so downstream
    `actor = user.id` and the audit-GUC/structlog code is untouched.
    """
    if action not in ("view", "read", "write"):
        raise ValueError(f"unknown action {action!r}")

    async def _dependency(
        conn: AsyncConnection = Depends(get_db),
        claims: dict[str, Any] | None = Depends(_validated_claims),
        user: User = Depends(get_current_user),
    ) -> User:
        if action == "write":
            allowed, reason = await edit_allowed(conn, claims, user, layer)
            if not allowed:
                log.info(
                    "authz_rejected",
                    capability=f"{layer}:write",
                    reason=reason,
                    user_id=str(user.id),
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"not authorized to edit {layer}",
                )
        else:  # view / read
            if not await view_allowed(user, layer):
                log.info(
                    "authz_rejected",
                    capability=f"{layer}:view",
                    reason="no_view_access",
                    user_id=str(user.id),
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"not authorized to view {layer}",
                )
        return user

    return _dependency
