"""Where-used queries (decision-where-used.md) — the ONE place item cross-references are enumerated.

The complete in-product reference graph (§1): web maps → {hosted layers/views/tables via
operationalLayers[].layerId + tables[].layerId, rasters via rasterId, external ref items via
externalItemId}; view layers → source layer; internal geocoders → address layer. Group-membership
references (Open Data curation, Home featuring) live in OpenFGA and are read by the router via the
existing authz helpers — not here (this module is pure SQL).

Web-map hits are jsonb containment scans — cheap at municipal scale (hundreds of maps), so no
index and no migration. Map-scoped externals (no externalItemId) reference no catalog item and
never appear.
"""
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb


async def maps_referencing(
    conn: AsyncConnection,
    *,
    layer_id: str | None = None,
    raster_id: str | None = None,
    external_item_id: str | None = None,
) -> list[dict[str, Any]]:
    """Web maps whose definition references the given item (exactly one kwarg). A layer probe also
    scans the `tables` list (map tables reference geometry-less layers the same way). UNFILTERED —
    the router intersects with the caller's viewable maps before anything leaves the API."""
    if layer_id is not None:
        probe: Any = [{"layerId": layer_id}]
        where = ("definition->'operationalLayers' @> %(probe)s "
                 "OR definition->'tables' @> %(probe)s")
    elif raster_id is not None:
        probe = [{"rasterId": raster_id}]
        where = "definition->'operationalLayers' @> %(probe)s"
    elif external_item_id is not None:
        probe = [{"externalItemId": external_item_id}]
        where = "definition->'operationalLayers' @> %(probe)s"
    else:
        raise ValueError("one of layer_id / raster_id / external_item_id is required")
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT id::text, title FROM platform.web_maps WHERE {where} ORDER BY title",  # noqa: S608 — `where` is one of three literals above
            {"probe": Jsonb(probe)},
        )
        return [{"id": r[0], "title": r[1]} for r in await cur.fetchall()]


async def views_of(conn: AsyncConnection, layer_id: str) -> list[dict[str, Any]]:
    """View layers depending on `layer_id` — as their SOURCE, or (V080) as their JOINED table.
    Both are the T38/Fork-7 dependency that HARD-blocks delete."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT lv.view_layer_id, COALESCE(l.display_name, lv.view_layer_id) "
            "FROM platform.layer_views lv "
            "LEFT JOIN platform.layers l ON l.layer_id = lv.view_layer_id "
            "WHERE lv.source_layer_id = %s OR lv.join_table_id = %s ORDER BY 2",
            (layer_id, layer_id),
        )
        return [{"id": r[0], "title": r[1]} for r in await cur.fetchall()]


async def geocoders_on(conn: AsyncConnection, layer_id: str) -> list[dict[str, Any]]:
    """Internal locators geocoding against `layer_id` (the T37/Mode-B dependency that HARD-blocks
    delete). Locator names are org-visible by design (the Locate panel lists them) — never filtered."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id::text, name, enabled FROM platform.geocoders WHERE layer_id = %s ORDER BY name",
            (layer_id,),
        )
        return [{"id": r[0], "name": r[1], "enabled": r[2]} for r in await cur.fetchall()]


async def relates_touching(conn: AsyncConnection, layer_id: str) -> list[dict[str, Any]]:
    """Relates where `layer_id` is EITHER end (V078) — informational, never delete-blocking (a
    relate cascades with its endpoint; the where-used listing is what makes that deletion informed).
    Each hit names the OTHER end so both Manage panels read naturally. UNFILTERED — the router
    intersects with the caller's viewable layers, same as view hits."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT r.id::text, r.display_name, "
            "       CASE WHEN r.origin_layer_id = %(id)s THEN r.related_table_id "
            "            ELSE r.origin_layer_id END AS other_id, "
            "       COALESCE(l.display_name, CASE WHEN r.origin_layer_id = %(id)s "
            "            THEN r.related_table_id ELSE r.origin_layer_id END) AS other_title, "
            "       (r.origin_layer_id = %(id)s) AS is_origin "
            "FROM platform.layer_relates r "
            "LEFT JOIN platform.layers l ON l.layer_id = CASE WHEN r.origin_layer_id = %(id)s "
            "       THEN r.related_table_id ELSE r.origin_layer_id END "
            "WHERE r.origin_layer_id = %(id)s OR r.related_table_id = %(id)s "
            "ORDER BY r.display_name",
            {"id": layer_id},
        )
        return [
            {"relateId": r[0], "name": r[1], "id": r[2], "title": r[3], "isOrigin": r[4]}
            for r in await cur.fetchall()
        ]


async def map_reference_counts(conn: AsyncConnection) -> dict[tuple[str, str], int]:
    """{(kind, referenced id) → referencing-map count} across EVERY web map in one scan — the
    org-wide report input (§4.2, no N+1). kind ∈ 'layer' | 'raster' | 'external'. A map referencing
    the same item twice (op layer + table row can't, but belt-and-braces) counts once per map."""
    q = (
        "WITH refs AS ("
        "  SELECT wm.id AS map_id, ol->>'layerId' AS layer_id, ol->>'rasterId' AS raster_id, "
        "         ol->>'externalItemId' AS ext_id "
        "  FROM platform.web_maps wm, jsonb_array_elements(wm.definition->'operationalLayers') ol "
        "  UNION ALL "
        "  SELECT wm.id, t->>'layerId', NULL, NULL "
        "  FROM platform.web_maps wm, jsonb_array_elements(wm.definition->'tables') t"
        ") "
        "SELECT 'layer', layer_id, count(DISTINCT map_id) FROM refs WHERE layer_id IS NOT NULL GROUP BY 2 "
        "UNION ALL "
        "SELECT 'raster', raster_id, count(DISTINCT map_id) FROM refs WHERE raster_id IS NOT NULL GROUP BY 2 "
        "UNION ALL "
        "SELECT 'external', ext_id, count(DISTINCT map_id) FROM refs WHERE ext_id IS NOT NULL GROUP BY 2"
    )
    async with conn.cursor() as cur:
        await cur.execute(q)
        return {(kind, item_id): int(n) for kind, item_id, n in await cur.fetchall()}


async def dependent_counts_by_layer(conn: AsyncConnection) -> tuple[dict[str, int], dict[str, int]]:
    """({source layer → view count}, {layer → geocoder count}) in two GROUP BYs — report input."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT dep, count(*) FROM ("
            "  SELECT source_layer_id AS dep FROM platform.layer_views"
            "  UNION ALL"
            "  SELECT join_table_id FROM platform.layer_views WHERE join_table_id IS NOT NULL"
            ") s GROUP BY 1"
        )
        views = {r[0]: int(r[1]) for r in await cur.fetchall()}
        await cur.execute(
            "SELECT layer_id, count(*) FROM platform.geocoders WHERE layer_id IS NOT NULL GROUP BY 1"
        )
        geocoders = {r[0]: int(r[1]) for r in await cur.fetchall()}
    return views, geocoders
