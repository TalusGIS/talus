"""Field-collectable layers of a web map — ONE definition, two consumers.

The .qgz generator (routers/field_projects.py, "Make field project") and the Talus+ field
module (paid/, which imports core under the one-way rule) must never disagree about which of a
web map's operational layers a crew can collect against. This module is that definition, lifted
verbatim from the generator's `_walk_web_map` (decision-talus-field-f1.md §4.3.3) so the
behaviour the 2026-08 field-project bench proved is the behaviour the field app inherits.

A layer is collectable when ALL hold:
  * it is a vector `kind='layer'` operational layer, VISIBLE in the map definition;
  * it is a hosted slot layer, not a `vw_` view (views are not served on the field doors);
  * the catalog row exists, is `ready`, and is point / line / polygon;
  * the ACTOR can_view it by a GENUINE OpenFGA tuple — deliberately NO admin override. The
    credential that eventually reads the layer in the field (a per-project PAT today, a device
    PAT once packs exist) resolves with is_admin=False, so an admin-only-visible layer would be
    listed here and then refused there. The 2026-08-18 A50 bench proved that mismatch live.

Warning codes are the generator's own (its API contract) and are re-exported by it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from psycopg import AsyncConnection

from .authz import _openfga_check
from .models.edit_ops import EditOps

WARN_SKIPPED = "skipped_layers"           # non-vector / invisible / missing / not ready / bad geom
WARN_VIEW = "view_layer_not_served"       # a vw_ view layer in the map
WARN_FILTERS = "filters_not_applied"      # the layer carries map filters neither consumer applies
WARN_TOKEN_DENIED = "layer_token_denied"  # no genuine can_view tuple for the actor

COLLECTABLE_GEOMS = ("point", "line", "polygon")


@dataclass
class CollectableLayer:
    layer_id: str
    title: str                    # the map's title override, else the catalog display name
    geom_type: str                # 'point' | 'line' | 'polygon'
    editable: bool                # the layer-level editing switch (platform.layers.editable)
    attachments_enabled: bool
    display_color: str | None
    renderer: Any                 # platform.layers.renderer jsonb (or None)
    has_filters: bool
    # V109 (decision-layer-edit-capabilities.md): the owner's four flags — the same set for everyone with
    # edit access. The field door and the pack manifest carry them as `editOps`; the phone hides per flag.
    edit_ops: EditOps = field(default_factory=EditOps)


async def collectable_layers(
    conn: AsyncConnection, user_id: Any, definition: dict[str, Any]
) -> tuple[list[CollectableLayer], list[str]]:
    """Visible hosted vector layers of `definition` in draw order that `user_id` can genuinely
    view -> CollectableLayers; everything else skipped with a loud, de-duplicated warning code."""
    warnings: list[str] = []

    def warn(code: str) -> None:
        if code not in warnings:
            warnings.append(code)

    refs: list[tuple[str, str | None, bool]] = []   # (layer_id, title override, has filters)
    for op in definition.get("operationalLayers") or []:
        if not isinstance(op, dict):
            continue
        if (op.get("kind") or "layer") != "layer":
            warn(WARN_SKIPPED)
            continue
        lid = op.get("layerId")
        if not lid:
            continue
        if not op.get("visible", True):
            warn(WARN_SKIPPED)
            continue
        if lid.startswith("vw_"):
            warn(WARN_VIEW)
            continue
        refs.append((lid, op.get("title"), bool(op.get("filters"))))
    if not refs:
        return [], warnings

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT layer_id, display_name, display_color, renderer, geom_type, editable, "
            "attachments_enabled, status, edit_add, edit_attributes, edit_geometry, edit_delete "
            "FROM platform.layers WHERE layer_id = ANY(%s)",
            ([r[0] for r in refs],),
        )
        by_id = {r[0]: r for r in await cur.fetchall()}

    out: list[CollectableLayer] = []
    for lid, title_override, has_filters in refs:
        row = by_id.get(lid)
        if row is None or row[7] != "ready" or row[4] not in COLLECTABLE_GEOMS:
            warn(WARN_SKIPPED)
            continue
        if not await _openfga_check(user_id, "can_view", lid, object_type="layer"):
            warn(WARN_TOKEN_DENIED)
            continue
        if has_filters:
            warn(WARN_FILTERS)
        out.append(CollectableLayer(
            layer_id=lid,
            title=(title_override or row[1] or lid),
            geom_type=row[4],
            editable=bool(row[5]),
            attachments_enabled=bool(row[6]),
            display_color=row[2],
            renderer=row[3],
            has_filters=has_filters,
            edit_ops=EditOps.from_row(row[8], row[9], row[10], row[11]),
        ))
    return out, warnings
