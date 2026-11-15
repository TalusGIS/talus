"""Geocoding proxy client (Talus 18c) — consume external locator services.

Talus does NOT build or operate a geocoder (permanent non-goal,
product-spec.md:137). This module is a thin client/proxy over locators the
*deployment* already runs: primarily a county/org ArcGIS REST GeocodeServer,
plus a self-hosted Nominatim (commit 3).

One adapter per protocol behind a common interface; the orchestration layer
(`suggest`/`search`) walks the configured geocoders in `order` and returns the
first that yields results — the point-locator -> street-locator fallback
(decision-geocoding §3 Q5). Every outbound call FAILS SOFT: a slow / unreachable
/ erroring service is logged and skipped, never raised, so address search
degrading never takes the map down (§6/§8). The routes turn an all-empty result
into a 200 with an empty list.

Mode B (decision-own-data-geocoder.md): a locator of type `internal` geocodes
against a hosted address-point layer IN PostGIS — its adapter is the conn-aware
sibling in `geocode_internal.py` (transport "db" vs "http"), and the walk here
branches per adapter kind: internal adapters get the request's DB connection
(passed by the routes), HTTP adapters keep the shared httpx client. Fail-soft
parity — with one conn-specific rule: after an internal-adapter error the walk
ROLLS BACK the connection, because a failed statement leaves the transaction
aborted and would otherwise poison every later locator on the same conn.
(`geocode_internal` imports this module's contract models; the import back is
deferred inside `_adapter_for`, so there is no cycle.)

Coordinates are normalized to EPSG:4326 `{label, lon, lat, score, source}`. The
ArcGIS adapter requests `outSR=4326`, so x=lon / y=lat with no reprojection here.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import httpx
import psycopg
import structlog
from psycopg import AsyncConnection
from pydantic import BaseModel

from .config import GeocoderConfig

log = structlog.get_logger(__name__)

# Outbound budget. Short enough that a dead geocoder fails soft quickly rather than
# hanging the request; the frontend debounces + cancels in-flight calls on top of this.
_TIMEOUT = httpx.Timeout(8.0)
# Cap candidates returned to the client — a search box needs a handful, not hundreds.
_MAX_CANDIDATES = 8
# Nominatim's public instance requires a descriptive User-Agent (it blocks generic/none) and
# rate-limits to ~1 req/s — real volume should self-host (the air-gapped path). The debounce
# on the client keeps interactive use well under the limit.
_NOMINATIM_UA = "TalusGIS/0.1 (geocoding proxy)"

# Batch geocoding (decision-batch-geocoding.md). A bulk run POSTs many records per call, so it needs a
# longer outbound budget than the interactive 8s — still bounded so a dead service fails the run rather
# than hanging it.
_BATCH_TIMEOUT = httpx.Timeout(60.0)
# ArcGIS geocodeAddresses chunk size when the service advertises no SuggestedBatchSize in its metadata.
_ARCGIS_BATCH_FALLBACK = 150
# OSM/Nominatim public hosts forbid bulk geocoding (usage policy) — batch is allowed only against a
# self-hosted instance (decision §3 F6a). Detected by host so the public block is honest, not a trap.
_PUBLIC_NOMINATIM_HOSTS = {"nominatim.openstreetmap.org", "nominatim.osm.org"}


def _host(url: str | None) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _is_public_nominatim(url: str | None) -> bool:
    """A Nominatim URL that points at the OSMF public instance (bulk-forbidden, F6a)."""
    h = _host(url)
    return h in _PUBLIC_NOMINATIM_HOSTS or h.endswith(".openstreetmap.org")


def _is_ago_locator_host(url: str | None) -> bool:
    """AGO-hosted (credit-consuming) geocode host — the backstop when metadata omits isAGOWorldLocator."""
    return _host(url).endswith("arcgis.com")


def _extent_str(box: list[float]) -> str:
    """[xmin, ymin, xmax, ymax] -> "xmin,ymin,xmax,ymax" — Nominatim viewbox (always WGS84)."""
    return ",".join(str(c) for c in box)


def _arcgis_extent(box: list[float]) -> str:
    """ArcGIS searchExtent as a JSON envelope WITH an explicit wkid:4326. A bare comma string
    is interpreted in the service's default SR (usually Web Mercator), so WGS84 degrees would
    be misread as meters and match nothing — the SR must be stated."""
    return json.dumps(
        {
            "xmin": box[0],
            "ymin": box[1],
            "xmax": box[2],
            "ymax": box[3],
            "spatialReference": {"wkid": 4326},
        }
    )


class GeoSuggestion(BaseModel):
    """One autocomplete row. `source` + `magic_key` let the client resolve THIS exact
    suggestion via /geocode/search (magicKey is meaningful only to the issuing service)."""

    text: str
    source: str
    magic_key: str | None = None


class GeoCandidate(BaseModel):
    """A resolved location, normalized to EPSG:4326."""

    label: str
    lon: float
    lat: float
    score: float | None = None
    source: str
    # ESRI Addr_type/Match_type (PointAddress, StreetName, …) — a rooftop-vs-approximate QA signal for
    # batch review (decision-batch-geocoding.md, doc review). None for locators that don't report it.
    match_type: str | None = None


class GeocoderError(Exception):
    """An external geocoder returned an error payload (e.g. ArcGIS's HTTP-200 error body).
    Caught by the orchestration layer and treated as "no results from this geocoder"."""


class GeocoderAdapter(ABC):
    """Protocol adapter: normalize one geocoding service to suggest/search.

    Adapters DO NOT swallow their own errors — they raise httpx.HTTPError (network)
    or GeocoderError (service-level). The orchestration layer owns the fail-soft
    policy so the fallback walk stays in one place.
    """

    transport = "http"  # the walk's branch key (InternalLocatorAdapter.transport = "db")

    def __init__(self, config: GeocoderConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @abstractmethod
    async def suggest(
        self, client: httpx.AsyncClient, q: str, bbox: list[float] | None = None
    ) -> list[GeoSuggestion]: ...

    @abstractmethod
    async def search(
        self,
        client: httpx.AsyncClient,
        q: str | None,
        magic_key: str | None,
        bbox: list[float] | None = None,
    ) -> list[GeoCandidate]: ...

    async def reverse(self, client: httpx.AsyncClient, lon: float, lat: float) -> str | None:
        """Point (EPSG:4326) → a single-line address, or None. Default None = this service doesn't do reverse, so
        the orchestration walk moves on. Overridden by services that support it (decision-reverse-geocode.md)."""
        return None

    async def search_batch(
        self, client: httpx.AsyncClient, queries: list[str], batch_size: int | None = None,
        bbox: list[float] | None = None, country: str | None = None,
    ) -> list[list[GeoCandidate]]:
        """Bulk forward-geocode, candidates per input aligned by index to `queries`
        (decision-batch-geocoding.md). Default = per-row `search()`; ArcGIS/Nominatim override. A blank
        input yields `[]` (skipped, never sent). A per-row service-level `GeocoderError` maps to `[]` so one
        bad row doesn't sink the run; a transport error propagates so a dead service aborts the run rather
        than silently marking every row unmatched. `country` (a country-narrow) is honored by the overriding
        adapters; the generic loop ignores it."""
        out: list[list[GeoCandidate]] = []
        for q in queries:
            if not q or not q.strip():
                out.append([])
                continue
            try:
                out.append(await self.search(client, q, None, bbox=bbox))
            except GeocoderError:
                out.append([])
        return out

    def _extent(self, bbox: list[float] | None) -> list[float] | None:
        """Effective search extent. A configured `extent` (the org boundary) is authoritative
        and WINS — so a fresh New Map zoomed out to the whole US can't reintroduce worldwide
        junk. The per-request viewport bbox is the fallback only when no extent is configured
        ("search what you're looking at")."""
        return self.config.extent or bbox


class ArcGISGeocodeServerAdapter(GeocoderAdapter):
    """Esri ArcGIS REST GeocodeServer (e.g. the county WebPointLocator / WebStreetLocator).

    suggest -> /suggest (returns {text, magicKey}); resolve -> /findAddressCandidates
    (by magicKey for a picked suggestion, else by free text). ArcGIS returns HTTP 200
    with an {"error": {...}} body on failure, so `_get` raises GeocoderError on it.
    """

    async def suggest(
        self, client: httpx.AsyncClient, q: str, bbox: list[float] | None = None
    ) -> list[GeoSuggestion]:
        params: dict[str, object] = {"text": q, "f": "json"}
        box = self._extent(bbox)
        if box:
            params["searchExtent"] = _arcgis_extent(box)
        if self.config.token:
            params["token"] = self.config.token
        data = await self._get(client, "suggest", params)
        out: list[GeoSuggestion] = []
        for s in data.get("suggestions") or []:
            text = s.get("text")
            if not text:
                continue
            out.append(GeoSuggestion(text=text, source=self.name, magic_key=s.get("magicKey")))
        return out

    async def search(
        self,
        client: httpx.AsyncClient,
        q: str | None,
        magic_key: str | None,
        bbox: list[float] | None = None,
    ) -> list[GeoCandidate]:
        if not q and not magic_key:
            return []
        params: dict[str, object] = {
            "f": "json",
            "outSR": self.config.out_sr,
            "maxLocations": _MAX_CANDIDATES,
        }
        # ArcGIS wants the suggestion text alongside its magicKey to resolve it.
        if q:
            params["singleLine"] = q
        if magic_key:
            params["magicKey"] = magic_key
        box = self._extent(bbox)
        if box:
            params["searchExtent"] = _arcgis_extent(box)
        if self.config.token:
            params["token"] = self.config.token
        data = await self._get(client, "findAddressCandidates", params)
        out: list[GeoCandidate] = []
        for c in data.get("candidates") or []:
            loc = c.get("location") or {}
            x, y = loc.get("x"), loc.get("y")
            if x is None or y is None:
                continue
            out.append(
                GeoCandidate(
                    label=c.get("address") or q or "",
                    lon=float(x),
                    lat=float(y),
                    score=c.get("score"),
                    source=self.name,
                )
            )
        return out

    async def reverse(self, client: httpx.AsyncClient, lon: float, lat: float) -> str | None:
        params: dict[str, object] = {"location": f"{lon},{lat}", "f": "json", "outSR": 4326}
        if self.config.token:
            params["token"] = self.config.token
        data = await self._get(client, "reverseGeocode", params)  # GeocoderError on an {"error":...} body
        addr = data.get("address") or {}
        label = addr.get("Match_addr") or addr.get("LongLabel") or addr.get("Label")
        return label or None

    async def search_batch(
        self, client: httpx.AsyncClient, queries: list[str], batch_size: int | None = None,
        bbox: list[float] | None = None, country: str | None = None,
    ) -> list[list[GeoCandidate]]:
        """The true ESRI batch op — `geocodeAddresses` (decision §3 F6). Records are chunked at
        `batch_size` (the service's SuggestedBatchSize, resolved by preflight; falls back to 150), each
        record tagged `OBJECTID = 1-based input index` so `ResultID` maps the result back to its row.
        `country` (comma-separated codes) narrows via `sourceCountry` (F-country). Unmatched (`Status='U'`)
        or coordinate-less results yield `[]` for that row (→ no_match). Blank inputs are never sent.
        Leak-safe: `_post` reports status+op only, never the token-bearing URL."""
        size = max(1, batch_size or _ARCGIS_BATCH_FALLBACK)
        out: list[list[GeoCandidate]] = [[] for _ in queries]
        box = self._extent(bbox)
        extent = _arcgis_extent(box) if box else None
        for start in range(0, len(queries), size):
            records = [
                {"attributes": {"OBJECTID": start + offset + 1, "SingleLine": q}}
                for offset, q in enumerate(queries[start:start + size])
                if q and q.strip()
            ]
            if not records:
                continue
            params: dict[str, object] = {
                "f": "json", "outSR": self.config.out_sr,
                "addresses": json.dumps({"records": records}),
            }
            if country:
                params["sourceCountry"] = country
            if extent:
                params["searchExtent"] = extent
            if self.config.token:
                params["token"] = self.config.token
            data = await self._post(client, "geocodeAddresses", params)
            for loc in data.get("locations") or []:
                attrs = loc.get("attributes") or {}
                rid = attrs.get("ResultID")
                if not isinstance(rid, int) or not (1 <= rid <= len(queries)):
                    continue
                if str(attrs.get("Status") or "").upper() == "U":  # ESRI: U = unmatched
                    continue
                geo = loc.get("location") or {}
                x, y = geo.get("x"), geo.get("y")
                if x is None or y is None:
                    continue
                out[rid - 1].append(
                    GeoCandidate(
                        label=attrs.get("Match_addr") or loc.get("address") or queries[rid - 1],
                        lon=float(x), lat=float(y), score=attrs.get("Score"), source=self.name,
                        match_type=attrs.get("Addr_type"),
                    )
                )
        return out

    async def _post(
        self, client: httpx.AsyncClient, op: str, data: dict[str, object]
    ) -> dict:
        """POST an ArcGIS REST op (batch payloads exceed a GET query string). Same leak-safe error
        handling as `_get`: NEVER `raise_for_status()` (it embeds the token-bearing URL in the exception);
        status + op is all a caller needs. ArcGIS returns HTTP 200 with an {"error":...} body on failure."""
        url = f"{self.config.url.rstrip('/')}/{op}"
        resp = await client.post(url, data=data)
        if resp.status_code >= 400:
            raise GeocoderError(f"{self.name}: HTTP {resp.status_code} on {op}")
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise GeocoderError(f"{self.name}: {payload['error']}")
        return payload if isinstance(payload, dict) else {}

    async def _get(
        self, client: httpx.AsyncClient, op: str, params: dict[str, object]
    ) -> dict:
        url = f"{self.config.url.rstrip('/')}/{op}"
        resp = await client.get(url, params=params)
        if resp.status_code >= 400:
            # NOT raise_for_status(): httpx embeds the full request URL — query string, token= and
            # all — in the exception text, which surfaces in walk logs and the Test probe's error
            # (decision-geocoder-token-locators §Security T3). Status + op is all a caller needs.
            raise GeocoderError(f"{self.name}: HTTP {resp.status_code} on {op}")
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise GeocoderError(f"{self.name}: {data['error']}")
        return data if isinstance(data, dict) else {}


class NominatimAdapter(GeocoderAdapter):
    """OpenStreetMap Nominatim (OSS; self-hostable — the air-gapped path).

    Unlike ArcGIS there's no suggest/magicKey concept: autocomplete and resolve both hit
    /search, and coordinates are already EPSG:4326 (lat/lon, returned as strings). A picked
    suggestion is re-resolved by its display_name text. The public instance requires a
    descriptive User-Agent and rate-limits to ~1 req/s, so a deployment doing real volume
    self-hosts (config `url` points at its own instance) — Talus just speaks the protocol.
    """

    async def suggest(
        self, client: httpx.AsyncClient, q: str, bbox: list[float] | None = None
    ) -> list[GeoSuggestion]:
        out: list[GeoSuggestion] = []
        for r in await self._search(client, q, bbox):
            name = r.get("display_name")
            if name:
                out.append(GeoSuggestion(text=name, source=self.name, magic_key=None))
        return out

    async def search(
        self,
        client: httpx.AsyncClient,
        q: str | None,
        magic_key: str | None,
        bbox: list[float] | None = None,
    ) -> list[GeoCandidate]:
        # No magicKey — resolve by text (the picked suggestion's display_name, or free text).
        if not q:
            return []
        out: list[GeoCandidate] = []
        for r in await self._search(client, q, bbox):
            lon, lat = r.get("lon"), r.get("lat")
            if lon is None or lat is None:
                continue
            importance = r.get("importance")
            out.append(
                GeoCandidate(
                    label=r.get("display_name") or q,
                    lon=float(lon),
                    lat=float(lat),
                    # Nominatim returns `importance` (0..1); scale to a 0..100 score like ArcGIS.
                    score=round(float(importance) * 100, 1) if importance is not None else None,
                    source=self.name,
                )
            )
        return out

    async def _search(
        self, client: httpx.AsyncClient, q: str, bbox: list[float] | None = None,
        country: str | None = None,
    ) -> list:
        url = f"{self.config.url.rstrip('/')}/search"
        params: dict[str, object] = {"q": q, "format": "jsonv2", "limit": _MAX_CANDIDATES}
        box = self._extent(bbox)
        if box:
            # viewbox biases; bounded=1 hard-restricts results to the box (the "limit to area" knob).
            params["viewbox"] = _extent_str(box)
            params["bounded"] = 1
        if country:
            params["countrycodes"] = country  # comma-separated ISO alpha-2 (the country-narrow)
        resp = await client.get(url, params=params, headers={"User-Agent": _NOMINATIM_UA})
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    async def search_batch(
        self, client: httpx.AsyncClient, queries: list[str], batch_size: int | None = None,
        bbox: list[float] | None = None, country: str | None = None,
    ) -> list[list[GeoCandidate]]:
        """Per-row loop (no batch op exists — F6a), applying the optional `country` narrow via
        `countrycodes`. A blank input yields `[]`; a transport error propagates (a dead service aborts the
        run). Only reached for a self-hosted host (the public one is blocked before this)."""
        out: list[list[GeoCandidate]] = []
        for q in queries:
            if not q or not q.strip():
                out.append([])
                continue
            cands: list[GeoCandidate] = []
            for r in await self._search(client, q, bbox, country):
                lon, lat = r.get("lon"), r.get("lat")
                if lon is None or lat is None:
                    continue
                importance = r.get("importance")
                cands.append(GeoCandidate(
                    label=r.get("display_name") or q, lon=float(lon), lat=float(lat),
                    score=round(float(importance) * 100, 1) if importance is not None else None,
                    source=self.name,
                ))
            out.append(cands)
        return out

    async def reverse(self, client: httpx.AsyncClient, lon: float, lat: float) -> str | None:
        url = f"{self.config.url.rstrip('/')}/reverse"
        params: dict[str, object] = {"lat": lat, "lon": lon, "format": "jsonv2"}
        resp = await client.get(url, params=params, headers={"User-Agent": _NOMINATIM_UA})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and not data.get("error"):
            return data.get("display_name") or None
        return None


def _adapter_for(cfg: GeocoderConfig):
    """Build the adapter for a config entry, or None for a type not yet supported, so an
    unrecognized entry is skipped (logged upstream as no results), not fatal. Returns a
    GeocoderAdapter (http) or an InternalLocatorAdapter (db) — callers branch on `.transport`."""
    if cfg.type == "arcgis":
        return ArcGISGeocodeServerAdapter(cfg)
    if cfg.type == "nominatim":
        return NominatimAdapter(cfg)
    if cfg.type == "internal":
        # Deferred import: geocode_internal imports this module's contract models at ITS top
        # level, so importing it here (not at ours) is what keeps the pair acyclic.
        from .geocode_internal import InternalLocatorAdapter

        return InternalLocatorAdapter(cfg)
    return None


def _ordered_adapters(configs: list[GeocoderConfig]) -> list:
    """The given geocoder configs as adapters (http AND db kinds — branch on `.transport`),
    ascending by `order` (point locator first).

    Talus 37: the config LIST now comes from the caller (the router reads it from the DB store —
    enabled rows ∪ token-bearing env entries — via `geocoders.active_geocoder_configs`), instead of
    this module reading `settings.geocoders` directly. The orchestration is otherwise unchanged, so
    the /geocode/* API contract is identical."""
    out: list = []
    for cfg in sorted(configs, key=lambda c: c.order):
        adapter = _adapter_for(cfg)
        if adapter is not None:
            out.append(adapter)
    return out


def _adapter_by_name(configs: list[GeocoderConfig], name: str):
    for adapter in _ordered_adapters(configs):
        if adapter.name == name:
            return adapter
    return None


def _scoped_adapters(configs: list[GeocoderConfig], locator: str | None) -> list:
    """Adapters to walk: just the named one when the "Search in" picker selects a locator,
    else all (ordered). An unknown name yields [] (fail-soft empty, not the full list)."""
    if locator:
        adapter = _adapter_by_name(configs, locator)
        return [adapter] if adapter is not None else []
    return _ordered_adapters(configs)


def list_locators(configs: list[GeocoderConfig]) -> list[str]:
    """Configured locator names (ordered) — drives the client's "Search in" picker."""
    return [a.name for a in _ordered_adapters(configs)]


# The walk's fail-soft catch set: network (httpx), service-level (GeocoderError — which covers the
# internal adapter's MappingError), and DB errors from the internal adapter (psycopg).
_WALK_ERRORS = (httpx.HTTPError, GeocoderError, psycopg.Error)


class LocatorsUnavailable(Exception):
    """EVERY locator the walk attempted failed (none answered, empty or not). Raised instead of the
    fail-soft [] so a search whose locators are all down says so — the finding of a live test
    (a dead locator was a silent 200 with no error code). A walk
    with no attemptable locator, or one that got an empty answer from any locator, still returns []."""

    def __init__(self, attempted: int, failed: int):
        super().__init__(f"every address locator failed ({failed} of {attempted})")
        self.attempted = attempted
        self.failed = failed


async def _rollback_if_db(adapter, conn: AsyncConnection | None) -> None:
    """After an internal-adapter failure the shared conn's transaction is ABORTED — roll it back so
    the walk's later locators (and the route itself) aren't poisoned. Best-effort: a conn too dead
    to roll back stays fail-soft (the next statement will fail and be skipped like any other)."""
    if getattr(adapter, "transport", "http") == "db" and conn is not None:
        try:
            await conn.rollback()
        except Exception:
            pass


async def probe(
    cfg: GeocoderConfig, q: str, conn: AsyncConnection | None = None
) -> tuple[bool, int, str | None]:
    """The admin "Test" action (decision §3 F3): fire ONE sample suggest against a locator def and
    report (ok, sample_count, error) — so a bad URL is caught before it silently breaks everyone's
    search box. Never raises (a dead/erroring service is a result, not an exception). Honors the
    locator's own `extent` (passed as the bbox). One dispatch point for BOTH transports: an
    internal def probes conn-aware via `geocode_internal.probe` (pass the request's `conn`)."""
    if cfg.type == "internal":
        if conn is None:
            return (False, 0, "internal locator probe requires a database connection")
        from .geocode_internal import probe as _probe_internal  # deferred, like _adapter_for

        return await _probe_internal(conn, cfg, q)
    adapter = _adapter_for(cfg)
    if adapter is None:
        return (False, 0, f"unsupported geocoder type: {cfg.type}")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            results = await adapter.suggest(client, q, bbox=None)
        return (True, len(results), None)
    except (httpx.HTTPError, GeocoderError) as exc:
        return (False, 0, f"{exc.__class__.__name__}: {exc}")


async def suggest(
    configs: list[GeocoderConfig],
    q: str,
    locator: str | None = None,
    bbox: list[float] | None = None,
    conn: AsyncConnection | None = None,
) -> list[GeoSuggestion]:
    """Autocomplete across the configured geocoders, in order, first non-empty wins.
    `locator` scopes to one (the picker); `bbox` restricts to the map view; `conn` carries the
    request's DB connection to internal locators (one without a conn is skipped). Fails soft:
    a geocoder that errors is logged and skipped; all-empty -> []."""
    q = q.strip()
    if not q:
        return []
    attempted = failed = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for adapter in _scoped_adapters(configs, locator):
            # OSMF's usage policy forbids autocomplete (search-as-you-type) against the public
            # Nominatim instance — skip it in the suggest walk only. Press-Enter /geocode/search,
            # click-driven reverse, and the admin Test probe are single deliberate queries and
            # remain allowed; a self-hosted Nominatim keeps its type-ahead.
            cfg = getattr(adapter, "config", None)
            if cfg is not None and cfg.type == "nominatim" and _is_public_nominatim(cfg.url):
                continue
            attempted += 1
            try:
                results = await _adapter_call(adapter, client, conn, "suggest", q, bbox=bbox)
            except _WALK_ERRORS as exc:
                await _rollback_if_db(adapter, conn)
                failed += 1
                log.warning(
                    "geocode_suggest_failed",
                    source=adapter.name,
                    error=exc.__class__.__name__,
                    detail=str(exc),
                )
                continue
            if results:
                return results
    if attempted and failed == attempted:
        raise LocatorsUnavailable(attempted, failed)
    return []


async def _adapter_call(
    adapter, client: httpx.AsyncClient, conn: AsyncConnection | None, method: str, *args, **kwargs
):
    """One adapter operation over its transport — THE dispatch point all three walks share.
    Returns None for an internal adapter given no conn (skipped, like an unsupported type)."""
    fn = getattr(adapter, method)
    if adapter.transport == "db":
        if conn is None:
            return None
        return await fn(conn, *args, **kwargs)
    return await fn(client, *args, **kwargs)


async def search(
    configs: list[GeocoderConfig],
    q: str | None,
    source: str | None = None,
    magic_key: str | None = None,
    locator: str | None = None,
    bbox: list[float] | None = None,
    conn: AsyncConnection | None = None,
) -> list[GeoCandidate]:
    """Resolve an address to EPSG:4326 candidates.

    Picked-suggestion path: `source` + `magic_key` resolve via the exact issuing
    geocoder. Otherwise fall back to a free-text walk (scoped to `locator` if the picker
    selected one, else the ordered geocoders), first non-empty wins. `bbox` restricts to
    the map view; `conn` carries the request's DB connection to internal locators.
    Fails soft like `suggest`."""
    q = (q or "").strip() or None
    if not q and not magic_key:
        return []
    attempted = failed = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # Picked-suggestion path — magicKey is only meaningful to its issuing source.
        if source and magic_key:
            adapter = _adapter_by_name(configs, source)
            if adapter is not None:
                attempted += 1
                try:
                    results = await _adapter_call(
                        adapter, client, conn, "search", q, magic_key, bbox=bbox
                    )
                    if results:
                        return results
                except _WALK_ERRORS as exc:
                    await _rollback_if_db(adapter, conn)
                    failed += 1
                    log.warning(
                        "geocode_search_failed",
                        source=source,
                        error=exc.__class__.__name__,
                        detail=str(exc),
                    )
        # Free-text / fallback path — scoped walk, no magicKey.
        if q:
            for adapter in _scoped_adapters(configs, locator):
                attempted += 1
                try:
                    results = await _adapter_call(
                        adapter, client, conn, "search", q, None, bbox=bbox
                    )
                except _WALK_ERRORS as exc:
                    await _rollback_if_db(adapter, conn)
                    failed += 1
                    log.warning(
                        "geocode_search_failed",
                        source=adapter.name,
                        error=exc.__class__.__name__,
                        detail=str(exc),
                    )
                    continue
                if results:
                    return results
    if attempted and failed == attempted:
        raise LocatorsUnavailable(attempted, failed)
    return []


async def reverse(
    configs: list[GeocoderConfig],
    lon: float,
    lat: float,
    locator: str | None = None,
    conn: AsyncConnection | None = None,
) -> str | None:
    """Point (EPSG:4326) → a single-line address. Scoped to the `locator` the "Search in" picker selected (so
    reverse uses the SAME service forward search is using — e.g. a World-Geocoder view with reverse enabled),
    else walks all configured geocoders in order. First that yields wins; fails soft like `suggest`/`search`
    (a geocoder that errors or doesn't support reverse is skipped; all-empty → None). `conn` carries the
    request's DB connection to internal locators (KNN reverse). decision-reverse-geocode.md."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for adapter in _scoped_adapters(configs, locator):
            try:
                addr = await _adapter_call(adapter, client, conn, "reverse", lon, lat)
            except _WALK_ERRORS as exc:
                await _rollback_if_db(adapter, conn)
                log.warning(
                    "geocode_reverse_failed",
                    source=adapter.name,
                    error=exc.__class__.__name__,
                    detail=str(exc),
                )
                continue
            if addr:
                return addr
    return None


# --- batch (bulk) geocoding (decision-batch-geocoding.md) -----------------------------------------


async def _fetch_arcgis_metadata(cfg: GeocoderConfig) -> dict:
    """Read a GeocodeServer's service metadata (`GET <serviceURL>?f=json`) — the free, no-credit,
    token-optional capability probe (decision §3 F6, layer A). Leak-safe like the adapter's `_get`."""
    params: dict[str, object] = {"f": "json"}
    if cfg.token:
        params["token"] = cfg.token
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(cfg.url.rstrip("/"), params=params)
    if resp.status_code >= 400:
        raise GeocoderError(f"{cfg.name}: HTTP {resp.status_code} on service metadata")
    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise GeocoderError(f"{cfg.name}: {data['error']}")
    return data if isinstance(data, dict) else {}


async def preflight(cfg: GeocoderConfig) -> dict:
    """The three-layer batch pre-flight (decision §3 F6/F6a), per locator type. Returns a plain dict
    (never raises — a failure is a result the UI shows):
      {batchCapable, batchSize, creditConsuming, needsToken, hasToken, maxBatchSize?, reason?}
    - internal → always capable, free, no token.
    - nominatim → self-hosted capable/free; the public OSM host is BLOCKED (F6a) with an honest reason.
    - arcgis → read `?f=json`; batch-capable iff `locatorProperties.MaxBatchSize > 0` (the verified
      signal — NOT the `capabilities` string); `isAGOWorldLocator` (or an arcgis.com host) ⇒ credit-
      consuming ⇒ needs a token. Detected via metadata ONLY — never a trial batch call (that burns a
      credit)."""
    # `type` tells the wizard which locator kind this is — it shows the (external-only) country-narrow
    # step for arcgis/nominatim, and never for the own-data internal locator (country is meaningless there).
    if cfg.type == "internal":
        return {"type": cfg.type, "batchCapable": True, "batchSize": None, "creditConsuming": False,
                "needsToken": False, "hasToken": False, "reason": None}
    if cfg.type == "nominatim":
        public = _is_public_nominatim(cfg.url)
        return {
            "type": cfg.type, "batchCapable": not public, "batchSize": None, "creditConsuming": False,
            "needsToken": False, "hasToken": False,
            "reason": ("OpenStreetMap's public geocoder prohibits bulk use; point this locator at a "
                       "self-hosted Nominatim, or use your internal address layer." if public else None),
        }
    if cfg.type == "arcgis":
        try:
            meta = await _fetch_arcgis_metadata(cfg)
        except GeocoderError as exc:
            # Our own message (name + status/op), token-free by construction — safe to surface.
            reason = str(exc)
        except httpx.HTTPError as exc:
            # A transport error: surface ONLY the class name, never str(exc). The metadata GET carries
            # the token in its query string, so echoing an exception that might embed the request URL
            # would leak it into this response (defense-in-depth with the leak-safe _get/_post).
            reason = f"could not read the locator's service metadata ({exc.__class__.__name__})"
        else:
            reason = None
        if reason is not None:
            return {"type": cfg.type, "batchCapable": False, "batchSize": None, "creditConsuming": False,
                    "needsToken": False, "hasToken": bool(cfg.token), "reason": reason}
        lp = meta.get("locatorProperties") or {}

        def _as_int(v: object) -> int:
            try:
                return int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0

        maxb = _as_int(lp.get("MaxBatchSize"))
        sugg = _as_int(lp.get("SuggestedBatchSize"))
        credit = bool(lp.get("isAGOWorldLocator")) or _is_ago_locator_host(cfg.url)
        batch_size = sugg or (min(maxb, _ARCGIS_BATCH_FALLBACK) if maxb else None)
        return {
            "type": cfg.type, "batchCapable": maxb > 0, "maxBatchSize": maxb, "batchSize": batch_size,
            "creditConsuming": credit, "needsToken": credit, "hasToken": bool(cfg.token),
            "reason": (None if maxb > 0 else
                       "This locator only does single-address lookups; use your internal locator or a "
                       "batch-capable service like the World Geocoder."),
        }
    return {"type": cfg.type, "batchCapable": False, "batchSize": None, "creditConsuming": False,
            "needsToken": False, "hasToken": False, "reason": f"unsupported locator type: {cfg.type}"}


async def search_batch(
    configs: list[GeocoderConfig],
    queries: list[str],
    locator: str,
    conn: AsyncConnection | None = None,
    batch_size: int | None = None,
    bbox: list[float] | None = None,
    country: str | None = None,
) -> list[list[GeoCandidate]]:
    """Bulk-resolve `queries` through the ONE chosen `locator` (decision-batch-geocoding.md §4). Unlike
    the interactive walk this does NOT fall back across locators — a batch run targets a single locator
    the user picked. Returns candidates per input aligned by index (best first); a blank or unresolved
    input → `[]`. `country` = an optional comma-separated ISO alpha-2 list to narrow the EXTERNAL locators
    to (ArcGIS `sourceCountry` / Nominatim `countrycodes`); ignored by the own-data internal locator.
    Guards the public-Nominatim bulk block (F6a) even if a caller skipped `preflight`. An unknown locator
    name → all-empty (fail-soft, mirrors the walk)."""
    if not queries:
        return []
    adapter = _adapter_by_name(configs, locator)
    if adapter is None:
        return [[] for _ in queries]
    cfg = getattr(adapter, "config", None)
    if cfg is not None and cfg.type == "nominatim" and _is_public_nominatim(cfg.url):
        raise GeocoderError("bulk geocoding is not allowed against the public OpenStreetMap geocoder")
    if getattr(adapter, "transport", "http") == "db":
        if conn is None:
            return [[] for _ in queries]
        return await adapter.search_batch(conn, queries, bbox=bbox)  # internal = own data, no country
    async with httpx.AsyncClient(timeout=_BATCH_TIMEOUT) as client:
        return await adapter.search_batch(client, queries, batch_size=batch_size, bbox=bbox, country=country)
