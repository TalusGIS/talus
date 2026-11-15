"""Vector-tile proxy.

MapLibre requests MVT from the api (not pg_tileserv directly). The api validates
the Keycloak token with the same dependency as the feature routes, then streams the
tile from pg_tileserv over the internal Docker network. pg_tileserv has no host
port and no healthcheck, so a clearly-logged 502 here is the visible signal that
it is unreachable.

One proxy body, one generic route: `_proxy_tile(layer, …)` holds the shared
fetch/204-passthrough/502 logic, and `/tiles/{slot}/...` serves every hosted layer
through the pooled `assets.slot_mvt` function — the slot token rides the path,
regex-gated to the frozen `tal_<8hex>` shape. (The per-built-in thin routes retired
with the pilot built-ins; views and filtered tiles ride tiles_dyn.py instead.)
"""
import re

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from psycopg import AsyncConnection

from ..auth import get_optional_user
from ..authz import public_object_allowed, require_view_or_public, view_allowed
from ..config import settings
from ..db import get_db
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["tiles"])

# Publisher slot pool: ONE generic route serves every slot's tile via the pooled
# assets.slot_mvt function (V061; slots from V019 + V056 + any future expansion).
# The slot token is NOT a hardcoded constant here — it comes from the path — so it is regex-validated to the
# frozen tal_<8hex> shape before it ever names an assets.<token>_tile function (identical token discipline to
# layer_features.py). A per-slot hardcoded route does not scale to a migration-expandable pool — the V056
# expansion is exactly what exposed the gap (its slots had tables + tile fns but no route → 404).
_SLOT_RE = re.compile(r"^tal_[0-9a-f]{8}$")
# Authenticated: the browser may keep a copy but must revalidate every time, so features
# created/moved/deleted via the CRUD routes appear immediately — no stale window.
_TILE_CACHE_CONTROL = "no-cache"
# Anonymous (public) tiles ARE cacheable (Talus 23): public data needs no instant edit-reflection,
# and a short browser/CDN cache sharply cuts anonymous tile load + cost.
_PUBLIC_TILE_CACHE_CONTROL = "public, max-age=60"
_UPSTREAM_TIMEOUT = httpx.Timeout(10.0)


async def _proxy_tile(
    layer: str, z: int, x: int, y: int, *, anon: bool = False, params: dict[str, str] | None = None
) -> Response:
    """Fetch one MVT tile for `layer` from pg_tileserv and return it (or a 204/502).

    `layer` is a fixed module constant per route below — never user input — so it is
    safe to interpolate into the upstream URL. `params` are extra pg_tileserv function
    arguments passed as query params (the generic slot_mvt takes ?layer=<token>; the token
    is regex-proven upstream and httpx encodes it as a bound query value, never a path).
    `anon` selects the cache policy: public/cacheable for an anonymous caller,
    revalidate-always for an authenticated one.
    """
    cache_control = _PUBLIC_TILE_CACHE_CONTROL if anon else _TILE_CACHE_CONTROL
    url = f"{settings.tileserv_url}/{layer}/{z}/{x}/{y}.pbf"
    try:
        async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT) as client:
            upstream = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        # pg_tileserv unreachable/timed out — this 502 stands in for the absent
        # pg_tileserv healthcheck (see docker-compose.yml).
        log.warning(
            "tileserv_unreachable",
            layer=layer,
            z=z,
            x=x,
            y=y,
            error=exc.__class__.__name__,
            detail=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="tile upstream unreachable",
        ) from exc

    # pg_tileserv returns 204 for tiles with no features — pass it through.
    if upstream.status_code == status.HTTP_204_NO_CONTENT:
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": cache_control})

    if upstream.status_code != status.HTTP_200_OK:
        log.warning(
            "tileserv_bad_status",
            layer=layer,
            z=z,
            x=x,
            y=y,
            upstream_status=upstream.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"tile upstream returned {upstream.status_code}",
        )

    return Response(
        content=upstream.content,
        media_type="application/vnd.mapbox-vector-tile",
        headers={"Cache-Control": cache_control},
    )


# The generic slot tile route: the layer id rides the PATH, so an on-demand-created layer tiles
# immediately with NO pg_tileserv restart. It only ever sees tal_ tokens — the regex guard rejects
# anything else with a 404. It used to be defined AFTER three bespoke handlers
# (/tiles/signs|street_lights|county_boundary) so those matched first; those retired with the pilot
# built-ins (decision-retire-builtins.md phase 2) and this is now the only vector-tile route.
# Gating mirrors require_view_or_public but reads the token from the PATH:
# authenticated view (403 on no access), or anonymous iff the layer is public_shared (404
# otherwise — existence-hiding). A fresh layer has its owner tuple before its table exists
# (FGA-first create), so there is no window where tiles leak.


async def _slot_view_or_public(
    slot: str,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> User | None:
    """Authorize a tile request for slot `slot` (from the path). GUARD 1 = regex: only a tal_<8hex>
    token may name an assets.<token>_tile function — never raw user input. Then the same view/public
    check require_view_or_public applies, bound to this dynamic token."""
    if not _SLOT_RE.fullmatch(slot):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if principal is not None:
        if not await view_allowed(principal, slot):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"not authorized to view {slot}"
            )
        return principal
    if not await public_object_allowed(conn, slot, object_type="layer"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return None


@router.get(
    "/tiles/{slot}/{z}/{x}/{y}.pbf",
    summary="Hosted layer MVT tile (proxied from pg_tileserv, Keycloak-gated)",
)
async def slot_tile(
    slot: str,
    z: int,
    x: int,
    y: int,
    principal: User | None = Depends(_slot_view_or_public),
) -> Response:
    # `slot` passed the _SLOT_RE guard in the dependency; it rides as slot_mvt's ?layer= argument
    # (a bound query param), where the function re-validates it (regex + registry) before any SQL.
    return await _proxy_tile("assets.slot_mvt", z, x, y, anon=principal is None, params={"layer": slot})
