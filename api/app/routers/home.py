"""Talus 30 — public home page endpoints.

  - GET /public/config — the anonymous bootstrap: whether this deployment offers a public face
    (PUBLIC_ANON_ACCESS_ENABLED, F6) + the admin-set home text. Drives the SPA's anon shell + hero.
    NO auth (it's the thing an anonymous visitor reads first; returns only non-sensitive org config).
  - GET /home/featured — the featured gallery: maps/layers shared to the reserved 'home' group,
    FILTERED to what the caller may see (anon -> public_shared only; signed-in -> can_view). Optional-
    auth, so the one endpoint serves both the public landing and signed-in staff. Fail-soft: an OpenFGA
    error degrades to an empty gallery (less content, never a leak).
"""
import structlog
from fastapi import APIRouter, Depends, HTTPException
from psycopg import AsyncConnection
from pydantic import BaseModel

from ..auth import get_optional_user
from ..authz import _objects_shared_with_group, _openfga_check, get_object_shares_and_owner
from ..db import get_db
from ..models.user import User
from ..org_settings import OrgHome, read_org_home
from ..public_sharing import public_sharing_enabled

log = structlog.get_logger(__name__)

router = APIRouter(tags=["home"])

_HOME_GROUP = "home"


class PublicConfig(BaseModel):
    publicAnonAccessEnabled: bool
    home: OrgHome


class FeaturedItem(BaseModel):
    itemType: str  # 'web_map' | 'layer'
    itemId: str
    title: str
    description: str | None
    ownerName: str | None


@router.get("/public/config", summary="Anonymous bootstrap: public-access flag + home text")
async def public_config(conn: AsyncConnection = Depends(get_db)) -> PublicConfig:
    # The RESOLVED switch (V092): the org override when an admin has set it, else the deployment
    # value. Same field, same meaning to the client ("does this deployment offer a public face") —
    # only the source moved, from an env var no admin could reach to a setting they can.
    return PublicConfig(
        publicAnonAccessEnabled=await public_sharing_enabled(conn),
        home=await read_org_home(conn),
    )


@router.get("/home/featured", summary="Featured maps & layers the caller may see")
async def home_featured(
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> dict[str, list[FeaturedItem]]:
    # F6 gate: an ANON caller gets nothing when this deployment hasn't enabled the public face — the
    # same switch that gates the rest of the anon surface (Talus 23). Without this, an anonymous
    # curl could enumerate public-featured items even with public access turned OFF. Signed-in users
    # are unaffected (the switch gates anon only; featuring is an internal capability too).
    if principal is None and not await public_sharing_enabled(conn):
        return {"items": []}

    # Enumerate featured items (shared to group:home). Fail-SOFT: OpenFGA down -> empty gallery.
    try:
        map_ids = await _objects_shared_with_group(_HOME_GROUP, "web_map")
        layer_ids = await _objects_shared_with_group(_HOME_GROUP, "layer")
    except HTTPException:
        log.warning("home_featured_enumerate_failed")
        return {"items": []}

    # Titles/descriptions from the registry tables (maps carry a description; layers don't).
    titles: dict[str, tuple[str, str | None]] = {}
    async with conn.cursor() as cur:
        if map_ids:
            await cur.execute(
                "SELECT id::text, title, description FROM platform.web_maps WHERE id = ANY(%s)",
                (map_ids,),
            )
            for mid, title, desc in await cur.fetchall():
                titles[f"web_map:{mid}"] = (title, desc)
        if layer_ids:
            await cur.execute(
                "SELECT layer_id, COALESCE(display_name, layer_id) FROM platform.layers "
                "WHERE layer_id = ANY(%s)",
                (layer_ids,),
            )
            for lid, title in await cur.fetchall():
                titles[f"layer:{lid}"] = (title, None)

    staged: list[tuple[str, str, str, str | None, str | None]] = []  # type,id,title,desc,owner
    owner_ids: set[str] = set()
    for item_type, ids in (("web_map", map_ids), ("layer", layer_ids)):
        for item_id in ids:
            ref = f"{item_type}:{item_id}"
            if ref not in titles:
                continue  # registry row gone — skip a dangling tuple
            title, desc = titles[ref]
            # Visibility filter — fail-soft per item: anon sees public only; signed-in sees can_view.
            try:
                shares, owner = await get_object_shares_and_owner(ref)
                if principal is None:
                    visible = bool(shares["publicAccess"])
                else:
                    visible = await _openfga_check(
                        principal.id, "can_view", item_id, object_type=item_type
                    )
            except HTTPException:
                continue  # one item's check failed — skip it, keep the gallery
            if not visible:
                continue
            if owner:
                owner_ids.add(owner)
            staged.append((item_type, item_id, title, desc, owner))

    names: dict[str, str] = {}
    if owner_ids:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id::text, COALESCE(display_name, username, email) FROM platform.users "
                "WHERE id = ANY(%s)",
                (list(owner_ids),),
            )
            names = {r[0]: r[1] for r in await cur.fetchall()}

    items = [
        FeaturedItem(
            itemType=t, itemId=i, title=ti, description=d,
            ownerName=names.get(o) if o else None,
        )
        for (t, i, ti, d, o) in staged
    ]
    return {"items": items}
