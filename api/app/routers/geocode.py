"""Geocoding proxy routes (Talus 18c) — auth-gated address search over external locators.

Forward search only (reverse geocoding deferred, decision-geocoding §3 Q6). Every route
requires a signed-in PRINCIPAL and FAILS SOFT: a missing or unreachable geocoder yields a
200 with an empty list (the orchestration layer logs the cause), so the map keeps working
when search is down (§8). Talus consumes the locator — it never builds one
(product-spec.md:137).

The principal (decision-talus-field-f7b.md g1, fork A): these are READ doors, so they take the
optional-auth seam like every other read door — a session Bearer OR a personal access token
sent as Basic (a desktop PAT, or a Talus Field pack's device token, which is the only credential
a crew phone holds once its portal session lapses). Anonymous is still refused: a locator proxy
is never a public surface, and `_require_principal` keeps the exact 401 the doors always gave.
"""
from fastapi import HTTPException, status, APIRouter, Depends, Query
from psycopg import AsyncConnection
from pydantic import BaseModel

from .. import geocode
from ..auth import require_principal as _require_principal
from ..db import get_db
from ..geo import parse_bbox
from ..geocoders import active_geocoder_configs
from ..models.user import User

router = APIRouter(prefix="/geocode", tags=["geocode"])


class SuggestResponse(BaseModel):
    suggestions: list[geocode.GeoSuggestion]
    # True when EVERY locator the walk attempted failed: the type-ahead stays
    # 200 so a keystroke never raises a toast, but the search box can say address search is unavailable.
    unavailable: bool = False


class SearchResponse(BaseModel):
    candidates: list[geocode.GeoCandidate]


class LocatorsResponse(BaseModel):
    locators: list[str]


def _bbox(bbox: str | None) -> list[float] | None:
    """Parse an optional 'xmin,ymin,xmax,ymax' viewport (EPSG:4326) — 400 on a bad box."""
    return list(parse_bbox(bbox)) if bbox else None


@router.get(
    "/locators",
    response_model=LocatorsResponse,
    summary="Configured locator names (for the 'Search in' picker)",
)
async def locators(
    conn: AsyncConnection = Depends(get_db),
    current_user: User = Depends(_require_principal),
) -> LocatorsResponse:
    return LocatorsResponse(locators=geocode.list_locators(await active_geocoder_configs(conn)))


@router.get(
    "/suggest",
    response_model=SuggestResponse,
    summary="Address autocomplete (proxied from a configured external geocoder)",
)
async def suggest(
    q: str = Query("", description="Partial address text"),
    locator: str | None = Query(None, description="Restrict to one configured locator (picker)"),
    bbox: str | None = Query(None, description="Map viewport 'xmin,ymin,xmax,ymax' (EPSG:4326)"),
    conn: AsyncConnection = Depends(get_db),
    current_user: User = Depends(_require_principal),
) -> SuggestResponse:
    configs = await active_geocoder_configs(conn)
    try:
        suggestions = await geocode.suggest(configs, q, locator=locator, bbox=_bbox(bbox), conn=conn)
    except geocode.LocatorsUnavailable:
        return SuggestResponse(suggestions=[], unavailable=True)
    return SuggestResponse(suggestions=suggestions)


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Resolve an address to coordinates (EPSG:4326)",
)
async def search(
    q: str = Query("", description="Address text (or the picked suggestion's text)"),
    source: str | None = Query(None, description="Geocoder name that issued the suggestion"),
    magic_key: str | None = Query(None, description="Opaque suggestion token (ArcGIS magicKey)"),
    locator: str | None = Query(None, description="Restrict to one configured locator (picker)"),
    bbox: str | None = Query(None, description="Map viewport 'xmin,ymin,xmax,ymax' (EPSG:4326)"),
    conn: AsyncConnection = Depends(get_db),
    current_user: User = Depends(_require_principal),
) -> SearchResponse:
    configs = await active_geocoder_configs(conn)
    try:
        candidates = await geocode.search(
            configs, q, source=source, magic_key=magic_key, locator=locator, bbox=_bbox(bbox),
            conn=conn,
        )
    except geocode.LocatorsUnavailable as exc:
        # A deliberate search (press Enter / a picked suggestion) whose every locator failed is a server
        # fault to show, with its error code: 502, fixed reason, no locator text (the admin reads the names
        # in Admin → Geocoders; the Helper reads the ring).
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "locators_unavailable",
                    "message": f"address search is unavailable: every configured locator failed to answer "
                               f"({exc.failed} of {exc.attempted}) — an administrator can check Admin → Geocoders"},
        )
    return SearchResponse(candidates=candidates)


class ReverseResponse(BaseModel):
    available: bool
    address: str | None = None
    detail: str | None = None


@router.get(
    "/reverse",
    response_model=ReverseResponse,
    summary="Reverse geocode a point to an address (proxied from a configured reverse-capable locator)",
)
async def reverse(
    lon: float = Query(..., description="Longitude (EPSG:4326)"),
    lat: float = Query(..., description="Latitude (EPSG:4326)"),
    locator: str | None = Query(None, description="Restrict to one configured locator (the 'Search in' picker)"),
    conn: AsyncConnection = Depends(get_db),
    current_user: User = Depends(_require_principal),
) -> ReverseResponse:
    """The 'What's here?' popup's address line (decision-reverse-geocode.md). Scoped to the selected `locator`
    when the picker chose one (so reverse uses the SAME service as forward search), else walks all configured
    geocoders in order; first that resolves wins. Fails soft: no reverse-capable/reachable locator → available
    false (the UI shows 'No address found'), never an error. Talus consumes the locator, never builds one."""
    configs = await active_geocoder_configs(conn)
    address = await geocode.reverse(configs, lon, lat, locator=locator, conn=conn)
    return ReverseResponse(available=address is not None, address=address)
