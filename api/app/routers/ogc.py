"""OGC API — Features façade (interop I-1, decision-desktop-interop-i1.md).

The desktop/harvester read surface: landing → conformance → collections → items, as a thin
FAÇADE over the exact machinery the portal already uses — there is NO publish step, NO service
registry, NO second permission system (the anchor's §3 reframe). A collection exists because a
viewable spatial layer exists; authz is the same optional-auth seam every read route uses
(Bearer, PAT-as-Basic, or anonymous-public behind the F6 switch), and items delegate to
layer_query.feature_page — the one paged-GeoJSON builder — so filters/scrub/reprojection cannot
drift from /features.

★ TWO FRONT DOORS (decision-desktop-interop-i1.md — the Pro-leg resolution). The same handlers
are mounted at two paths that differ in ONE behavior: whether an unauthenticated request is
CHALLENGED. This exists because ArcGIS Pro (unlike QGIS) never volunteers HTTP Basic — it sends
a bare request and only authenticates after a `401 + WWW-Authenticate: Basic` challenge. A single
optional-auth endpoint therefore can't serve both an anonymous harvester AND a
challenge-driven desktop client: a no-credentials request from each is byte-identical, so the
server must pick one behaviour. So we offer two honest entrances and let the "Use in…" wizard
hand each client the right one — the user never toggles anything:
  - **`/api/v1/ogc` — the PUBLIC door** (challenge=False): never challenges. Anonymous callers get
    public layers (behind the F6 switch); credential-volunteering callers (QGIS, curl, scripts)
    get their full scope. The harvester / Open-Data / lingua-franca surface. Unchanged from I-1.
  - **`/api/v1/ogc-auth` — the SIGN-IN door** (challenge=True): an unauthenticated request gets
    `401 + WWW-Authenticate: Basic` instead of the anonymous view, so a challenge-driven client
    (Pro) authenticates and a credential-volunteering client (QGIS) is unaffected (it never sees
    the challenge). Any signed-in desktop user — Pro OR QGIS — uses this door and always sees
    their FULL scope, even in a mixed public/private catalog.
This is NOT per-layer or per-client config (that would put the data owner in charge of predicting
consumers — a drift trap): it is one behaviour flag on the connection endpoint, wizard-selected.

Client-pickiness rules this file exists to satisfy (the sourced I-1 recon; QGIS 3.44 + Pro 3.7
are the pinned rig):
  - /conformance MUST literally declare BOTH the Core and GeoJSON classes — the actual gate both
    flagship clients check, independent of behavior.
  - Every links[].href is ABSOLUTE (QGIS/GDAL cannot resolve relative hrefs) and rel values are
    exact: `conformance`, `data`, `items`, `self`, `next`. The base comes from the REQUEST's own
    scheme/host AND door path, so links keep a client on the door it dialed (Pro builds every
    sub-request from the returned links — they must not jump doors) and a multi-homed host / caddy
    edge just works.
  - An over-max `limit` CLAMPS to the max (QGIS has shipped hardcoded limit=1000) — never a 422.
  - Items are `application/geo+json`; everything else `application/json`; output is CRS84
    (lon/lat WGS-84 — exactly what feature_page emits), declared and not negotiable in v1.
Spatial layers only (geom_type <> 'none'): OAPIF has no story for geometry-less tables in the
clients we serve; tables stay on /features + exports.
"""
import json
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from psycopg import AsyncConnection, sql

from ..auth import BASIC_CHALLENGE, BASIC_CHALLENGE_NO_VIEW, get_optional_user
from ..authz import is_shared, public_object_allowed
from ..db import get_db
from ..models.user import User
from .layer_query import _authorize_read, _require_registered, feature_page

log = structlog.get_logger(__name__)

_CONF_CORE = "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core"
_CONF_GEOJSON = "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson"
_CRS84 = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
_GEOJSON_MT = "application/geo+json"

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000  # advertised page ceiling; over-asks CLAMP here (never 400/422)

# The two door mount paths (under /api/v1). PUBLIC is the shipped I-1 path; SIGN-IN is the
# challenge variant Pro needs. Both are wizard-selected — see the module docstring.
PUBLIC_MOUNT = "/ogc"
AUTH_MOUNT = "/ogc-auth"


def _base(request: Request, mount: str) -> str:
    """The absolute OGC root for THIS door, built from the address the CLIENT dialed (scheme +
    host of this very request) + the door's mount path. Deriving from the request keeps links on
    the same door the client is using (Pro rebuilds every sub-request from these links) and valid
    on whichever interface / caddy edge hostname served them (the multi-homed-rig lesson)."""
    u = request.url
    return f"{u.scheme}://{u.netloc}/api/v1{mount}"


def _json(payload: dict[str, Any], media_type: str = "application/json") -> Response:
    # jsonable_encoder: feature properties carry datetimes/UUIDs (created_at, edit tracking) that
    # raw json.dumps rejects — encode exactly the way FastAPI's default response path would.
    # no-store: without it, Qt's disk cache (QGIS) heuristically caches these responses — and a
    # single interrupted download leaves a TRUNCATED body that replays on every later request of
    # the same URL (the "Failed to read GeoJSON data" / dead-layer-until-restart syndrome, found
    # live on the 3.44 rig). Desktop clients must always hit the server; it also keeps
    # authenticated payloads off unencrypted disk caches (the tiles no-cache posture).
    # Two QGIS-3.44-proven serialization choices (decision-desktop-interop-i1.md build notes):
    #  - COMPACT separators: machine endpoint, ~7% smaller pages.
    #  - `\\` pairs re-encoded as the \ unicode escape: QGIS's OAPIF page-preprocessor
    #    (removeUselessSpacesFromJSONBuffer) mis-tracks escapes at an escaped backslash ENDING a
    #    string value (`…/\\",` → it corrupts the closing quote) and GDAL then rejects the page
    #    with "Illegal escape sequence (\,)" — byte-proven live against captured /vsimem buffers
    #    (two sign notes ending in a literal backslash killed every signs load at page 2).
    #    \ decodes to the SAME string content, but no bare backslash-pair exists for the
    #    buggy tracker to misread. Left-to-right str.replace pairs 2N backslashes correctly
    #    (JSON output only contains backslashes as escape initiators).
    #  - `\"` re-encoded as the " unicode escape: the SAME QGIS function also emits a stale
    #    backslash in place of every escaped quote — a value `say "hi"` reads back as `say \hi\`,
    #    SILENTLY (no error anywhere; empirically proven with a standalone reproducer,
    #    2026-07-05). " decodes identically and
    #    gives the preprocessor nothing to misread. ORDER MATTERS: after the \ rewrite no
    #    bare `\\` pairs remain, so every remaining `\"` is a genuine escaped in-string quote
    #    (bare `"` is only ever a JSON delimiter).
    return Response(
        content=json.dumps(jsonable_encoder(payload), separators=(",", ":"))
        .replace("\\\\", "\\u005c")
        .replace('\\"', "\\u0022"),
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


async def _authorize_read_or_invite(
    request: Request, conn: AsyncConnection, principal: User | None, layer_id: str
) -> bool:
    """_authorize_read, but a PAT caller denied VIEW gets the 401-INVITE (re-prompt) instead of a
    dead-end 403 (interop I-3). The fix for a wrong token is "use another
    token," and only a challenge re-prompts Pro/QGIS; the realm explains why. Bearer keeps its 403
    (the SPA handles it), anon keeps its 404 (existence-hiding) — both unchanged. PAT-ness is
    `request.state.pat_scope`, set by auth._pat_user. Still fail-closed: no data served."""
    try:
        return await _authorize_read(conn, principal, layer_id)
    except HTTPException as e:
        if e.status_code == status.HTTP_403_FORBIDDEN and \
                getattr(request.state, "pat_scope", None) is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="this access token's account can't see this layer — "
                "sign in with a token whose account has access",
                headers={"WWW-Authenticate": BASIC_CHALLENGE_NO_VIEW},
            ) from e
        raise


async def _viewable_spatial_layers(
    conn: AsyncConnection, principal: User | None
) -> list[tuple[str, str | None]]:
    """(layer_id, display_name) for every spatial layer THIS caller may see — the same scoping the
    portal uses: authenticated = genuine share (is_shared) or admin god-mode; anonymous =
    public_shared behind the F6 master switch (fail-soft per layer: an OpenFGA hiccup omits the
    layer rather than failing the whole catalog — the /me/layers badge posture)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT layer_id, display_name FROM platform.layers "
            "WHERE geom_type <> 'none' AND status = 'ready' ORDER BY display_name NULLS LAST, layer_id"
        )
        rows = await cur.fetchall()
    out: list[tuple[str, str | None]] = []
    for layer_id, display_name in rows:
        try:
            if principal is not None:
                if await is_shared(principal, layer_id) or principal.is_admin:
                    out.append((layer_id, display_name))
            elif await public_object_allowed(conn, layer_id, object_type="layer"):
                out.append((layer_id, display_name))
        except HTTPException:
            continue  # fail-soft: omit, never break the whole listing
    return out


async def _extent_bbox(conn: AsyncConnection, layer_id: str) -> list[float] | None:
    """[w, s, e, n] in WGS-84 (the layer_extent core) — populated extents are what make Pro/QGIS
    zoom sensibly instead of pulling the world (recon §1.2). None when empty."""
    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL(
                "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM "
                "(SELECT ST_Extent(ST_Transform(geom, 4326)) AS e FROM {tbl}) s WHERE e IS NOT NULL"
            ).format(tbl=sql.Identifier("assets", layer_id))
        )
        row = await cur.fetchone()
    return [row[0], row[1], row[2], row[3]] if row else None


def _collection_doc(base: str, layer_id: str, title: str | None,
                    bbox: list[float] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": layer_id,
        "title": title or layer_id,
        "itemType": "feature",
        "crs": [_CRS84],
        "links": [
            {"href": f"{base}/collections/{layer_id}", "rel": "self",
             "type": "application/json", "title": "collection metadata"},
            {"href": f"{base}/collections/{layer_id}/items", "rel": "items",
             "type": _GEOJSON_MT, "title": "features"},
        ],
    }
    if bbox is not None:
        doc["extent"] = {"spatial": {"bbox": [bbox], "crs": _CRS84}}
    return doc


def _build_ogc_router(mount: str, *, challenge: bool) -> APIRouter:
    """Build one OGC door. `mount` is its path under /api/v1; `challenge` makes an unauthenticated
    request answer `401 + WWW-Authenticate: Basic` (the SIGN-IN door) instead of the anonymous view
    (the PUBLIC door). Handlers are identical across doors — only the guard + link base differ, so
    behaviour can't drift between them. The sign-in door is hidden from the OpenAPI schema (same
    contract, different auth posture) — the public door documents it."""
    router = APIRouter(tags=["ogc"])
    in_schema = not challenge

    def guard(principal: User | None) -> None:
        # SIGN-IN door only: no anonymous surface here by design — a credential-less request is
        # challenged so a challenge-driven client (Pro) authenticates. A client that already sent
        # a valid credential (principal resolved) sails through. Malformed/expired credentials
        # already 401'd upstream in the auth seam, so `principal is None` here means "no
        # Authorization header at all" — exactly Pro's pre-challenge request.
        if challenge and principal is None:
            # BASIC_CHALLENGE (auth.py): the one Basic challenge string — its realm renders in the
            # desktop credential dialog, and the bad-credential 401s in the auth seam carry the
            # SAME header so clients keep re-prompting instead of giving up (RFC 7235).
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required — this is the authenticated OGC endpoint",
                headers={"WWW-Authenticate": BASIC_CHALLENGE},
            )

    @router.get(mount, summary="OGC API — Features landing page", include_in_schema=in_schema)
    async def ogc_landing(
        request: Request,
        principal: User | None = Depends(get_optional_user),
    ) -> Response:
        guard(principal)
        base = _base(request, mount)
        return _json({
            "title": "Talus GIS — OGC API Features",
            "description": "Your viewable Talus layers as OGC API Features collections. "
            "Authenticate with a personal access token as HTTP Basic (any username) or a bearer "
            "token." + ("" if challenge else " Public layers are also available anonymously."),
            "links": [
                {"href": base, "rel": "self", "type": "application/json", "title": "this document"},
                # service-desc is REQUIRED by QGIS's landing parser (qgsoapiflandingpagerequest.cpp:
                # missing rel=service-desc OR rel=data ⇒ "Missing information in response" — found
                # against the live 3.44 rig; conformance is ironically optional there). FastAPI's own
                # OpenAPI doc is a genuine service description; the vnd type is what findLink matches.
                {"href": f"{request.url.scheme}://{request.url.netloc}/openapi.json",
                 "rel": "service-desc",
                 "type": "application/vnd.oai.openapi+json;version=3.0",
                 "title": "OpenAPI service description"},
                {"href": f"{base}/conformance", "rel": "conformance", "type": "application/json",
                 "title": "conformance classes"},
                {"href": f"{base}/collections", "rel": "data", "type": "application/json",
                 "title": "feature collections"},
            ],
        })

    @router.get(f"{mount}/conformance", summary="OGC API — Features conformance declaration",
                include_in_schema=in_schema)
    async def ogc_conformance(
        principal: User | None = Depends(get_optional_user),
    ) -> Response:
        guard(principal)
        # BOTH classes, literally — the actual accept/reject gate in Pro and QGIS (recon §1.2).
        return _json({"conformsTo": [_CONF_CORE, _CONF_GEOJSON]})

    @router.get(f"{mount}/collections",
                summary="OGC feature collections — the caller's viewable spatial layers",
                include_in_schema=in_schema)
    async def ogc_collections(
        request: Request,
        conn: AsyncConnection = Depends(get_db),
        principal: User | None = Depends(get_optional_user),
    ) -> Response:
        guard(principal)
        base = _base(request, mount)
        layers = await _viewable_spatial_layers(conn, principal)
        return _json({
            "links": [
                {"href": f"{base}/collections", "rel": "self", "type": "application/json"},
            ],
            "collections": [_collection_doc(base, lid, name) for lid, name in layers],
        })

    @router.get(f"{mount}/collections/{{layer_id}}", summary="One collection's metadata (view-gated)",
                include_in_schema=in_schema)
    async def ogc_collection(
        layer_id: str,
        request: Request,
        conn: AsyncConnection = Depends(get_db),
        principal: User | None = Depends(get_optional_user),
    ) -> Response:
        guard(principal)
        await _require_registered(conn, layer_id)
        await _authorize_read_or_invite(request, conn, principal, layer_id)  # 401-invite for PAT view-denial
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT display_name, geom_type FROM platform.layers WHERE layer_id = %s", (layer_id,)
            )
            row = await cur.fetchone()
        if row is None or row[1] == "none":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        bbox = await _extent_bbox(conn, layer_id)
        return _json(_collection_doc(_base(request, mount), layer_id, row[0], bbox))

    @router.get(f"{mount}/collections/{{layer_id}}/items",
                summary="Collection items (paged GeoJSON, view-gated)", include_in_schema=in_schema)
    async def ogc_items(
        layer_id: str,
        request: Request,
        bbox: str | None = Query(None, description="w,s,e,n in CRS84"),
        limit: int = Query(_DEFAULT_LIMIT, ge=1),
        offset: int = Query(0, ge=0),
        datetime_: str | None = Query(None, alias="datetime"),  # accepted per Core; no temporal cols → ignored
        conn: AsyncConnection = Depends(get_db),
        principal: User | None = Depends(get_optional_user),
    ) -> Response:
        guard(principal)
        await _require_registered(conn, layer_id)
        is_anon = await _authorize_read_or_invite(request, conn, principal, layer_id)
        limit = min(limit, _MAX_LIMIT)  # CLAMP, never reject (QGIS lore)
        if datetime_ is not None:
            log.debug("ogc_datetime_ignored", layer=layer_id)  # accepted-and-ignored: no temporal fields
        total, features = await feature_page(
            conn, layer_id, is_anon,
            filters=[], bbox=bbox, feature_ids=None, sort=None, limit=limit, offset=offset,
        )
        base = _base(request, mount)
        self_qs = f"limit={limit}&offset={offset}" + (f"&bbox={bbox}" if bbox else "")
        links = [
            {"href": f"{base}/collections/{layer_id}/items?{self_qs}", "rel": "self", "type": _GEOJSON_MT},
            {"href": f"{base}/collections/{layer_id}", "rel": "collection", "type": "application/json"},
        ]
        if offset + len(features) < total:
            next_qs = f"limit={limit}&offset={offset + limit}" + (f"&bbox={bbox}" if bbox else "")
            links.append(
                {"href": f"{base}/collections/{layer_id}/items?{next_qs}", "rel": "next", "type": _GEOJSON_MT}
            )
        return _json(
            {
                "type": "FeatureCollection",
                "numberMatched": total,
                "numberReturned": len(features),
                "features": features,
                "links": links,
            },
            media_type=_GEOJSON_MT,
        )

    @router.get(f"{mount}/collections/{{layer_id}}/items/{{feature_id}}",
                summary="One feature (view-gated)", include_in_schema=in_schema)
    async def ogc_item(
        layer_id: str,
        feature_id: str,
        request: Request,
        conn: AsyncConnection = Depends(get_db),
        principal: User | None = Depends(get_optional_user),
    ) -> Response:
        guard(principal)
        await _require_registered(conn, layer_id)
        is_anon = await _authorize_read_or_invite(request, conn, principal, layer_id)
        _total, features = await feature_page(
            conn, layer_id, is_anon,
            filters=[], bbox=None, feature_ids=[feature_id], sort=None, limit=1, offset=0,
        )
        if not features:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feature not found")
        base = _base(request, mount)
        doc = features[0]
        doc["links"] = [
            {"href": f"{base}/collections/{layer_id}/items/{feature_id}", "rel": "self", "type": _GEOJSON_MT},
            {"href": f"{base}/collections/{layer_id}", "rel": "collection", "type": "application/json"},
        ]
        return _json(doc, media_type=_GEOJSON_MT)

    return router


# The two doors (module docstring): PUBLIC (anon-friendly, never challenges — the shipped I-1
# surface) and SIGN-IN (challenges an unauthenticated request → Pro authenticates). The wizard
# hands each client the right one.
public_router = _build_ogc_router(PUBLIC_MOUNT, challenge=False)
auth_router = _build_ogc_router(AUTH_MOUNT, challenge=True)
