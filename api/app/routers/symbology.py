"""Layer-default symbology: distinct field values (for the Styles panel) + set/clear the
renderer (Talus 13).

Two concerns, both layer-LEVEL (not per-feature):

  GET  /layers/{layer_id}/field-values/{field}
       The capped distinct-value scan that populates the unique-value picker for a
       NON-coded field (coded fields get their richer ordered/labelled list from the
       existing /domains route). View-gated. Values come back as text and NULL is
       excluded — the «No value» bucket is the renderer's default, not a scanned value.

  PUT    /layers/{layer_id}/renderer   — set the layer-default renderer (owner/admin)
  DELETE /layers/{layer_id}/renderer   — clear it back to single-symbol fallback (owner/admin)

Injection discipline (mirrors layer_features.py / publisher.py): layer_id and field both
NAME DATABASE OBJECTS. Neither is ever interpolated raw. layer_id must match one of the two
server-generated id shapes (`tal_<8 hex>` slot / `vw_<12 hex>` view — the ONLY registry
populations since the pilot built-ins retired, decision-retire-builtins.md phase 2) AND be a
platform.layers row; field is validated against information_schema for that relation. All of
that happens BEFORE either is wrapped in psycopg.sql.Identifier (defense-in-depth) — every
value is a bound parameter. Unlike the editing doors this gate admits `vw_*`: a view's owner
styles it in the Styles panel exactly like a slot (the map paints views by renderer too), so
a slot-only gate here would silently break view-layer styling — the same class of error as
the search.py non-slot branch (decision doc §2.3).
The relation name equals the layer_id in the assets schema uniformly (tal_xxx ->
assets.tal_xxx, vw_xxx -> assets.vw_xxx).
"""
import re
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb
from pydantic import Field, BaseModel, field_validator, model_validator

from ..auth import _validated_claims, get_current_user
from ..authz import _has_admin_role, _openfga_check, manage_allowed, view_allowed
from ..db import get_db
from ..models.user import User
from ..point_symbols import POINT_SYMBOL_NAMES
from .layer_query import _view_source_fields

log = structlog.get_logger(__name__)

router = APIRouter(tags=["symbology"])

_AUDIT_CLIENT = "portal-api"

# Distinct-value scan ceiling — matches AGOL. We pull one extra to detect truncation, then
# trim. The UI soft-warns above ~50; this hard cap only bounds the scan so an unbounded
# free-text field can't force an unbounded one. The DEFAULT is sized for the symbology
# unique-value picker; the FILTER value-suggestion list (FilterPanel, a datalist that filters
# as you type) asks for more via ?limit=, bounded by _VALUE_CAP_MAX.
_VALUE_CAP = 200
_VALUE_CAP_MAX = 2000

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# The two server-generated layer-id shapes — a publisher slot and a Talus 38 view. Since the
# pilot built-ins retired (decision-retire-builtins.md phase 2) these are the ONLY strings
# platform.layers can hold, so anything else 404s in _require_registered before it could ever
# name a relation. Belt to the registry check's braces, same defense-in-depth as the slot routes.
_SLOT_RE = re.compile(r"^tal_[0-9a-f]{8}$")
_VIEW_RE = re.compile(r"^vw_[0-9a-f]{12}$")

# Names that are never symbology candidates: geometry/id (unique per feature), the
# audit/system/provenance spine, and ESRI import lineage. The lineage names are NOT
# historical residue: imported layers re-create them as owner-defined custom fields
# (the county twin carries all eight), and a `shape_area` is a useless symbology choice
# whether it lives in a column or in props. (View access already exposes attributes via
# popups, so this is UX/consistency hygiene, not an access boundary.)
_NON_SYMBOLOGY_COLUMNS = frozenset(
    {
        "geom", "id", "created_at", "created_by", "updated_at", "updated_by",
        "ai_provenance",
        # ESRI import lineage (live as custom fields on imported layers)
        "source_objectid", "source_globalid", "source_creator", "source_create_date",
        "source_last_update", "source_last_editor", "shape_leng", "shape_area",
    }
)


# --- renderer request model -----------------------------------------------------


def _check_hex(v: str) -> str:
    if not _HEX_RE.fullmatch(v):
        raise ValueError("color must be a 7-char hex like '#1f77b4'")
    return v


class SimpleSymbol(BaseModel):
    color: str

    _hex = field_validator("color")(_check_hex)


class RendererStop(BaseModel):
    value: str
    color: str
    label: str | None = None

    _hex = field_validator("color")(_check_hex)


class RendererDefault(BaseModel):
    color: str
    label: str | None = None

    _hex = field_validator("color")(_check_hex)


class Renderer(BaseModel):
    """The portable renderer spec (mirrors V022 / decision-13). Validated here beyond the
    DB's gross-shape CHECK: a simple renderer needs a symbol; a uniqueValue renderer needs
    a field + at least one stop. `field`'s existence on the layer is checked in the handler
    (it needs the DB)."""

    type: Literal["simple", "uniqueValue"]
    symbol: SimpleSymbol | None = None
    field: str | None = None
    stops: list[RendererStop] | None = None
    default: RendererDefault | None = None
    minZoom: float | None = Field(default=None, ge=0, le=24)  # icon handover zoom (wired 2026-09-03)
    # c7 (decision-line-polygon-layers.md §8) — LAYER-WIDE geometry style, independent of simple/uniqueValue
    # (only colour varies by category). The map applies these per geometry; absent => today's defaults.
    # Line: width + optional dash. Polygon: fill opacity + outline colour/width. Ignored for point layers.
    lineWidth: float | None = None
    lineDash: list[float] | None = None
    fillOpacity: float | None = None
    outlineColor: str | None = None
    outlineWidth: float | None = None
    # POINT size as the rendered DIAMETER in px (ArcGIS/QGIS both mean diameter by "size", and the
    # QField marker `size` is a diameter) — the client halves it for MapLibre's circle-radius.
    # Absent => the zoom ramp points have always used.
    pointSize: float | None = None
    # The bundled marker name (decision-point-icons.md); absent => the plain circle points have
    # always drawn. Validated by SET MEMBERSHIP below, never by shape.
    pointIcon: str | None = None

    @field_validator("outlineColor")
    @classmethod
    def _outline_hex(cls, v: str | None) -> str | None:
        return None if v is None else _check_hex(v)

    @field_validator("lineWidth", "outlineWidth")
    @classmethod
    def _width(cls, v: float | None) -> float | None:
        if v is not None and not (0 <= v <= 50):
            raise ValueError("width must be between 0 and 50")
        return v

    @field_validator("pointSize")
    @classmethod
    def _point_size(cls, v: float | None) -> float | None:
        if v is not None and not (1 <= v <= 96):
            raise ValueError("pointSize must be between 1 and 96")
        return v

    @field_validator("pointIcon")
    @classmethod
    def _point_icon(cls, v: str | None) -> str | None:
        """A CLOSED allowlist, generated from the same curated.txt as the client manifest
        (decision-point-icons.md §6.1). This value reaches the client as a MapLibre `icon-image`
        key and as a lookup into the bundled symbol set — a shape check (regex, length cap) would
        still let an unknown name through, so membership is the only acceptable test."""
        if v is None:
            return None
        if v not in POINT_SYMBOL_NAMES:
            raise ValueError("pointIcon must be one of the bundled point symbols")
        return v

    @field_validator("fillOpacity")
    @classmethod
    def _opacity(cls, v: float | None) -> float | None:
        if v is not None and not (0 <= v <= 1):
            raise ValueError("fillOpacity must be between 0 and 1")
        return v

    @field_validator("lineDash")
    @classmethod
    def _dash(cls, v: list[float] | None) -> list[float] | None:
        if v is not None:
            if not (1 <= len(v) <= 8):
                raise ValueError("lineDash must have 1 to 8 numbers")
            if any(x < 0 or x > 100 for x in v):
                raise ValueError("lineDash values must be between 0 and 100")
        return v

    @model_validator(mode="after")
    def _shape(self) -> "Renderer":
        if self.type == "simple":
            if self.symbol is None:
                raise ValueError("simple renderer requires `symbol`")
        else:  # uniqueValue
            if not self.field:
                raise ValueError("uniqueValue renderer requires `field`")
            if not self.stops:
                raise ValueError("uniqueValue renderer requires at least one stop")
        return self

    def _style(self) -> dict[str, Any]:
        """The layer-wide geometry style keys that are set (non-null) — appended to either renderer shape."""
        s = {
            "lineWidth": self.lineWidth, "lineDash": self.lineDash,
            "fillOpacity": self.fillOpacity, "outlineColor": self.outlineColor,
            "outlineWidth": self.outlineWidth, "pointSize": self.pointSize,
            "pointIcon": self.pointIcon,
            # Icon handover zoom (wired 2026-09-03): layer-wide like pointSize/pointIcon, so BOTH
            # renderer shapes persist it — it lived only in the uniqueValue dict before, which
            # silently dropped it for simple renderers.
            "minZoom": self.minZoom,
        }
        return {k: v for k, v in s.items() if v is not None}

    def to_storage(self) -> dict[str, Any]:
        """Canonical JSON to persist — only the keys relevant to the type, so a simple
        renderer never stores empty stops and vice-versa; plus any layer-wide style keys (c7)."""
        if self.type == "simple":
            return {"type": "simple", "symbol": self.symbol.model_dump(), **self._style()}
        return {
            "type": "uniqueValue",
            "field": self.field,
            "default": self.default.model_dump() if self.default else None,
            "stops": [s.model_dump() for s in (self.stops or [])],
            **self._style(),
        }


class FieldValuesResponse(BaseModel):
    layer_id: str
    field: str
    values: list[str]
    truncated: bool


class SymbolizableField(BaseModel):
    field: str
    type: str  # information_schema data_type: text/boolean/date/integer/numeric/...
    # An OWNER-DEFINED coded field's bound V046 domain id (uuid) — the same customDomain
    # marker /columns rides. (The retired built-ins' FK-table system had a separate `domain`
    # marker, gone with them.)
    customDomain: str | None = None


class SymbolizableFieldsResponse(BaseModel):
    layer_id: str
    fields: list[SymbolizableField]


# --- shared guards --------------------------------------------------------------


async def _require_registered(conn: AsyncConnection, layer_id: str) -> None:
    """layer_id must be shaped like a slot or view id AND be a row in platform.layers, else
    404 (existence-hiding, matching the registry miss). The shape gate runs first — the
    registry check binds layer_id as a parameter, and together they make it one of a fixed
    known-safe set before it names a relation."""
    if not (_SLOT_RE.fullmatch(layer_id) or _VIEW_RE.fullmatch(layer_id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.layers WHERE layer_id = %s", (layer_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")


async def _require_manage(claims: dict | None, user: User, layer_id: str) -> str:
    """The manage-level gate (set/clear renderer), identical in spirit to release-layer.
    Returns the auth path for logs, or 403."""
    if _has_admin_role(claims):
        return "admin"
    if await _openfga_check(user.id, "owner", layer_id):
        return "owner"
    log.info("authz_rejected", capability=f"{layer_id}:manage", reason="not_owner_not_admin", user_id=str(user.id))
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to manage this layer")


async def _require_view_or_manage(claims: dict | None, user: User, layer_id: str) -> None:
    """field-values gate: anyone who can VIEW the layer (the values are visible in popups
    already) OR who can MANAGE it (a styler building a renderer needs them, even on the
    pure-admin path where no view tuple exists). Either suffices — neither is an escalation."""
    if await view_allowed(user, layer_id):
        return
    if await manage_allowed(claims, user, layer_id):
        return
    log.info("authz_rejected", capability=f"{layer_id}:field-values", reason="no_view_no_manage", user_id=str(user.id))
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"not authorized to read {layer_id}")


async def _is_props_field(conn: AsyncConnection, layer_id: str, field: str) -> bool:
    """Is `field` an ACTIVE owner-defined field of this layer (platform.layer_fields)? Both
    arguments stay bound parameters; a hit means the scan reads props ->> key (key bound too)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM platform.layer_fields WHERE layer_id = %s AND field_key = %s AND active",
            (layer_id, field),
        )
        return await cur.fetchone() is not None


async def _require_field(conn: AsyncConnection, layer_id: str, field: str) -> None:
    """field must be symbolizable on this layer, else 422: either an ACTIVE owner-defined
    field (value at props ->> key) or a real, symbolizable column of assets.<layer_id>
    validated against information_schema (parameterized) before it is ever wrapped in an
    Identifier.

    The props branch mirrors what GET /field-values has always done (and
    what tiles_dyn does when it bakes attributes): a slot layer keeps its owner-defined
    fields in `props`, so an information_schema-only test made every custom field
    unsymbolizable — a hosted layer could be given a unique-value renderer on `row_version`
    but not on its own `condition_code`. A name that isn't an owner-defined field falls
    through to the identifier path unchanged. No new injection surface: a props hit means
    the KEY binds as a parameter and is never an identifier."""
    # The denylist runs FIRST so this gate and GET /layers/{id}/fields agree exactly: a name
    # that list refuses to offer is a name this gate refuses to accept, whether it lives in a
    # column or in props. (Hygiene, not an access boundary — view already exposes attributes.)
    if field in _NON_SYMBOLOGY_COLUMNS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"field '{field}' is not symbolizable")
    if await _is_props_field(conn, layer_id, field):
        return
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'assets' AND table_name = %s AND column_name = %s",
            (layer_id, field),
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"no field '{field}' on {layer_id}")


def _assets_table(layer_id: str) -> sql.Identifier:
    """assets.<layer_id> — ONLY after _require_registered has proven layer_id is shaped
    right and is a known registry row. The relation name equals the layer_id uniformly
    across slot + view layers."""
    return sql.Identifier("assets", layer_id)


async def _set_audit_context(cur: Any, actor: Any) -> None:
    await cur.execute("SELECT set_config('app.current_user_id', %s, true)", (str(actor),))
    await cur.execute("SELECT set_config('app.current_client', %s, true)", (_AUDIT_CLIENT,))


# --- routes ---------------------------------------------------------------------


@router.get(
    "/layers/{layer_id}/fields",
    response_model=SymbolizableFieldsResponse,
    summary="Symbolizable fields of a layer, with coded-field domains (view or manage)",
)
async def symbolizable_fields(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> SymbolizableFieldsResponse:
    """DB-introspected list of fields a layer can be symbolized on (Talus 13). Single source
    of truth: information_schema for the columns (minus the never-symbolizable denylist) plus
    the layer's ACTIVE owner-defined fields. This stays correct for any slot or view layer
    without any frontend change — the panel never hardcodes a field list."""
    await _require_registered(conn, layer_id)
    await _require_view_or_manage(claims, user, layer_id)

    async with conn.cursor() as cur:
        # Columns of the layer's table (layer_id == table name in assets, bound param).
        await cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'assets' AND table_name = %s ORDER BY ordinal_position",
            (layer_id,),
        )
        cols = await cur.fetchall()
    # No FK-domain lookup: the only layers whose coded columns were real foreign keys were the
    # pilot built-ins, retired in decision-retire-builtins.md phase 2. An owner-defined coded
    # field carries `customDomain` instead, appended below.
    fields = [
        SymbolizableField(field=name, type=dtype)
        for (name, dtype) in cols
        if name not in _NON_SYMBOLOGY_COLUMNS
    ]

    # Owner-defined fields live in `props`, so information_schema cannot see them. Without
    # this a hosted layer offered only its spine columns and could never be symbolized on
    # its own attributes. Ordered as the field designer orders them, appended after the
    # real columns. `props` itself is dropped once its members are listed individually —
    # the raw jsonb was never a useful symbology choice.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT field_key, data_type, domain_id::text FROM platform.layer_fields "
            "WHERE layer_id = %s AND active ORDER BY sort_order, field_key",
            (layer_id,),
        )
        custom = await cur.fetchall()
    # The raw `props` jsonb is never a useful symbology choice, so it goes whether or not
    # the layer has custom fields yet (a slot layer with none would otherwise still offer it).
    fields = [f for f in fields if f.field != "props"]
    if custom:
        # The denylist applies to owner-defined fields too. It is not about storage: a field
        # named `shape_area` or `source_globalid` is import lineage and a useless symbology
        # choice whether it lives in a column or in props — the county twin re-creates
        # those exact names as custom fields.
        fields.extend(
            SymbolizableField(field=key, type=dtype, customDomain=domain_id)
            for (key, dtype, domain_id) in custom
            if key not in _NON_SYMBOLOGY_COLUMNS
        )

    # A VIEW's props-derived computed columns (decision-view-props-fields.md fork B) introspect
    # as plain typed columns above, so a coded one would fall to the raw-value scan (#136's gap
    # class — view owners style views). Resolve the SOURCE's field defs for the keys the view
    # exposes and re-attach customDomain; the V044 reserved-key CHECK makes a name match
    # unambiguous.
    view_fields = await _view_source_fields(conn, layer_id)
    if view_fields:
        fields = [
            SymbolizableField(field=f.field, type=f.type, customDomain=view_fields[f.field][1])
            if f.field in view_fields and view_fields[f.field][1] is not None
            else f
            for f in fields
        ]
    return SymbolizableFieldsResponse(layer_id=layer_id, fields=fields)


@router.get(
    "/layers/{layer_id}/field-values/{field}",
    response_model=FieldValuesResponse,
    summary="Distinct values of a field (capped) for the unique-value picker (view)",
)
async def field_values(
    layer_id: str,
    field: str,
    limit: int = Query(default=_VALUE_CAP, ge=1, le=_VALUE_CAP_MAX),
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> FieldValuesResponse:
    await _require_registered(conn, layer_id)
    await _require_view_or_manage(claims, user, layer_id)

    # Owner-defined (props) fields are first-class here too (decision-geometry-calc.md flagged
    # item 2): the value lives at props ->> key with the KEY BOUND — never an identifier — the
    # same pattern as the g2 filter/sort grammar. Checked BEFORE the real-column gate; a name
    # that isn't an owner-defined field (a spine or view column) falls through to the
    # identifier path unchanged.
    if await _is_props_field(conn, layer_id, field):
        query = sql.SQL(
            "SELECT DISTINCT (props ->> %(key)s) AS value FROM {tbl} "
            "WHERE props ->> %(key)s IS NOT NULL ORDER BY 1 LIMIT %(lim)s"
        ).format(tbl=_assets_table(layer_id))
        params: dict[str, Any] = {"key": field, "lim": limit + 1}
    else:
        await _require_field(conn, layer_id, field)
        # DISTINCT <field>::text, NULL excluded (the «No value» bucket is the renderer default,
        # not a scanned value). LIMIT limit+1 detects truncation; identifiers are safe (validated
        # + quoted), the cap is a bound parameter. `limit` defaults to the symbology-sized cap;
        # the filter suggestion list requests up to _VALUE_CAP_MAX (Query-validated above).
        query = sql.SQL(
            "SELECT DISTINCT {col}::text AS value FROM {tbl} "
            "WHERE {col} IS NOT NULL ORDER BY 1 LIMIT %(lim)s"
        ).format(col=sql.Identifier(field), tbl=_assets_table(layer_id))
        params = {"lim": limit + 1}

    async with conn.cursor() as cur:
        await cur.execute(query, params)
        rows = [r[0] for r in await cur.fetchall()]

    truncated = len(rows) > limit
    return FieldValuesResponse(layer_id=layer_id, field=field, values=rows[:limit], truncated=truncated)


@router.patch(
    "/layers/{layer_id}/renderer",
    summary="Set the layer-default renderer (owner or admin)",
)
async def set_renderer(
    layer_id: str,
    payload: Renderer,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require_registered(conn, layer_id)
    auth_path = await _require_manage(claims, user, layer_id)
    if payload.type == "uniqueValue" and payload.field:
        await _require_field(conn, layer_id, payload.field)

    stored = payload.to_storage()
    async with conn.cursor() as cur:
        await _set_audit_context(cur, user.id)
        await cur.execute(
            "UPDATE platform.layers SET renderer = %(r)s, updated_at = now() WHERE layer_id = %(lid)s",
            {"r": Jsonb(stored), "lid": layer_id},
        )
    await conn.commit()
    log.info("layer_renderer_set", layer_id=layer_id, actor=str(user.id), auth_path=auth_path, renderer_type=payload.type)
    return {"layer_id": layer_id, "renderer": stored}


@router.delete(
    "/layers/{layer_id}/renderer",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear the layer-default renderer back to single-symbol fallback (owner or admin)",
)
async def clear_renderer(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> Response:
    await _require_registered(conn, layer_id)
    auth_path = await _require_manage(claims, user, layer_id)
    async with conn.cursor() as cur:
        await _set_audit_context(cur, user.id)
        await cur.execute(
            "UPDATE platform.layers SET renderer = NULL, updated_at = now() WHERE layer_id = %(lid)s",
            {"lid": layer_id},
        )
    await conn.commit()
    log.info("layer_renderer_cleared", layer_id=layer_id, actor=str(user.id), auth_path=auth_path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
