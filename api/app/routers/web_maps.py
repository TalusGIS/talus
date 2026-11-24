"""Talus 20 — web maps (save & own a composed map).

A web map is a SAVED VIEW over existing layers: it references layers by id and stores the
basemap + viewport. It never copies or transforms layer data (decision-webmap-save.md §1).

Authz mirrors layers via the OpenFGA `web_map` object type (Talus 20 c1), MINUS the edit
machinery: `can_view` gates open; update/delete is the `owner` relation OR talus-admin, checked
at the app layer (like release_layer). Creating a web map is publisher/admin only (F3 — field
crew edit data, they don't mint items; same as AGO).

Lifecycle (mirrors publisher.py's failure-safe ordering):
  - create: app-generates the UUID -> write the OpenFGA `owner` tuple FIRST -> insert the row.
    A mid-failure leaves a harmless orphan tuple (invisible; cleaned on retry), not an
    unopenable orphan row.
  - delete: OpenFGA-FIRST (read every web_map:<id> tuple -> delete) -> delete the row. The
    inverse of create, fail-safe (decision-11 §Q3 logic).

No shadow-audit table (F1b): created_by/updated_by attribute the row; a web map is presentation
config over already-audited data.
"""
from typing import Any, Literal
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator, model_validator

from .. import admin_audit, last_accessed
from ..auth import _optional_claims, _validated_claims, get_current_user, get_optional_user
from ..authz import (
    OPEN_DATA_GROUP,
    PUBLIC_SYSTEM_GROUPS,
    _has_admin_role,
    _has_publish_role,
    _openfga_check,
    _openfga_delete,
    _openfga_list_objects,
    _openfga_read_object_tuples,
    _openfga_read_user_groups,
    _openfga_write,
    assert_may_share_public,
    get_object_shares,
    manage_allowed,
    public_object_allowed,
    set_object_public,
    set_object_shares,
)
from ..db import get_db
from ..models.user import User
from .layer_query import _VALID_OPS  # single source of truth for the filter-op whitelist
from .metadata import metadata_complete

log = structlog.get_logger(__name__)

router = APIRouter(tags=["web-maps"])

# The basemap keys the New Map switcher offers (BasemapSwitcher.tsx). A saved map names one of
# these; the frontend resolves it to the actual style/source.
# V115 added the two streamed names (decision-streamed-basemap.md §14). A saved map may name a
# streamed map the portal later stops offering; the client then falls back to Light at load, the
# same way it treats the retired "white". This stays a list of NAMES — never an address.
# V116 added the two OpenStreetMap looks drawn gray and dark in the browser (§16).
_BASEMAPS = frozenset(
    {"light", "dark", "grayscale", "white", "imagery", "osm", "usgstopo", "osm-gray", "osm-dark"}
)
_MAX_TITLE_LEN = 200
_MAX_LAYERS = 200  # a sane ceiling; a web map is a curated view, not a dump of everything
_MAX_BOOKMARKS = 50  # per map — a generous municipal ceiling, well under _MAX_LAYERS
_MAX_TABLES = 20  # tables shown on a map (decision-map-tables.md) — a curated set, not a warehouse
_MAX_BOOKMARK_NAME = 80  # bookmark names are short labels, not prose
_MAX_FILTERS = 10  # per-layer attribute-filter conditions cap (decision-feature-filter.md §4a.5)
_MAX_POPUP_FIELDS = 30  # per-layer popup visible-field cap (decision-map-popups.md §3) — a curated preview, not a schema dump


def _clean_label(v: str, max_len: int, field: str) -> str:
    """Shared text-label validation for the map title AND bookmark names: strip, require non-empty,
    cap length, and reject control characters. Keeps the two validators in lockstep."""
    v = v.strip()
    if not v:
        raise ValueError(f"{field} must not be empty")
    if len(v) > max_len:
        raise ValueError(f"{field} must be at most {max_len} characters")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
        raise ValueError(f"{field} must not contain control characters")
    return v


# --- the web-map definition (validated before it lands in jsonb) ----------------


class FilterCondition(BaseModel):
    # One attribute-filter condition on an operational layer (decision-feature-filter.md). Mirrors the
    # injection-safe `/features?filter=field:op:value` primitives: a field name, a whitelisted op, and a
    # value. Persisted verbatim in the web_map jsonb and re-validated at render time by layer_query
    # (field ∈ the layer's real columns; op ∈ _VALID_OPS; value bound) — so a hand-edited definition can
    # never smuggle SQL. `value` is a string for every op; `in` ("is any of") is comma-joined, and AGO's
    # "is between" is stored as two conditions (gte + lte), not a special shape (§4a.2).
    field: str = Field(min_length=1, max_length=63)  # a Postgres identifier (NAMEDATALEN-1); revalidated at query time
    op: str
    value: str = Field(max_length=2000)

    @field_validator("op")
    @classmethod
    def _check_op(cls, v: str) -> str:
        if v not in _VALID_OPS:
            raise ValueError(f"filter op must be one of {sorted(_VALID_OPS)}")
        return v


class LabelConfig(BaseModel):
    # Per-layer map labels (decision-map-labels.md): ONE label class, text = an attribute field only
    # (the standing no-expression-engine fence — no Arcade, no concatenation). Map-scoped presentation
    # persisted in the web_map jsonb (no migration; absent = labels off, so every pre-labels map still
    # validates). The field name is never interpolated into SQL — it's a MapLibre ['get', field] key
    # over the existing view-gated /features fetch (slots/views) or V022's baked tile properties
    # (built-ins) — but it's bounded like FilterCondition.field anyway.
    field: str = Field(min_length=1, max_length=63)  # a Postgres identifier (NAMEDATALEN-1)
    size: int = Field(default=12, ge=8, le=32)
    color: str = Field(default="#1a1a1a", pattern=r"^#[0-9a-fA-F]{6}$")
    haloColor: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    bold: bool = False  # Noto Sans Medium vs Regular — a two-fontstack toggle, NOT a font picker
    minZoom: float | None = Field(default=None, ge=0.0, le=24.0)


class PopupField(BaseModel):
    # One visible popup field (decision-map-popups.md §3): array order IS display order. An object (not a
    # bare string) so later per-field options (Esri fieldInfos analogy) need no reshape of saved maps.
    name: str = Field(min_length=1, max_length=63)  # a Postgres identifier (NAMEDATALEN-1)


class PopupConfig(BaseModel):
    # Per-layer, per-map popup configuration (decision-map-popups.md): the STRUCTURAL subset of Esri's
    # popupInfo — on/off + title field + ordered visible-field list. NO templates/expressions/media (the
    # standing no-expression-engine fence; every OSS analog is a template engine — kickoff §0). Absent =
    # defaults (built-ins curated rows; slots notes+owner fields; views all). Field names are never
    # interpolated into SQL — they only pick keys off already-fetched feature props client-side.
    enabled: bool = True  # False = this layer is skipped by map identify on THIS map
    titleField: str | None = Field(default=None, min_length=1, max_length=63)  # None = layer-label title
    fields: list[PopupField] | None = Field(default=None, max_length=_MAX_POPUP_FIELDS)  # None = default rows; [] = title-only


class OperationalLayer(BaseModel):
    # An operational layer is a vector layer (kind='layer', layerId), a raster (kind='raster',
    # rasterId, opacity), or an EXTERNAL reference (kind='external' — decision-add-from-url.md), in one
    # ordered list (front = top, draw order). `kind` defaults to 'layer' so every PRE-raster saved map
    # (just {layerId, visible}) still validates — definition is jsonb, no migration. opacity applies to
    # raster + external-raster kinds (vectors style via the renderer).
    kind: Literal["layer", "raster", "external"] = "layer"
    layerId: str | None = None
    rasterId: str | None = None
    visible: bool = True
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    # --- external-only (decision-add-from-url.md): a REFERENCE to a remote public service — nothing
    # ingested, no catalog item; the map stores a URL the VIEWER'S browser fetches directly (no proxy,
    # no SSRF surface). `url`/`where` are never interpolated into HTML or SQL — they only feed the
    # browser's fetch/MapLibre against the REMOTE server; bounded like every other persisted string.
    externalId: str | None = Field(default=None, pattern=r"^ext_[0-9a-f]{8}$")  # the map-source/TOC key
    # Optional link to a shared catalog ITEM (decision-external-catalog-items.md, V060): when set,
    # the map resolves GET /external-refs/{id} on open and the ITEM's spec wins (central fixes
    # propagate); the fields below stay populated as a SNAPSHOT so the map renders unchanged when
    # the item is deleted/unshared (404 → fall back — maps never break). Additive: absent on every
    # map-scoped external, so all pre-item definitions still validate.
    externalItemId: str | None = Field(default=None, pattern=r"^xref_[0-9a-f]{8}$")
    externalKind: Literal["arcgis-feature", "geojson", "xyz", "arcgis-image"] | None = None
    url: str | None = Field(default=None, max_length=2000)
    # Vector externals: the drawn geometry family (from the add-time service probe) + display color.
    geometryType: Literal["point", "line", "polygon"] | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    # arcgis-feature only: a where-clause carried from a pasted /query URL — sent as a bound query
    # PARAM to the remote ArcGIS server (never touches our SQL); shown read-only in the UI (v1).
    where: str | None = Field(default=None, max_length=2000)
    # raster-only: a named colormap applied to a single-band raster (e.g. a hotspot/KDE output). None =
    # default render (RGB for multiband, grayscale for single-band). A display choice, validated + applied
    # by the serve path; stored verbatim so a reopened map restores it.
    colormap: str | None = Field(default=None, max_length=40)
    # Per-layer attribute filter (decision-feature-filter.md): AND-composed simple predicates that narrow the
    # layer to matching features (rendered via the GeoJSON/View path, not the MVT tiles). Absent/empty =
    # unfiltered — every PRE-filter saved map still validates (jsonb, no migration). Capped so a persisted
    # definition can't carry an unbounded predicate list. Vector-only in practice (rasters ignore it).
    filters: list[FilterCondition] = Field(default_factory=list, max_length=_MAX_FILTERS)
    # Per-layer labels (decision-map-labels.md): map-scoped presentation, absent/None = off. Vector +
    # view layers only in practice (rasters and non-spatial tables are never labelable — no features
    # drawn); a raster op simply never carries one.
    labels: LabelConfig | None = None
    # Per-layer popup config (decision-map-popups.md): absent/None = default popup behavior. Same
    # vector+view scoping as labels (a raster op's identify page is the sampled band values).
    popup: PopupConfig | None = None
    # Map-only display-name override (decision-map-rename.md): renames the layer ON THIS MAP only —
    # never the layer item. Esri's webmap-spec field name for the same concept. Absent = the layer's
    # own name. Bookmark-name validation posture (shared _clean_label).
    title: str | None = None
    # This map's OWN visible range for the layer (decision-layer-zoom-range.md §3.1): when either is set,
    # the pair replaces the layer's default (its renderer's visibleMinZoom/visibleMaxZoom) as a whole;
    # both absent = use the layer's default, live. Any kind may carry one — externals and rasters have no
    # layer style, so this is their only range. Drawn from min (inclusive) up to, not including, max.
    # Absent on every pre-range map, so they all still validate (jsonb, no migration).
    visibleMinZoom: float | None = Field(default=None, ge=0.0, le=24.0)
    visibleMaxZoom: float | None = Field(default=None, ge=0.0, le=24.0)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str | None) -> str | None:
        return None if v is None else _clean_label(v, 80, "layer title")

    @model_validator(mode="after")
    def _check_ref(self) -> "OperationalLayer":
        if self.kind == "layer" and not self.layerId:
            raise ValueError("a layer operational layer requires layerId")
        if self.kind == "raster" and not self.rasterId:
            raise ValueError("a raster operational layer requires rasterId")
        if self.kind == "external":
            if not self.externalId:
                raise ValueError("an external operational layer requires externalId")
            if self.externalKind is None:
                raise ValueError("an external operational layer requires externalKind")
            if not self.url or not (self.url.startswith("https://") or self.url.startswith("http://")):
                raise ValueError("an external operational layer requires an http(s) url")
        if (
            self.visibleMinZoom is not None
            and self.visibleMaxZoom is not None
            and self.visibleMinZoom >= self.visibleMaxZoom
        ):
            raise ValueError("visibleMinZoom must be below visibleMaxZoom")
        return self


class InitialView(BaseModel):
    center: list[float] = Field(min_length=2, max_length=2)  # [lon, lat]
    zoom: float


class Bookmark(InitialView):
    # A bookmark IS a named InitialView (center [lon,lat] + zoom) — an extent-only saved viewpoint that lives
    # in the web_map definition JSON (no table, no migration; jsonb round-trips it). It is NOT a saved map
    # state: it never snapshots layer visibility/symbology (AGO parity — see docs/decision-bookmarks.md).
    name: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        return _clean_label(v, _MAX_BOOKMARK_NAME, "bookmark name")


class MapTable(BaseModel):
    # A standalone non-spatial table shown ON this map (decision-map-tables.md, part b): a reference to a
    # geometry-less layer (decision-nonspatial-tables.md), listed in the map's Tables section and opened in
    # the docked grid. An object (not a bare id) so later per-map table props (e.g. a saved column layout)
    # slot in without reshaping. Nothing is drawn — deliberately NOT an operationalLayer (no draw order/
    # visibility/opacity semantics to carry).
    layerId: str = Field(min_length=1, max_length=63)


# Sketch caps (decision-sketch-layer.md §2.1): a hand-drawn note never needs more, and the jsonb
# stays bounded — 200 features × 2,000 positions is a generous ceiling, not an invitation.
_MAX_SKETCH = 200
_MAX_SKETCH_POSITIONS = 2000
_MAX_SKETCH_LABEL = 80


class SketchFeature(BaseModel):
    # One AGO-style map note (decision-sketch-layer.md): an ad-hoc point/line/polygon drawn ON this
    # map, with a per-feature color and an optional STATIC text label (one string — the standing
    # no-expression-engine fence). Map-scoped only: never a catalog item, never served, never seen
    # by interop — it lives and dies with this jsonb document. Coordinates are validated for SHAPE
    # and bounded for SIZE, but never touch SQL/PostGIS — they only feed the client's GeoJSON source.
    id: str = Field(pattern=r"^skt_[0-9a-f]{8}$")  # the select/edit key (string id — identify lesson)
    geometryType: Literal["point", "line", "polygon"]
    # GeoJSON coordinates for the type: point = [lon, lat]; line = [[lon, lat], ...];
    # polygon = [[[lon, lat], ...], ...] (rings). Validated structurally below.
    coordinates: Any
    color: str = Field(default="#2a9d8f", pattern=r"^#[0-9a-fA-F]{6}$")
    label: str | None = None

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v: str | None) -> str | None:
        return None if v is None else _clean_label(v, _MAX_SKETCH_LABEL, "sketch label")

    @staticmethod
    def _is_position(p: Any) -> bool:
        return (
            isinstance(p, list) and len(p) == 2
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in p)
            and -180.0 <= p[0] <= 180.0 and -90.0 <= p[1] <= 90.0
        )

    @model_validator(mode="after")
    def _check_coordinates(self) -> "SketchFeature":
        c = self.coordinates
        if self.geometryType == "point":
            if not self._is_position(c):
                raise ValueError("a point sketch requires [lon, lat] coordinates")
            return self
        if self.geometryType == "line":
            if not (isinstance(c, list) and 2 <= len(c) <= _MAX_SKETCH_POSITIONS
                    and all(self._is_position(p) for p in c)):
                raise ValueError(
                    f"a line sketch requires 2..{_MAX_SKETCH_POSITIONS} [lon, lat] positions"
                )
            return self
        # polygon: a list of rings, each 4+ closed-ring positions, bounded in TOTAL.
        if not (isinstance(c, list) and 1 <= len(c) <= 10):
            raise ValueError("a polygon sketch requires 1..10 rings")
        total = 0
        for ring in c:
            if not (isinstance(ring, list) and len(ring) >= 4 and all(self._is_position(p) for p in ring)):
                raise ValueError("each polygon ring requires 4+ [lon, lat] positions (closed)")
            if ring[0] != ring[-1]:
                raise ValueError("each polygon ring must close (first position == last)")
            total += len(ring)
        if total > _MAX_SKETCH_POSITIONS:
            raise ValueError(f"a polygon sketch is capped at {_MAX_SKETCH_POSITIONS} total positions")
        return self


class FieldSettings(BaseModel):
    """Talus Field (decision-talus-field-f2.md §4.4): the map-level "Field-ready" switch. A map property
    (the §6 authoring principle), read by the Talus+ field door as its list filter. Opt-in (fork 3)."""
    enabled: bool = False


class WebMapDefinition(BaseModel):
    version: int = 1
    basemap: str
    # Optional so every pre-f2 saved map still deserializes; absent = not a field map.
    field: FieldSettings | None = None
    operationalLayers: list[OperationalLayer] = Field(max_length=_MAX_LAYERS)
    initialView: InitialView
    # Saved named viewpoints (Map bookmarks). Extent-only; managed by whoever can edit the map, persisted by
    # the existing map-save path. default_factory=[] so every PRE-bookmarks saved map still deserializes.
    bookmarks: list[Bookmark] = Field(default_factory=list, max_length=_MAX_BOOKMARKS)
    # Tables shown on this map (decision-map-tables.md). default_factory=[] keeps every pre-tables map valid.
    tables: list[MapTable] = Field(default_factory=list, max_length=_MAX_TABLES)
    # Map notes (decision-sketch-layer.md): ad-hoc drawn features, map-scoped only. default_factory=[]
    # keeps every pre-sketch map valid; the count cap bounds the jsonb alongside the per-feature caps.
    sketch: list[SketchFeature] = Field(default_factory=list, max_length=_MAX_SKETCH)

    @field_validator("basemap")
    @classmethod
    def _validate_basemap(cls, v: str) -> str:
        if v not in _BASEMAPS:
            raise ValueError(f"basemap must be one of {sorted(_BASEMAPS)}")
        return v


# --- request / response models --------------------------------------------------


class WebMapCreate(BaseModel):
    title: str
    description: str | None = None
    definition: WebMapDefinition

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        return _clean_label(v, _MAX_TITLE_LEN, "title")


class WebMapUpdate(WebMapCreate):
    """Same shape as create — update replaces title/description/definition in place."""


class WebMapSummary(BaseModel):
    id: str
    title: str
    description: str | None
    created_at: Any
    updated_at: Any
    # The map's current sharing level for the at-a-glance catalog share-status icon (Talus 23
    # follow-up): 'public' | 'organization' | 'groups' | 'owner' (owner = private). Computed for
    # BOTH owned AND shared maps (Talus 36 — icon consistency across every row, parity with layers);
    # None on an OpenFGA hiccup (the icon simply doesn't render).
    shareLevel: str | None = None
    # favorite (Talus 24): has the caller starred this map? Parity with layers; drives the ★
    # Favorites filter + the per-row star on the Maps list.
    favorite: bool = False
    # sharedWithMe: is this map in the caller's "shared" bucket via a GENUINE share (not owned)? For a regular
    # caller every shared-bucket map is a genuine share (true). For an ADMIN (who sees ALL maps) the shared
    # bucket also carries admin-override-only maps with sharedWithMe:false, so the catalog "Shared with me" tab
    # shows only genuine shares while "All" still shows everything (admin-sees-all, 2026-06-30).
    sharedWithMe: bool = False
    # deleteProtected (Talus 24 c4): the map's delete-protection flag, surfaced so the catalog row
    # shows the at-a-glance padlock on the delete affordance (parity with layers). The dedicated
    # GET/PUT /web-maps/{id}/delete-protection remains the Manage dialog's read/write surface.
    deleteProtected: bool = False
    # owner (Talus 26): the CURRENT owner's id + display name (mutable — transfer changes them;
    # `created_by` stays the immutable creator). Drives the catalog "owner" column + the "ask the
    # owner" permission notice. ownerName degrades to None if the user row is missing.
    ownerId: str | None = None
    ownerName: str | None = None


class WebMapDetail(WebMapSummary):
    definition: dict
    # The opener's management right (owner-or-admin) — drives read-only gating in the open
    # map so a non-owner who opens a SHARED map sees no Save/Delete/Share. A UI hint only;
    # the real gate is _load_for_manage on PATCH/DELETE/shares. creator == owner in v1.
    canManage: bool = False
    # ANON ONLY (decision-map-embed.md F0): the caller-visible metadata for the map's OWN vector
    # layers, computed server-side with the same fail-closed gate as every anon read
    # (public_object_allowed per layer). An authenticated caller gets None and keeps using
    # /me/layers — zero behavior change. Only layers IN THIS PUBLIC MAP's definition are probed,
    # and only their public subset is returned — no enumeration surface.
    layersMeta: list[dict[str, Any]] | None = None


# --- sharing (Talus 21) ---------------------------------------------------------


class ShareGroup(BaseModel):
    group_id: str
    display_name: str
    # V026 `system` flag (home / open_data): a client may hide these for item kinds that must
    # never reach a public surface (Talus+ reports). The server enforces the refusal regardless.
    system: bool = False


class WebMapShares(BaseModel):
    """Current/updated share state of a web map. `organizationOnly` = shared to the whole org
    (org_shared); `groups` = the additive group shares; `publicAccess` = "Public Access" (the
    anonymous `public_shared` tier, Talus 23) — an INDEPENDENT tier (F5), governed + gated."""
    organizationOnly: bool = False
    groups: list[ShareGroup] = []
    publicAccess: bool = False


class WebMapShareUpdate(BaseModel):
    """Replace a web map's share set. Empty (all false / []) clears all sharing. `groups` are
    platform.groups ids the caller may share to (member-scoped; enforced server-side).
    `publicAccess` turns the anonymous Public Access tier on/off — gated by the F6 deploy switch
    + org policy when turned ON (assert_may_share_public); turning it OFF always allowed."""
    organizationOnly: bool = False
    groups: list[str] = []
    publicAccess: bool = False


class PublicLayerReviewItem(BaseModel):
    """One referenced layer in the make-public cascade (Talus 23 c3b): its current public state and
    whether THIS caller may change it (admins always; owners of the layer; ownerless system layers
    are admin-only). The UI shows non-public layers so the owner can publish them alongside the map."""
    layerId: str
    title: str
    isPublic: bool
    canManage: bool


class PublicRasterReviewItem(BaseModel):
    """One referenced RASTER in the make-public cascade (raster-public slice) — same shape as the
    layer item so the dialog renders both lists with one row component."""
    rasterId: str
    title: str
    isPublic: bool
    canManage: bool


# --- gates ----------------------------------------------------------------------


async def _require_publisher(
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> User:
    """403 unless the caller holds talus-publisher or talus-admin. Creating a web map is item
    creation — same gate as create_layer/create_group (F3)."""
    if not _has_publish_role(claims):
        log.info(
            "authz_rejected", capability="web_maps:create",
            reason="missing_publish_role", user_id=str(user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to create web maps",
        )
    return user


async def _assert_layers_exist(conn: AsyncConnection, definition: WebMapDefinition) -> None:
    """Reject a definition that names a layer/raster not in the registry (400) — so we never persist a
    reference to a phantom item. Access is re-checked at OPEN (F5), not frozen here."""
    layer_ids = [ol.layerId for ol in definition.operationalLayers if ol.kind == "layer" and ol.layerId]
    # Map-table refs (decision-map-tables.md) are layer ids too (a table IS a geometry-less layer) —
    # validated through the same existence check so a phantom table ref never persists.
    layer_ids += [t.layerId for t in definition.tables]
    raster_ids = [ol.rasterId for ol in definition.operationalLayers if ol.kind == "raster" and ol.rasterId]
    if layer_ids:
        async with conn.cursor() as cur:
            await cur.execute("SELECT layer_id FROM platform.layers WHERE layer_id = ANY(%s)", (layer_ids,))
            known = {r[0] for r in await cur.fetchall()}
        unknown = sorted(set(layer_ids) - known)
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown layer(s): {', '.join(unknown)}"
            )
    if raster_ids:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id::text FROM platform.rasters WHERE id = ANY(%s::uuid[])", (raster_ids,))
            known = {r[0] for r in await cur.fetchall()}
        unknown = sorted(set(raster_ids) - known)
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown raster(s): {', '.join(unknown)}"
            )


async def _load_for_manage(
    conn: AsyncConnection, claims: dict[str, Any] | None, user: User, map_id: str
) -> None:
    """Authorize update/delete of a web map: 404 if it doesn't exist, else owner-OR-admin
    (else 403). Admin is role-only and checked FIRST so it short-circuits the OpenFGA call
    (mirrors release_layer). _openfga_check fails closed (503) on an FGA error."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.web_maps WHERE id = %s", (map_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="web map not found")
    if _has_admin_role(claims):
        return
    if await _openfga_check(user.id, "owner", map_id, object_type="web_map"):
        return
    log.info(
        "authz_rejected", capability="web_maps:manage",
        reason="not_owner_not_admin", user_id=str(user.id), web_map=map_id,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to modify this web map"
    )


# --- endpoints ------------------------------------------------------------------


async def _favorited_web_map_ids(conn: AsyncConnection, user_id: Any) -> set[str]:
    """The set of web_map ids the caller has starred (platform.user_favorites, item_type='web_map').
    One query for the whole list; orphan-tolerant (Talus 24)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT item_id FROM platform.user_favorites WHERE user_id = %s AND item_type = 'web_map'",
            (user_id,),
        )
        return {r[0] for r in await cur.fetchall()}


async def _summaries(rows: list[Any], favs: set[str], shared_with_me: bool = False) -> list[WebMapSummary]:
    """Build catalog summaries, each tagged with its current `shareLevel` (broadest tier wins) for
    the share-status icon. Computed for BOTH owned and shared maps (Talus 36 — the icon must be
    consistent across every row, matching the layers list, which already tags every row). One
    OpenFGA read per map — fine at municipal scale. Degrades to shareLevel=None on an OpenFGA error
    (the icon just doesn't render — never fails the list)."""
    out: list[WebMapSummary] = []
    for r in rows:
        try:
            shares = await get_object_shares(f"web_map:{r[0]}")
            level = (
                "public" if shares["publicAccess"]
                else "organization" if shares["organizationOnly"]
                else "groups" if shares["groups"]
                else "owner"
            )
        except HTTPException:
            level = None
        out.append(WebMapSummary(
            id=str(r[0]), title=r[1], description=r[2], created_at=r[3], updated_at=r[4],
            shareLevel=level, favorite=str(r[0]) in favs, deleteProtected=bool(r[5]),
            ownerId=str(r[6]) if r[6] else None, ownerName=r[7], sharedWithMe=shared_with_me,
        ))
    return out


@router.get("/me/web-maps", summary="The caller's web maps — owned + shared (admin: + all others)")
async def my_web_maps(
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, list[WebMapSummary]]:
    # Owned: keyed on owner_id (Talus 26 — ownership is now transferable, distinct from the
    # immutable created_by creator). Still a cheap DB query — no per-row OpenFGA round-trip, and it
    # does not depend on OpenFGA being up (it's the caller's own content). The owner name join is
    # for parity with the shared rows (here it's always the caller).
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT w.id, w.title, w.description, w.created_at, w.updated_at, w.delete_protected, "
            "w.owner_id, COALESCE(u.display_name, u.username, u.email) "
            "FROM platform.web_maps w LEFT JOIN platform.users u ON w.owner_id = u.id "
            "WHERE w.owner_id = %s ORDER BY w.updated_at DESC",
            (user.id,),
        )
        owned_rows = await cur.fetchall()
    owned_ids = {str(r[0]) for r in owned_rows}

    # Shared with me (Talus 21): every web_map the caller can_view, MINUS the ones they own.
    # ListObjects fails closed (503); but this endpoint only DISPLAYS a list, so on an OpenFGA
    # blip we degrade the shared bucket to empty (fail-closed: shows LESS access, never more)
    # rather than 503-ing the whole catalog — owned still renders. Access decisions (open/share)
    # keep their hard 503.
    try:
        viewable = await _openfga_list_objects(user.id, "can_view", "web_map")
        shared_ids = [vid for vid in viewable if vid not in owned_ids]
    except HTTPException:
        log.warning("web_maps_shared_bucket_degraded", user_id=str(user.id))
        shared_ids = []

    _MAP_COLS = (
        "SELECT w.id, w.title, w.description, w.created_at, w.updated_at, w.delete_protected, "
        "w.owner_id, COALESCE(u.display_name, u.username, u.email) "
        "FROM platform.web_maps w LEFT JOIN platform.users u ON w.owner_id = u.id "
    )

    async def _rows_for(ids: list[str]) -> list[Any]:
        if not ids:
            return []
        async with conn.cursor() as cur:
            await cur.execute(_MAP_COLS + "WHERE w.id = ANY(%s) ORDER BY w.updated_at DESC", (ids,))
            return await cur.fetchall()

    shared_rows = await _rows_for(shared_ids)

    # An ADMIN sees EVERY map: the ones neither owned nor genuinely-shared come back in the SHARED bucket but
    # flagged sharedWithMe=false, so the catalog's "All" shows them while "Shared with me" does not.
    admin_rows: list[Any] = []
    if _has_admin_role(claims):
        seen = owned_ids | set(shared_ids)
        async with conn.cursor() as cur:
            await cur.execute(_MAP_COLS + "WHERE NOT (w.id::text = ANY(%s)) ORDER BY w.updated_at DESC",
                              (list(seen),))
            admin_rows = await cur.fetchall()

    favs = await _favorited_web_map_ids(conn, user.id)
    return {
        "owned": await _summaries(owned_rows, favs),
        # genuine shares (sharedWithMe=true) + admin-override-only maps (sharedWithMe=false)
        "shared": await _summaries(shared_rows, favs, shared_with_me=True)
        + await _summaries(admin_rows, favs, shared_with_me=False),
    }


@router.get("/me/shareable-groups", summary="Groups the caller may share a map to")
async def my_shareable_groups(
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, list[ShareGroup]]:
    """The share dialog's group picker. A caller may share to the groups they are a MEMBER of
    (so a Signs-team publisher can't share to Street Lights); an admin may share to any group.
    Group names aren't sensitive, so this is open to any authenticated user — the real gate is
    the membership check in PUT /web-maps/{id}/shares (this just scopes the UI)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT group_id, display_name, system FROM platform.groups ORDER BY display_name"
        )
        rows = await cur.fetchall()
        registry = {r[0]: r[1] for r in rows}
        system = {r[0] for r in rows if r[2]}

    if _has_admin_role(claims):
        chosen = sorted(registry.items(), key=lambda kv: kv[1].lower())
    else:
        # _openfga_read_user_groups fails closed (503); for a non-admin we let that propagate —
        # better an honest "try again" than silently offering no groups as if they had none.
        member_slugs = set(await _openfga_read_user_groups(user.id))
        chosen = sorted(
            ((g, n) for g, n in registry.items() if g in member_slugs),
            key=lambda kv: kv[1].lower(),
        )
    return {"groups": [ShareGroup(group_id=g, display_name=n, system=g in system) for g, n in chosen]}


@router.post("/web-maps", status_code=status.HTTP_201_CREATED, summary="Save a web map (publisher only)")
async def create_web_map(
    payload: WebMapCreate,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(_require_publisher),
) -> WebMapDetail:
    await _assert_layers_exist(conn, payload.definition)
    map_id = uuid4()
    actor = user.id

    # OpenFGA FIRST — the owner grant. A failure here raises 503 and NO row is written (the map
    # never half-exists). ALWAYS fires (no stub short-circuit): an unowned map must fail closed.
    await _openfga_write([(f"user:{actor}", "owner", f"web_map:{map_id}")])

    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO platform.web_maps "
            "(id, title, description, definition, created_by, updated_by, owner_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING created_at, updated_at",
            (str(map_id), payload.title, payload.description,
             Jsonb(payload.definition.model_dump()), actor, actor, actor),
        )
        ts = await cur.fetchone()
    await conn.commit()
    log.info("web_map_created", web_map=str(map_id), actor=str(actor))
    return WebMapDetail(
        id=str(map_id), title=payload.title, description=payload.description,
        definition=payload.definition.model_dump(), created_at=ts[0], updated_at=ts[1],
        canManage=True,  # the creator owns it
    )


@router.get("/web-maps/{map_id}", summary="Open a web map (can_view, or anonymous if public)")
async def get_web_map(
    map_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_optional_claims),
    principal: User | None = Depends(get_optional_user),
) -> WebMapDetail:
    # Optional-auth (Talus 23): an AUTHENTICATED caller is gated by can_view (403 on fail); an
    # ANONYMOUS caller passes ONLY if the map is public_shared (404 on fail — existence-hiding).
    # Both fail closed (503 on OpenFGA error). The stored definition is returned as-is; the
    # frontend resolves which referenced layers the caller can actually see and skips the rest
    # (F5 — a non-public layer on a public map simply doesn't render).
    if principal is not None:
        # talus-admin reads every map (god-mode, parity with layers' admin-sees-all); else gate on can_view.
        if not principal.is_admin and not await _openfga_check(
            principal.id, "can_view", map_id, object_type="web_map"
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to view this web map"
            )
    else:
        if not await public_object_allowed(conn, map_id, object_type="web_map"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="web map not found")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, title, description, definition, created_at, updated_at, created_by "
            "FROM platform.web_maps WHERE id = %s",
            (map_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="web map not found")
    # canManage = owner-or-admin. NEVER true for an anonymous caller. A token caller (a principal
    # with None claims) is an admin to nobody: _has_admin_role answers True for None in stub mode
    # only (once-over fix A3), so for them this is ownership alone.
    can_manage = principal is not None and (
        _has_admin_role(claims) or str(row[6]) == str(principal.id)
    )
    await last_accessed.touch(conn, "web_map", map_id)  # stale-content signal (throttled, best-effort)
    layers_meta = None if principal is not None else await _anon_layers_meta(conn, row[3])
    return WebMapDetail(
        id=str(row[0]), title=row[1], description=row[2], definition=row[3],
        created_at=row[4], updated_at=row[5], canManage=can_manage, layersMeta=layers_meta,
    )


async def _anon_layers_meta(conn: AsyncConnection, definition: dict) -> list[dict[str, Any]]:
    """The F0 seam (decision-map-embed.md): an anonymous opener of a PUBLIC map can't call
    /me/layers, so the map doc carries the metadata for its own vector layers — the same
    MyLayer-shaped fields the SPA's restore path consumes (buildSlotLayerDef/buildViewLayerDef
    inputs), for exactly the PUBLIC subset of the layers this map references. Per-layer gate =
    public_object_allowed, the direct public_shared probe every anon read uses (fail-closed;
    a non-public layer is simply absent, so the SPA's F5 skip counts it as unavailable — the
    same outcome an authed non-viewer gets). Read-only affordances by construction: canEdit/
    canManage are hardwired false for anon."""
    ops = definition.get("operationalLayers") or []
    tables = definition.get("tables") or []
    lids = {o.get("layerId") for o in ops if isinstance(o, dict) and o.get("layerId")}
    lids |= {t.get("layerId") for t in tables if isinstance(t, dict) and t.get("layerId")}
    if not lids:
        return []
    public_ids = [lid for lid in sorted(lids)
                  if await public_object_allowed(conn, lid, object_type="layer")]
    if not public_ids:
        return []
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT layer_id, display_name, display_color, renderer, geom_type, "
            "       native_storage, working_srid, updated_at "
            "FROM platform.layers WHERE layer_id = ANY(%s)",
            (public_ids,),
        )
        rows = await cur.fetchall()
    return [{
        "id": r[0], "canView": True, "canEdit": False, "canManage": False, "owner": False,
        "favorite": False, "editable": False, "deleteProtected": False,
        "displayName": r[1], "displayColor": r[2], "renderer": r[3],
        "geomType": r[4] or "point", "nativeStorage": bool(r[5]), "workingSrid": r[6],
        "renderMode": "features" if str(r[0]).startswith("vw_") else "tiles",
        "updated_at": r[7].isoformat() if r[7] is not None else None,
    } for r in rows]


@router.patch("/web-maps/{map_id}", summary="Update a web map in place (owner or admin)")
async def update_web_map(
    map_id: str,
    payload: WebMapUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> WebMapDetail:
    await _load_for_manage(conn, claims, user, map_id)
    await _assert_layers_exist(conn, payload.definition)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.web_maps SET title = %s, description = %s, definition = %s, "
            "updated_by = %s, updated_at = now() WHERE id = %s RETURNING created_at, updated_at",
            (payload.title, payload.description, Jsonb(payload.definition.model_dump()),
             user.id, map_id),
        )
        ts = await cur.fetchone()
    await conn.commit()
    log.info("web_map_updated", web_map=map_id, actor=str(user.id))
    return WebMapDetail(
        id=map_id, title=payload.title, description=payload.description,
        definition=payload.definition.model_dump(), created_at=ts[0], updated_at=ts[1],
        canManage=True,  # passed _load_for_manage above (owner or admin)
    )


@router.delete("/web-maps/{map_id}", summary="Delete a web map (owner or admin)")
async def delete_web_map(
    map_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    await _load_for_manage(conn, claims, user, map_id)
    # Delete protection (Talus 24): a protected map can't be deleted by ANYONE — owner OR admin —
    # until protection is turned off (ABSOLUTE; AGO-faithful, guards human error incl.
    # an admin's). No admin override; an admin still holds the toggle, so it's a deliberate two-step
    # (untick → delete), never a one-click force. Guard-rail against accidental loss; actionable msg.
    async with conn.cursor() as cur:
        await cur.execute("SELECT delete_protected FROM platform.web_maps WHERE id = %s", (map_id,))
        prot = await cur.fetchone()
    if prot and prot[0]:
        log.info("web_map_delete_blocked", web_map=map_id, actor=str(user.id), reason="delete_protected")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This map is protected from deletion. Turn off delete protection "
            "(Manage → Delete protection) before deleting it.",
        )
    # OpenFGA FIRST — delete every tuple on the object (owner + any future shares), then the row.
    # Fail-safe inverse of create: a crash after the tuple delete leaves a row an admin can re-delete.
    tuples = await _openfga_read_object_tuples(f"web_map:{map_id}")
    await _openfga_delete(tuples)
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM platform.web_maps WHERE id = %s", (map_id,))
        # Clear its metadata record (Talus 31) so a deleted map leaves no orphan.
        await cur.execute(
            "DELETE FROM platform.item_metadata WHERE item_type = 'web_map' AND item_id = %s",
            (map_id,),
        )
    await conn.commit()
    log.info("web_map_deleted", web_map=map_id, actor=str(user.id), tuples_deleted=len(tuples))
    return {"id": map_id, "status": "deleted"}


# --- sharing endpoints (Talus 21; general per-object seam, F6) -------------------


async def _shares_response(conn: AsyncConnection, map_id: str) -> WebMapShares:
    """Read the web map's current share state and resolve group display names for the UI."""
    shares = await get_object_shares(f"web_map:{map_id}")
    slugs: list[str] = shares["groups"]
    names: dict[str, str] = {}
    if slugs:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT group_id, display_name FROM platform.groups WHERE group_id = ANY(%s)",
                (slugs,),
            )
            names = {r[0]: r[1] for r in await cur.fetchall()}
    return WebMapShares(
        organizationOnly=shares["organizationOnly"],
        groups=[ShareGroup(group_id=s, display_name=names.get(s, s)) for s in slugs],
        publicAccess=shares["publicAccess"],
    )


@router.get("/web-maps/{map_id}/shares", summary="Current share state (owner or admin)")
async def get_web_map_shares(
    map_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> WebMapShares:
    # Only someone who can MANAGE the map (owner or admin) may see/set how it's shared —
    # recipients don't need its share config. 404 if absent, 403 if not owner/admin.
    await _load_for_manage(conn, claims, user, map_id)
    return await _shares_response(conn, map_id)


@router.put("/web-maps/{map_id}/shares", summary="Set/replace share state (owner or admin)")
async def set_web_map_shares(
    map_id: str,
    payload: WebMapShareUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> WebMapShares:
    await _load_for_manage(conn, claims, user, map_id)  # owner-or-admin; 404/403

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
        # can't surface a map to Street Lights. _openfga_read_user_groups fails closed (503).
        if not _has_admin_role(claims):
            member_of = set(await _openfga_read_user_groups(user.id))
            forbidden = [g for g in requested if g not in member_of]
            if forbidden:
                log.info(
                    "authz_rejected", capability="web_maps:share",
                    reason="group_not_member", user_id=str(user.id),
                    web_map=map_id, groups=forbidden,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"not authorized to share to group(s): {', '.join(sorted(forbidden))}",
                )

    # Open Data public invariant (Talus 33 §F8): the public catalog enumerates `group:open_data`, so
    # a request adding it must ALSO turn Public Access ON (bound here so it can't be bypassed). Home
    # is deliberately NOT coupled — a featured map may be org-only OR public, and the featured
    # gallery filters per-viewer so org-only items never reach anon (see authz.py PUBLIC_SYSTEM_GROUPS).
    coupled = sorted(PUBLIC_SYSTEM_GROUPS & set(requested))
    if coupled and not payload.publicAccess:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"sharing to {', '.join(coupled)} requires public access",
        )

    # Open Data metadata gate (Talus 33 §F8) — bound here too so the generic share path can't slip an
    # undocumented item into Open Data (maps aren't catalog-listed in v1, but the invariant stays
    # uniform; `metadata_complete` is type-generic). Removing `open_data` is never gated.
    if OPEN_DATA_GROUP in requested:
        complete, missing = await metadata_complete(conn, "web_map", map_id)
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
        f"web_map:{map_id}", organization_only=payload.organizationOnly, groups=requested
    )
    public_changed = await set_object_public(f"web_map:{map_id}", public=payload.publicAccess)
    log.info(
        "web_map_shares_set", web_map=map_id, actor=str(user.id),
        organization_only=payload.organizationOnly, groups=requested, public=payload.publicAccess,
    )
    if public_changed:
        # A distinct, loud audit line for the irreversible-exposure transition (decision §F3).
        log.info(
            "object_public_changed", object=f"web_map:{map_id}", actor=str(user.id),
            public=payload.publicAccess,
        )
    return await _shares_response(conn, map_id)


@router.get(
    "/web-maps/{map_id}/public-layer-review",
    summary="Per-layer public status for the make-public cascade (owner or admin)",
)
async def web_map_public_layer_review(
    map_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, list[Any]]:
    """The AGO "Review sharing" data (Talus 23 c3b): for each layer the map references, its current
    public state + whether the caller may make it public. The dialog uses this to prompt publishing
    the map's non-public layers (each its own `public_shared`, F5) so a public map isn't silently
    blank. Owner-or-admin gated (the share authority). Per-layer FGA reads — fine at municipal scale."""
    await _load_for_manage(conn, claims, user, map_id)  # owner-or-admin; 404/403
    async with conn.cursor() as cur:
        await cur.execute("SELECT definition FROM platform.web_maps WHERE id = %s", (map_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="web map not found")
    definition = row[0] or {}
    ops = definition.get("operationalLayers", [])
    # De-dupe ids, preserve first-seen order. Vector ops key on layerId; raster ops (raster-public
    # slice) on rasterId — both join the review so "make map public" never silently strands an op.
    ordered: list[str] = list(
        dict.fromkeys(ol.get("layerId") for ol in ops if ol.get("kind", "layer") == "layer" and ol.get("layerId"))
    )
    raster_ids: list[str] = list(
        dict.fromkeys(ol.get("rasterId") for ol in ops if ol.get("kind") == "raster" and ol.get("rasterId"))
    )
    items: list[PublicLayerReviewItem] = []
    if ordered:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT layer_id, display_name FROM platform.layers WHERE layer_id = ANY(%s)", (ordered,)
            )
            titles = {r[0]: r[1] for r in await cur.fetchall()}
        for lid in ordered:
            if lid not in titles:  # referenced layer no longer registered — skip (can't be shared)
                continue
            shares = await get_object_shares(f"layer:{lid}")
            items.append(
                PublicLayerReviewItem(
                    layerId=lid,
                    title=titles[lid] or lid,
                    isPublic=shares["publicAccess"],
                    canManage=await manage_allowed(claims, user, lid),
                )
            )
    raster_items: list[PublicRasterReviewItem] = []
    if raster_ids:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id::text, title FROM platform.rasters WHERE id = ANY(%s::uuid[])", (raster_ids,)
            )
            rtitles = {r[0]: r[1] for r in await cur.fetchall()}
        for rid in raster_ids:
            if rid not in rtitles:  # referenced raster gone — skip
                continue
            shares = await get_object_shares(f"raster:{rid}")
            # Raster manage = admin or OpenFGA owner (manage_allowed is layer-specific).
            can_manage = _has_admin_role(claims) or await _openfga_check(
                user.id, "owner", rid, object_type="raster"
            )
            raster_items.append(
                PublicRasterReviewItem(
                    rasterId=rid, title=rtitles[rid] or rid,
                    isPublic=shares["publicAccess"], canManage=can_manage,
                )
            )
    return {"layers": items, "rasters": raster_items}


# --- delete protection (Talus 24) -----------------------------------------------
#
# An owner-settable guard-rail: a protected map can't be deleted by its (non-admin) owner —
# delete_web_map enforces it above. Read/write is owner-or-admin (the manage authority), surfaced in
# the catalog's "Manage" dialog. The layer analog lives in layer_shares.py.


class DeleteProtection(BaseModel):
    deleteProtected: bool = False


@router.get("/web-maps/{map_id}/delete-protection", summary="A map's delete-protection flag (owner or admin)")
async def get_web_map_delete_protection(
    map_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> DeleteProtection:
    await _load_for_manage(conn, claims, user, map_id)  # owner-or-admin; 404/403
    async with conn.cursor() as cur:
        await cur.execute("SELECT delete_protected FROM platform.web_maps WHERE id = %s", (map_id,))
        row = await cur.fetchone()
    return DeleteProtection(deleteProtected=bool(row[0]))


@router.put("/web-maps/{map_id}/delete-protection", summary="Set a map's delete-protection flag (owner or admin)")
async def set_web_map_delete_protection(
    map_id: str,
    payload: DeleteProtection,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> DeleteProtection:
    await _load_for_manage(conn, claims, user, map_id)  # owner-or-admin; 404/403
    # Metadata flip only — deliberately does NOT bump updated_at (toggling protection isn't editing
    # the map's content, so it shouldn't reorder the catalog's Modified sort).
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.web_maps SET delete_protected = %s WHERE id = %s",
            (payload.deleteProtected, map_id),
        )
    await conn.commit()
    log.info(
        "web_map_delete_protection_set", web_map=map_id, actor=str(user.id),
        protected=payload.deleteProtected,
    )
    await admin_audit.record(
        conn, actor_id=user.id, action="web_map_delete_protection_set", target_type="web_map",
        target_id=map_id, detail={"protected": payload.deleteProtected},
    )
    return DeleteProtection(deleteProtected=payload.deleteProtected)
