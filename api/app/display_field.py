"""Display-field derivation (decision-layer-display-field.md, fork F2).

A layer's `display_field` (V072) is the attribute whose VALUE names a feature wherever it shows as one line
(popup title, Locate/feature-search labels, identify, the ESRI/OGC `displayField`). When it is NULL (unset),
we derive a sensible first-text-field at READ time so the layer labels well with zero config — never `id`.

This module is the SINGLE derivation both consumers share, so web (`/me/layers`) and the ESRI door agree on the
same field for an unstarred layer (the reason for the server-side-effective option): a slot takes
its first ACTIVE text/coded owner field by the owner's `sort_order`, else `notes` (the always-present base text
column); a view layer takes its first real text column (ordinal order), minus identity / audit / plumbing.

The rule lives in `_slot_pick_sql` / `_column_pick_sql` so the single-layer form (`derive_display_field`, used
by the per-layer ESRI doc) and the batched form (`derive_display_fields`, used by the whole-catalog `/me/layers`
list — two queries, not N) can never drift apart.
"""
import re

from psycopg import AsyncConnection

_SLOT_RE = re.compile(r"^tal_[0-9a-f]{8}$")  # owner-created slot (props-backed) vs view (real columns)

# Real columns that are never a feature's NAME (identity / geometry / audit / plumbing) — mirrors search.py
# (+ objectid, the V064 ESRI int handle: real, but an identity, never a label).
LABEL_SKIP_COLS = frozenset(
    {"id", "objectid", "geom", "row_version", "created_at", "created_by", "updated_at", "updated_by",
     "ai_provenance", "props"}
)
LABEL_TEXT_TYPES = ("text", "character varying", "character", "citext")


async def derive_display_field(conn: AsyncConnection, layer_id: str) -> str | None:
    """The derived label field for ONE layer with an unset display_field (rule above). None only when a layer
    has no usable text field at all (e.g. an unprovisioned slot with no backing table and no fields)."""
    async with conn.cursor() as cur:
        if _SLOT_RE.match(layer_id):
            await cur.execute(
                "SELECT field_key FROM platform.layer_fields "
                "WHERE layer_id = %s AND active AND data_type IN ('text', 'coded') "
                "ORDER BY sort_order, field_key LIMIT 1",
                (layer_id,),
            )
            row = await cur.fetchone()
            if row is not None:
                return row[0]
            # Every slot has a notes column — the universal fallback (only when a backing table exists).
            await cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'assets' AND table_name = %s AND column_name = 'notes'",
                (layer_id,),
            )
            return "notes" if await cur.fetchone() is not None else None
        await cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'assets' AND table_name = %s "
            "  AND data_type = ANY(%s) AND column_name <> ALL(%s) "
            "ORDER BY ordinal_position LIMIT 1",
            (layer_id, list(LABEL_TEXT_TYPES), list(LABEL_SKIP_COLS)),
        )
        row = await cur.fetchone()
        return row[0] if row is not None else None


async def derive_display_fields(conn: AsyncConnection, layer_ids: set[str]) -> dict[str, str]:
    """Batched form of `derive_display_field` for a whole layer set — two queries (slot fields, real columns),
    not N. Same rule; layers with nothing usable are simply absent from the result."""
    if not layer_ids:
        return {}
    derived: dict[str, str] = {}
    slot_ids = [lid for lid in layer_ids if _SLOT_RE.match(lid)]
    other_ids = [lid for lid in layer_ids if not _SLOT_RE.match(lid)]
    async with conn.cursor() as cur:
        if slot_ids:
            # First text/coded owner field per slot, by the owner's sort_order (DISTINCT ON = one per layer).
            await cur.execute(
                "SELECT DISTINCT ON (layer_id) layer_id, field_key "
                "FROM platform.layer_fields "
                "WHERE layer_id = ANY(%s) AND active AND data_type IN ('text', 'coded') "
                "ORDER BY layer_id, sort_order, field_key",
                (slot_ids,),
            )
            for lid, field_key in await cur.fetchall():
                derived[lid] = field_key
            # `notes` fallback for slots with no owner text field — only where a backing table exists.
            missing = [lid for lid in slot_ids if lid not in derived]
            if missing:
                await cur.execute(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'assets' AND table_name = ANY(%s) AND column_name = 'notes'",
                    (missing,),
                )
                for (table_name,) in await cur.fetchall():
                    derived[table_name] = "notes"
        if other_ids:
            # First real text column per view (ordinal order), skipping identity/audit/plumbing.
            await cur.execute(
                "SELECT DISTINCT ON (table_name) table_name, column_name "
                "FROM information_schema.columns "
                "WHERE table_schema = 'assets' AND table_name = ANY(%s) "
                "  AND data_type = ANY(%s) AND column_name <> ALL(%s) "
                "ORDER BY table_name, ordinal_position",
                (other_ids, list(LABEL_TEXT_TYPES), list(LABEL_SKIP_COLS)),
            )
            for table_name, column_name in await cur.fetchall():
                derived[table_name] = column_name
    return derived
