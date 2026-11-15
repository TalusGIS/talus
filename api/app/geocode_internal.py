"""Mode B — internal locator: geocode against a hosted address-point layer in PostGIS.

The org's own authoritative address points become a locator (decision-own-data-geocoder.md) — no
external service in the loop, so this is the first locator type that works in an air-gapped deployment.
A ranked fuzzy match over ONE org layer, nothing more (the hard fences: no interpolation, no
parsing engine, no TIGER, never a general-purpose geocoder).

This is the conn-aware SIBLING of the httpx adapters in `geocode.py` (decision §3 F4): same
suggest/search/reverse surface, but the transport is the request's DB connection. `geocode.py`'s
orchestration branches per adapter kind inside the fail-soft walk; the HTTP adapters are untouched.
This module imports the contract models FROM `geocode` (one-way at module level — `geocode` imports
this module lazily inside `_adapter_for`, so there is no import cycle).

Matching (decision §3 F1): `platform.addr_document(ARRAY[<field exprs>])` — the mapped fields
concatenated, whitespace-normalized, unaccented (V066) — compared with pg_trgm. The SAME composed
expression backs the per-locator GIN index (F5) and every query here; they must match verbatim to
hit the index. Suggest = prefix-OR-similar ranked prefix-first; search = `%` similarity (the 0.3
pg_trgm default threshold), score = similarity × 100 onto the 0..100 contract; a picked
suggestion resolves exactly by feature id (`magic_key`, F8). Reverse = KNN nearest point within
a 100 m geography cap.

Identifier discipline (mirrors layer_views/layer_query): the layer table name is a proven
`platform.layers` registry id gated to the hosted `tal_*` shape; field names are validated against
the layer_fields registry before they reach SQL; everything is composed via `psycopg.sql`;
every runtime value is a bound, explicitly-cast parameter (the banked IndeterminateDatatype lesson).
"""
from __future__ import annotations

import re
import uuid as uuid_mod
from dataclasses import dataclass

import structlog
from psycopg import AsyncConnection, sql

from .config import GeocoderConfig
from .geocode import _MAX_CANDIDATES, GeoCandidate, GeocoderError, GeoSuggestion

log = structlog.get_logger(__name__)

# Reverse cap: a click farther than this from the nearest address point is "no address here"
# (kickoff §1; keeps a mid-street click from labeling itself with a far-away parcel).
_REVERSE_CAP_M = 100.0

# Field-mapping bounds (decision §3 F2): 1..N concatenated fields; 6 is plenty for
# number + street + unit + city + state + zip.
_MAX_FIELDS = 6

# Hosted-layer token (mirrors publisher._SLOT_RE); vw_* view layers are ineligible (decision §5).
# Since the pilot built-ins retired (decision-retire-builtins.md phase 2) these two shapes are the
# whole registry, so a locator source is ALWAYS a hosted tal_* slot (props/notes fields) — the
# flat-column locator branch retired with the built-ins.
_HOSTED_RE = re.compile(r"^tal_[0-9a-f]{8}$")
_VIEW_RE = re.compile(r"^vw_[0-9a-f]{12}$")


class MappingError(GeocoderError):
    """The locator's layer/field mapping doesn't resolve (missing layer, wrong geometry type,
    unknown field, …). A GeocoderError subclass so the orchestration walk's existing fail-soft
    catch covers it without importing this module."""


@dataclass
class _LayerInfo:
    layer_id: str
    storage_srid: int  # the SRID geometry is physically stored in (native storage ≠ 4326)


def _escape_like(q: str) -> str:
    """Escape LIKE/ILIKE wildcards in user text so the prefix pattern is a literal prefix."""
    return q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _normalize_q(q: str) -> str:
    """Collapse runs of whitespace + trim — the SAME normalization addr_document applies to the
    row side, so the ILIKE prefix comparison isn't broken by a double space or tab in user text."""
    return " ".join(q.split())


# Below this many characters a query has no extractable trigram, so the GIN degenerates to a
# full scan-and-recheck per keystroke — suggest waits for the third character instead (external
# geocoder services impose the same server-side).
_MIN_SUGGEST_LEN = 3

# Batch prefilter threshold (decision-batch-geocoding §3 F1a). The set-based batch path scores with
# `word_similarity` (a bare-street reference row found as a continuous extent WITHIN a full-postal
# query scores ~1.0, where symmetric `similarity` is diluted to ~0.46 by the city/state/zip tail —
# measured on the real poc_address data). But `word_similarity` (`<%`) is a SEQ SCAN — the GIN can't
# serve it in the doc-within-query direction. So the GIN-served `%` (similarity) stays the cheap
# PREFILTER and `word_similarity` re-scores only the survivors. This LOCAL threshold widens the `%`
# net below the 0.3 default so a heavily-tailed-but-correct address isn't filtered out before the
# re-score can rescue it; it is set transaction-LOCAL so the interactive locator's 0.3 is untouched.
#
# On a county-scale layer the `%` net is barely a net at all — nearly every row shares the city / ZIP
# trigrams a full-postal query carries (47,720 of 318,761 rows survived at 0.2; 3.1 s per address,
# measured 2026-09-18). So the batch path splits its inputs (decision-batch-geocoding-county-scale.md
# F2): an input that BEGINS WITH A HOUSE NUMBER is prefiltered by that number as an ILIKE prefix on the
# same GIN (`doc ILIKE '3706 %'` → 29 rows, 2.8 ms — "same house number, any street"), and only an input
# without one (`PO BOX 894`, a place name, an intersection) falls back to this `%` prefilter. Both stages
# re-score the survivors identically.
_BATCH_PREFILTER_THRESHOLD = 0.2

# A leading run of digits followed by whitespace — the house number the prefix prefilter keys on. The
# match is on the whitespace-normalized query, so it is anchored at the true first token. `1415-B Main`
# and `12A Main` deliberately do NOT qualify (the token isn't a bare number; `%` handles them), and there
# is no fallback when the prefix finds nothing: "no point carries that house number" is a true no_match,
# not a reason to spend 3 s on the wide net (F2a).
_HOUSE_NUMBER_RE = re.compile(r"^(\d+)\s")


def _house_number(normalized_q: str) -> str | None:
    """The leading house number of a whitespace-normalized query, or None when it doesn't start with one."""
    m = _HOUSE_NUMBER_RE.match(normalized_q)
    return m.group(1) if m else None


def _house_number_pattern(house_number: str) -> str:
    """The ILIKE prefix pattern for one house number: `'<n> %'`. The trailing space is load-bearing —
    `'750%'` would let `7501 X ST` survive for `750 X ST`, and `word_similarity` barely distinguishes a
    one-digit house-number difference (it would score ~90). The token is digits-only by construction, but
    it is escaped anyway: a LIKE pattern is built from user text, and the rule is never to reason about
    which user text is safe."""
    return _escape_like(house_number) + " %"


async def _resolve_layer(conn: AsyncConnection, layer_id: str) -> _LayerInfo:
    """The registry row, proven eligible: a registered, 'ready' hosted POINT layer.
    Views/tables/line/polygon are ineligible (decision §3 F2). Raises MappingError."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, geom_type, working_srid, native_storage "
            "FROM platform.layers WHERE layer_id = %s",
            (layer_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise MappingError(f"layer {layer_id!r} is not a registered layer")
    status, geom_type, working_srid, native_storage = row
    if _VIEW_RE.fullmatch(layer_id):
        raise MappingError(f"layer {layer_id!r} is a view layer (not a locator source in v1)")
    if not _HOSTED_RE.fullmatch(layer_id):
        # Unreachable once V095 leaves only tal_/vw_ in the registry — kept as the shape proof
        # (a locator's layer_id ends up named in index DDL; never trust, even proven tokens).
        raise MappingError(f"layer {layer_id!r} is not a hosted layer")
    if geom_type != "point":
        raise MappingError(f"layer {layer_id!r} is not a point layer (geom_type={geom_type!r})")
    if status != "ready":
        raise MappingError(f"layer {layer_id!r} is not ready (status={status!r})")
    return _LayerInfo(
        layer_id=layer_id,
        storage_srid=int(working_srid) if native_storage else 4326,
    )


async def _field_catalog(conn: AsyncConnection, info: _LayerInfo) -> list[dict[str, str]]:
    """key→label catalog of the layer's addressable fields — the ONE eligibility definition, used
    by both the admin picker (`mappable_fields`) and mapping validation (`_field_exprs`), so the
    picker can never offer a field validation rejects (or vice versa): the active layer_fields
    registry + `notes`."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT field_key, label FROM platform.layer_fields "
            "WHERE layer_id = %s AND active ORDER BY sort_order, field_key",
            (info.layer_id,),
        )
        out = [{"key": r[0], "label": r[1] or r[0]} for r in await cur.fetchall()]
    out.append({"key": "notes", "label": "Notes"})
    return out


async def _field_exprs(
    conn: AsyncConnection, info: _LayerInfo, fields: list[str], *,
    allowed: set[str] | None = None, locality: bool = False,
) -> list[sql.Composable]:
    """The per-field SQL expressions (catalog-validated, `psycopg.sql`-composed) in the mapping's
    order: `props ->> '<key>'` for a defined layer_fields key, or the literal `notes`
    column. Raises MappingError. `allowed` lets a caller that resolves both lists share ONE catalog
    read; `locality=True` is the 0..N list (an empty locality list is the norm)."""
    which = "locality_fields" if locality else "address_fields"
    if (not fields and not locality) or len(fields) > _MAX_FIELDS:
        raise MappingError(f"{which} must name {'0' if locality else '1'}..{_MAX_FIELDS} fields")
    if len(set(fields)) != len(fields):
        raise MappingError(f"{which} must not repeat a field")

    if allowed is None:
        allowed = {f["key"] for f in await _field_catalog(conn, info)}
    exprs: list[sql.Composable] = []
    for f in fields:
        if f not in allowed:
            raise MappingError(f"{f!r} is not an addressable field of layer {info.layer_id!r}")
        if f == "notes":
            exprs.append(sql.SQL("notes"))
        else:
            # jsonb key as a baked literal (NOT a bind param): the index expression has no
            # parameters, and index and query must match verbatim (module docstring).
            exprs.append(sql.SQL("(props ->> {key})").format(key=sql.Literal(f)))
    return exprs


def _doc_expr(exprs: list[sql.Composable]) -> sql.Composable:
    """The match document — MUST be composed identically here and in the index DDL (F1/F5)."""
    return sql.SQL("platform.addr_document(ARRAY[{parts}]::text[])").format(
        parts=sql.SQL(", ").join(exprs)
    )


def _locality_score_expr(loc_exprs: list[sql.Composable], qtext: sql.Composable) -> sql.Composable:
    """The tiebreak term (decision-batch-geocoding-county-scale §F3): how much of the locality
    document (city, ZIP …) appears in the query. Orders equal street matches — the same street number
    and name in two towns — behind the town the query names; NEVER part of the score, so a query
    without a city loses nothing. A locator with no locality fields gets a constant (no-op order)."""
    if not loc_exprs:
        return sql.SQL("0")
    return sql.SQL("word_similarity({doc}, platform.f_unaccent({q}))").format(
        doc=_doc_expr(loc_exprs), q=qtext
    )


async def validate_mapping(
    conn: AsyncConnection, layer_id: str, address_fields: list[str], locality_fields: list[str]
) -> None:
    """Prove a full internal mapping against the layer's field catalog — both lists resolve, and
    they are disjoint. The create/edit paths call this BEFORE any row is written (F3a). Raises
    MappingError; returns nothing on success."""
    if set(address_fields) & set(locality_fields):
        raise MappingError("a field cannot be both an address field and a locality field")
    info = await _resolve_layer(conn, layer_id)
    allowed = {f["key"] for f in await _field_catalog(conn, info)}
    await _field_exprs(conn, info, address_fields, allowed=allowed)
    await _field_exprs(conn, info, locality_fields, allowed=allowed, locality=True)


def _label_expr(exprs: list[sql.Composable]) -> sql.Composable:
    """The display label: same fields, same order, whitespace-normalized — but NOT unaccented
    (the user should see 'Muñoz St', not its folded match form)."""
    return sql.SQL(r"btrim(regexp_replace(concat_ws(' ', {parts}), '\s+', ' ', 'g'))").format(
        parts=sql.SQL(", ").join(exprs)
    )


def _bbox_clause(info: _LayerInfo, box: list[float] | None) -> sql.Composable:
    """Viewport restriction ('search what you're looking at') — contract parity with the external
    adapters. Storage-SRID envelope so the layer's spatial index serves the `&&`. For projected
    storage the 4326 rectangle is DENSIFIED before transforming: a bare corner transform maps only
    the vertices, and in a conic CRS the true image bows past the corner-chord bbox — points near
    the viewport's north/south edge would silently drop out."""
    if not box:
        return sql.SQL("")
    if info.storage_srid == 4326:
        return sql.SQL(
            " AND geom && ST_MakeEnvelope("
            "%(xmin)s::float8, %(ymin)s::float8, %(xmax)s::float8, %(ymax)s::float8, 4326)"
        )
    return sql.SQL(
        " AND geom && (SELECT assets.shape_in_srid(ST_Segmentize(ST_MakeEnvelope("
        "%(xmin)s::float8, %(ymin)s::float8, %(xmax)s::float8, %(ymax)s::float8, 4326)"
        ", 0.05), {srid}))"
    ).format(srid=sql.Literal(info.storage_srid))


def _bbox_params(box: list[float] | None) -> dict[str, float]:
    if not box:
        return {}
    return {"xmin": box[0], "ymin": box[1], "xmax": box[2], "ymax": box[3]}


class InternalLocatorAdapter:
    """Conn-aware locator over one hosted address-point layer (the F4 sibling adapter).

    Mirrors the `GeocoderAdapter` surface but takes the request's `AsyncConnection` instead of an
    `httpx.AsyncClient`. Like its HTTP siblings it does NOT swallow errors — `MappingError` and
    `psycopg.Error` propagate to the orchestration walk, which owns the fail-soft policy (and the
    conn ROLLBACK, so a failed statement can't poison the walk's shared connection)."""

    transport = "db"  # the walk's branch key (GeocoderAdapter.transport = "http")

    def __init__(self, config: GeocoderConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    def _extent(self, bbox: list[float] | None) -> list[float] | None:
        """Same precedence as GeocoderAdapter._extent (configured extent wins, else the viewport);
        internal locators normally have extent NULL — data-bounded by nature (decision §3 F7)."""
        return self.config.extent or bbox

    async def _mapping(
        self, conn: AsyncConnection
    ) -> tuple[_LayerInfo, list[sql.Composable], list[sql.Composable]]:
        """(layer, street exprs, locality exprs). The street exprs compose the indexed / scored
        document; the locality exprs compose the tiebreak document and the tail of the label
        (V107 — decision-batch-geocoding-county-scale §F3a). One catalog read for both."""
        if not self.config.layer_id or not self.config.address_fields:
            raise MappingError(f"locator {self.name!r} has no layer/field mapping")
        info = await _resolve_layer(conn, self.config.layer_id)
        allowed = {f["key"] for f in await _field_catalog(conn, info)}
        exprs = await _field_exprs(conn, info, list(self.config.address_fields), allowed=allowed)
        loc = await _field_exprs(
            conn, info, list(self.config.locality_fields or []), allowed=allowed, locality=True
        )
        return info, exprs, loc

    async def suggest(
        self, conn: AsyncConnection, q: str, bbox: list[float] | None = None
    ) -> list[GeoSuggestion]:
        q = _normalize_q(q)
        if len(q) < _MIN_SUGGEST_LEN:
            return []
        info, exprs, loc = await self._mapping(conn)
        box = self._extent(bbox)
        # `%%` is the pg_trgm similarity operator (doubled for psycopg's param substitution);
        # prefix matches rank first, then trigram similarity, then the locality tiebreak (decision
        # §3 F1; county-scale §F3).
        query = sql.SQL(
            "SELECT id::text, label FROM ("
            "  SELECT id, {label} AS label,"
            "         ({doc} ILIKE platform.f_unaccent(%(prefix)s::text) || '%%') AS pref,"
            "         similarity({doc}, platform.f_unaccent(%(q)s::text)) AS sim,"
            "         {loc} AS loc"
            "  FROM {tbl}"
            "  WHERE ({doc} %% platform.f_unaccent(%(q)s::text)"
            "         OR {doc} ILIKE platform.f_unaccent(%(prefix)s::text) || '%%'){bbox}"
            ") s ORDER BY pref DESC, sim DESC, loc DESC, label LIMIT {lim}"
        ).format(
            label=_label_expr(exprs + loc),
            doc=_doc_expr(exprs),
            loc=_locality_score_expr(loc, sql.SQL("%(q)s::text")),
            tbl=sql.Identifier("assets", info.layer_id),
            bbox=_bbox_clause(info, box),
            lim=sql.Literal(_MAX_CANDIDATES),
        )
        params: dict[str, object] = {"q": q, "prefix": _escape_like(q), **_bbox_params(box)}
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()
        return [
            GeoSuggestion(text=r[1], source=self.name, magic_key=r[0]) for r in rows if r[1]
        ]

    async def search(
        self,
        conn: AsyncConnection,
        q: str | None,
        magic_key: str | None,
        bbox: list[float] | None = None,
    ) -> list[GeoCandidate]:
        info, exprs, loc = await self._mapping(conn)

        # Picked-suggestion path (F8): the magic_key IS the feature id — exact resolve. The key is
        # client-supplied: validate it app-side (a bad uuid must yield empty, not an aborted txn).
        if magic_key:
            try:
                uuid_mod.UUID(magic_key)
            except ValueError:
                return []
            query = sql.SQL(
                "SELECT {label}, ST_X(ST_Transform(geom, 4326)), ST_Y(ST_Transform(geom, 4326)) "
                "FROM {tbl} WHERE id = %(id)s::uuid"
            ).format(label=_label_expr(exprs + loc), tbl=sql.Identifier("assets", info.layer_id))
            async with conn.cursor() as cur:
                await cur.execute(query, {"id": magic_key})
                row = await cur.fetchone()
            if row is None or row[1] is None or row[2] is None:
                return []
            return [
                GeoCandidate(
                    label=row[0] or (q or ""), lon=float(row[1]), lat=float(row[2]),
                    score=100.0, source=self.name,
                )
            ]

        if not q:
            return []
        q = _normalize_q(q)
        if not q:
            return []
        box = self._extent(bbox)
        # `%%` = pg_trgm similarity at the default 0.3 threshold (stated, not GUC-tuned);
        # score = similarity × 100 onto the ArcGIS-like 0..100 contract (decision §3 F1).
        query = sql.SQL(
            "SELECT id::text, label, lon, lat, sim FROM ("
            "  SELECT id, {label} AS label,"
            "         ST_X(ST_Transform(geom, 4326)) AS lon, ST_Y(ST_Transform(geom, 4326)) AS lat,"
            "         similarity({doc}, platform.f_unaccent(%(q)s::text)) AS sim,"
            "         {loc} AS loc"
            "  FROM {tbl}"
            "  WHERE {doc} %% platform.f_unaccent(%(q)s::text){bbox}"
            ") s ORDER BY sim DESC, loc DESC, label LIMIT {lim}"
        ).format(
            label=_label_expr(exprs + loc),
            doc=_doc_expr(exprs),
            loc=_locality_score_expr(loc, sql.SQL("%(q)s::text")),
            tbl=sql.Identifier("assets", info.layer_id),
            bbox=_bbox_clause(info, box),
            lim=sql.Literal(_MAX_CANDIDATES),
        )
        params: dict[str, object] = {"q": q, **_bbox_params(box)}
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()
        return [
            GeoCandidate(
                label=r[1] or q, lon=float(r[2]), lat=float(r[3]),
                score=round(float(r[4]) * 100, 1), source=self.name,
            )
            for r in rows
            if r[2] is not None and r[3] is not None
        ]

    async def search_batch(
        self,
        conn: AsyncConnection,
        queries: list[str],
        bbox: list[float] | None = None,
        limit: int = 2,
    ) -> list[list[GeoCandidate]]:
        """Bulk forward-geocode N address strings in ONE set-based pass (decision-batch-geocoding §4).

        The free/instant default for batch geocoding: a LATERAL query joins every input string against
        the address layer, so N addresses cost one statement per stage, not N round-trips. Scoring is the
        HYBRID resolved on the real data (decision §3 F1a): a cheap GIN-served PREFILTER, then
        `word_similarity({doc}, f_unaccent(q))` re-scores the survivors — a bare-street row found within
        a full-postal query scores ~100 where symmetric `similarity` would dilute it to ~46.

        The prefilter runs in two stages (decision-batch-geocoding-county-scale.md F2), chosen per input:
        (A) an input that begins with a house number is prefiltered by `{doc} ILIKE '<number> %'` — the
        same GIN serves ILIKE, and "same house number, any street" is a few dozen rows on a county layer
        where the `%` net is tens of thousands; (B) an input without one uses the `%` similarity
        prefilter widened to a LOCAL 0.2 threshold. Stage A has no fallback: a house number no point
        carries is a true no_match (F2a). Both stages share the re-score and the ordering.

        Returns up to `limit` candidates per input (best first — 2 is enough for the caller's matched/tie
        bucketing; rematch re-fetches a fuller candidate set per row via `search()`), aligned by index
        to `queries`; a blank or no-match input yields `[]`. Blank inputs never reach SQL (they're
        `skipped`, not geocoded)."""
        if not queries:
            return []
        out: list[list[GeoCandidate]] = [[] for _ in queries]
        # Normalize each input; keep only the non-blank ones for SQL, remembering their original slot so
        # results map back exactly (a blank cell stays [] = skipped, never a geocode attempt).
        normalized = [_normalize_q(q or "") for q in queries]
        slots = [i for i, q in enumerate(normalized) if q]
        if not slots:
            return out
        # Split the live inputs by stage. Each stage keeps (slot, query) pairs so results map back to
        # the caller's index exactly, whichever stage answered.
        stage_a: list[tuple[int, str, str]] = []  # (slot, query, house-number ILIKE pattern)
        stage_b: list[tuple[int, str]] = []
        for i in slots:
            num = _house_number(normalized[i])
            if num is not None:
                stage_a.append((i, normalized[i], _house_number_pattern(num)))
            else:
                stage_b.append((i, normalized[i]))

        info, exprs, loc = await self._mapping(conn)
        box = self._extent(bbox)
        # One statement shape for both stages; only the prefilter clause differs. Aliases
        # `_bg(qtext, pref, ordn)` are named so they can't collide with a mapped real column; `ordn` is
        # 1-based into the stage's payload. `word_similarity` re-scores the survivors (the GIN can't serve
        # `<%` in the doc-within-query direction, so it must ride behind an index-served prefilter — F1a).
        # The score is the STREET document alone; the locality document only orders equal street
        # matches (county-scale §F3 — every blend that let city/ZIP into the score lowered `matched`).
        stmt = sql.SQL(
            "SELECT _bg.ordn, m.label, m.lon, m.lat, m.sim "
            "FROM unnest(%(queries)s::text[], %(prefixes)s::text[]) WITH ORDINALITY AS _bg(qtext, pref, ordn) "
            "LEFT JOIN LATERAL ("
            "  SELECT {label} AS label,"
            "         ST_X(ST_Transform(geom, 4326)) AS lon, ST_Y(ST_Transform(geom, 4326)) AS lat,"
            "         word_similarity({doc}, platform.f_unaccent(_bg.qtext)) AS sim"
            "  FROM {tbl}"
            "  WHERE {prefilter}{bbox}"
            "  ORDER BY word_similarity({doc}, platform.f_unaccent(_bg.qtext)) DESC, {loc} DESC, label"
            "  LIMIT {lim}"
            ") m ON true "
            "ORDER BY _bg.ordn, m.sim DESC NULLS LAST"
        )
        common = dict(
            label=_label_expr(exprs + loc),
            doc=_doc_expr(exprs),
            loc=_locality_score_expr(loc, sql.SQL("_bg.qtext")),
            tbl=sql.Identifier("assets", info.layer_id),
            bbox=_bbox_clause(info, box),
            lim=sql.Literal(max(1, limit)),
        )
        # Stage A: the house-number prefix, served by the trigram GIN's LIKE/ILIKE support (a ≥3-char
        # literal — `'12 %'` is the shortest possible and still qualifies with pg_trgm's padding).
        query_a = stmt.format(prefilter=sql.SQL("{doc} ILIKE _bg.pref").format(doc=common["doc"]), **common)
        # Stage B: `%%` = the pg_trgm similarity operator (doubled for psycopg param substitution).
        query_b = stmt.format(
            prefilter=sql.SQL("{doc} %% platform.f_unaccent(_bg.qtext)").format(doc=common["doc"]), **common
        )

        async def run(query: sql.Composable, payload: list[str], prefixes: list[str | None],
                      stage_slots: list[int]) -> None:
            params: dict[str, object] = {"queries": payload, "prefixes": prefixes, **_bbox_params(box)}
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                rows = await cur.fetchall()
            for ordn, label, lon, lat, sim in rows:
                if lon is None or lat is None:  # the LEFT JOIN's no-match row (all-NULL) — skip it
                    continue
                out[stage_slots[ordn - 1]].append(
                    GeoCandidate(
                        label=label or payload[ordn - 1], lon=float(lon), lat=float(lat),
                        score=round(float(sim) * 100, 1), source=self.name,
                    )
                )

        if stage_a:
            await run(query_a, [q for _, q, _ in stage_a], [p for _, _, p in stage_a], [i for i, _, _ in stage_a])
        if stage_b:
            async with conn.cursor() as cur:
                # Widen the `%` prefilter net (LOCAL — never leaks to the interactive locator's 0.3).
                await cur.execute(
                    "SELECT set_config('pg_trgm.similarity_threshold', %s, true)",
                    (str(_BATCH_PREFILTER_THRESHOLD),),
                )
            await run(query_b, [q for _, q in stage_b], [None] * len(stage_b), [i for i, _ in stage_b])
        return out

    async def reverse(self, conn: AsyncConnection, lon: float, lat: float) -> str | None:
        """KNN nearest address point within the geography cap — the "What's here?" address line
        (decision-reverse-geocode.md consumer, zero UI change). The `<->` runs in storage SRID so
        the layer's geom index drives it, but its units are planar (degrees for 4326 storage) and
        anisotropic with latitude — so take a small K by KNN and pick the winner by the REAL
        (geography meters) distance, then apply the cap. K=8: the true nearest within 100 m is
        never outside the 8 planar-nearest at municipal point densities."""
        info, exprs, loc = await self._mapping(conn)
        exprs = exprs + loc  # the address line shows the full address, locality included
        query = sql.SQL(
            "SELECT label FROM ("
            "  SELECT {label} AS label,"
            "         ST_Distance("
            "           ST_Transform(geom, 4326)::geography,"
            "           ST_SetSRID(ST_MakePoint(%(lon)s::float8, %(lat)s::float8), 4326)::geography"
            "         ) AS dist"
            "  FROM {tbl}"
            "  ORDER BY geom <-> ST_Transform("
            "    ST_SetSRID(ST_MakePoint(%(lon)s::float8, %(lat)s::float8), 4326), {srid})"
            "  LIMIT 8"
            ") s WHERE dist <= %(cap)s::float8 ORDER BY dist LIMIT 1"
        ).format(
            label=_label_expr(exprs),
            tbl=sql.Identifier("assets", info.layer_id),
            srid=sql.Literal(info.storage_srid),
        )
        async with conn.cursor() as cur:
            await cur.execute(query, {"lon": lon, "lat": lat, "cap": _REVERSE_CAP_M})
            row = await cur.fetchone()
        return (row[0] or None) if row else None


# --- admin-form pickers (decision §3 F7) ----------------------------------------------------------


async def eligible_layers(conn: AsyncConnection) -> list[dict[str, str]]:
    """Layers an internal locator may search (decision §3 F2): registered, ready, hosted POINT
    layers; view layers excluded (v1). Feeds the admin form's layer picker."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT layer_id, coalesce(display_name, layer_id) FROM platform.layers "
            r"WHERE geom_type = 'point' AND status = 'ready' AND layer_id LIKE 'tal\_%' "
            "ORDER BY 2"
        )
        rows = await cur.fetchall()
    return [{"layerId": r[0], "displayName": r[1]} for r in rows]


async def mappable_fields(conn: AsyncConnection, layer_id: str) -> list[dict[str, str]]:
    """The address-field choices for one eligible layer — the shared `_field_catalog`, so the
    picker and mapping validation can never disagree. Raises MappingError for an ineligible
    layer."""
    info = await _resolve_layer(conn, layer_id)
    return await _field_catalog(conn, info)


# --- the F5 per-locator trigram index (runtime DDL, the T38-views precedent) ---------------------


def index_name(geocoder_id: str) -> str:
    """`idx_geocoder_<32 hex>` — server-generated from the locator's uuid, never user-derived."""
    return f"idx_geocoder_{uuid_mod.UUID(str(geocoder_id)).hex}"


async def create_locator_index(
    conn: AsyncConnection, geocoder_id: str, layer_id: str, fields: list[str]
) -> str:
    """CREATE the expression GIN for one internal locator (decision §3 F5). Rides the caller's
    transaction (transactional DDL, like register_view) — the caller commits. The index expression
    is composed by the SAME helpers the queries use, so they match verbatim. Raises MappingError
    on a bad mapping (nothing created)."""
    info = await _resolve_layer(conn, layer_id)
    exprs = await _field_exprs(conn, info, fields)
    name = index_name(geocoder_id)
    ddl = sql.SQL(
        "CREATE INDEX {idx} ON {tbl} USING GIN ({doc} public.gin_trgm_ops)"
    ).format(
        idx=sql.Identifier(name),
        tbl=sql.Identifier("assets", info.layer_id),
        doc=_doc_expr(exprs),
    )
    async with conn.cursor() as cur:
        await cur.execute(ddl)
    log.info("geocoder_index_created", geocoder_id=str(geocoder_id), layer_id=layer_id, index=name)
    return name


async def drop_locator_index(conn: AsyncConnection, geocoder_id: str) -> None:
    """DROP the locator's index if present (delete / pre-rebuild). IF EXISTS: the index may already
    be gone (e.g. created before a crash rolled back) — dropping a missing index is not an error."""
    name = index_name(geocoder_id)
    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL("DROP INDEX IF EXISTS {idx}").format(idx=sql.Identifier("assets", name))
        )
    log.info("geocoder_index_dropped", geocoder_id=str(geocoder_id), index=name)


async def probe(conn: AsyncConnection, config: GeocoderConfig, q: str) -> tuple[bool, int, str | None]:
    """The admin "Test" action, conn-aware (decision §3 F7): validate the mapping and fire ONE
    sample suggest against the live table. Never raises — a broken mapping is a result, not an
    exception (and the conn is rolled back so the caller's transaction stays usable)."""
    adapter = InternalLocatorAdapter(config)
    try:
        results = await adapter.suggest(conn, q)
        return (True, len(results), None)
    except MappingError as exc:
        await conn.rollback()
        return (False, 0, str(exc))
    except Exception as exc:  # psycopg.Error etc. — same fail-soft posture as geocode.probe
        await conn.rollback()
        return (False, 0, f"{exc.__class__.__name__}: {exc}")
