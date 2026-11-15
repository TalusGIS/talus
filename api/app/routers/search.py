"""Cross-layer feature search (decision-feature-search.md) — the in-map "Locate" panel's backend.

One box, type a query, find features by ATTRIBUTE across the visible layers, and zoom to them — with
ZERO per-layer config (the ArcGIS-Pro-enhanced-search feel). Fans out over an explicit layer-id list
(the map's visible layers). Every layer is VIEW-GATED and **fail-closed**: a caller's search never
returns a feature they can't view, and a layer they can't view is silently skipped (never 403s the
whole search, never leaks existence). Each layer runs the SAME `assets.search_document(notes, props)`
expression that V058 indexed (functional GIN), so a hit is an index lookup, not a full scan — the
whole point of indexing in v1 (30k+-row parcel layers).

Two layer kinds are searched (dispatched per id): (1) owner-created `tal_*` slots — attributes in `props`
jsonb, matched via the V058-indexed `search_document(notes, props)` (index lookup, values-only, zero-config);
(2) Talus 38 view layers (`vw_*`) — attributes in the view's real relation columns, matched by
`_search_view_layer` which introspects text/number columns and builds the tsvector on the fly
(unindexed — a view is a filtered projection, searched as-is).

v2 (decision-feature-search-v2.md) adds three operators to the SLOT path (`_search_slot`), each an added path
that leaves the v1 exact tsquery byte-for-byte intact: (a) `*`/`?` WILDCARDS via `search_text ILIKE` on the
V067 trigram GIN; (b) automatic FUZZY fallback via `search_text %> f_unaccent(q)` + `word_similarity()` when the
exact pass is thin (word_similarity, not plain similarity — the per-row document is long); (c) slot coded-value
LABEL search resolved at query time (labels -> codes -> matched against props directly, no new index — F4).
Results are tiered (exact > wildcard 'partial' > fuzzy 'similar') and badged via `match_kind`. `=case-sensitive`
is a bounded v3. View layers run the same three modes over their on-the-fly document. Result rows
carry a representative point (fly) + a bbox (fitBounds for line/polygon), NOT full geometry — the popup fetches
that itself.

Injection discipline mirrors layer_query.py: a layer_id is slot-regex + platform.layers validated BEFORE
it ever names a table (`sql.Identifier`); the query text and caps are always bound parameters; the FTS
config is a single literal that MUST equal the V058 index config or the GIN index is bypassed.
"""
import re
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from ..auth import get_current_user
from ..authz import view_allowed
from ..db import get_db
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["search"])

_SLOT_RE = re.compile(r"^tal_[0-9a-f]{8}$")   # v1 scope gate AND the table-name safety proof (with the registry check)
_VIEW_RE = re.compile(r"^vw_[0-9a-f]{12}$")   # the other server-generated shape — the non-slot (view) search path
_TSCONFIG = "simple"     # MUST equal the V058 functional-index config, else the WHERE bypasses the GIN index.
_PER_LAYER_CAP = 50      # hits per layer (rank-ordered); a layer with more is flagged `capped` so the UI can
                         # say "refine to narrow" rather than silently truncating.
_MAX_LAYERS = 25         # bound the fan-out; a map rarely has more visible layers than this.
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 300

# ── v2 (fuzzy / wildcard / coded-label) — decision-feature-search-v2.md ───────────────────────────────────────
_MIN_LITERAL_RUN = 3     # trigram GIN accelerates ILIKE/%%> ONLY with a >=3-char literal run (verified live at
                         # V067: '%%co%%' seq-scans, '%%col%%' index-scans). Shorter wildcard/fuzzy terms are
                         # skipped (never a 38k-row seq scan per keystroke); the exact tsquery path is NOT guarded
                         # (tsvector serves any token length).
_FUZZY_TRIGGER = 5       # run the automatic fuzzy fallback ONLY when the exact pass yields fewer than this across
                         # all layers — a typo is unintentional, so nobody opts in; but the common (exact-rich)
                         # case must stay a single pass.
_WORD_SIM_THRESHOLD = 0.5  # word_similarity() floor for a fuzzy hit. 0.5 (not the 0.6 GUC default) — calibrated
                           # live: 'parcking'->'parking' scores 0.545, a real typo 0.6 would miss ('colege' 0.667,
                           # 'speedlimit' 0.643, 'destinaton' 0.727 all clear it). The index-driving `%%>` operator
                           # gates on the pg_trgm.word_similarity_threshold GUC, so the request sets it transaction-
                           # locally (set_config is_local) to THIS value; the explicit re-filter pins it too.
                           # word_similarity (NOT plain similarity): search_text is a LONG per-row document, so a
                           # short query dilutes under similarity() — see decision §2 (live-verified 1 vs 12 hits).
_WILDCARD_CHARS = ("*", "?")  # ArcGIS enhanced search: * = zero-or-more, ? = single char (no `**`).

# Talus 38 view layers (vw_*) expose REAL relation columns, not props — so they get a separate
# search core (below) that introspects those columns. These are the columns feature search NEVER
# includes: geometry, identity/audit, and the raw props blob (the slot path handles props).
# Everything else that is text / number is searchable.
_SKIP_COLS = frozenset(
    {
        "geom", "id", "row_version", "created_at", "created_by", "updated_at", "updated_by",
        "ai_provenance", "props",
    }
)
_TEXT_TYPES = frozenset({"text", "character varying", "character", "citext"})
_NUM_TYPES = frozenset({"integer", "bigint", "smallint", "numeric", "double precision", "real"})


def _label(notes: str | None, props: dict[str, Any] | None) -> str:
    """Best-effort display string for a result row: `notes` → first non-empty `props` value → "" (the
    caller falls back to a short id). Deliberately simple for v1 — the panel also shows the layer name +
    a geometry icon; a registry-`sort_order`-aware label is a later refinement (props key order is not
    meaningful in jsonb, so "first value" is heuristic, not authoritative)."""
    if notes and notes.strip():
        return notes.strip()[:120]
    if props:
        for value in props.values():
            if value is not None and str(value).strip():
                return str(value).strip()[:120]
    return ""


def _normalize_q(q: str) -> str:
    """Collapse whitespace + trim — the SAME normalization assets.search_text applies to the row side (V067),
    so an ILIKE/%%> comparison isn't broken by a double space the user typed (mirrors geocode_internal)."""
    return " ".join(q.split())


def _escape_like(q: str) -> str:
    """Escape LIKE/ILIKE metacharacters in user text so they match literally (mirrors geocode_internal)."""
    return q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _has_wildcard(q: str) -> bool:
    return any(c in q for c in _WILDCARD_CHARS)


def _has_operators(q: str) -> bool:
    """True if the query uses an explicit websearch operator — a phrase (`"…"`), a `-term` NOT, or a standalone
    `OR`. Such a query is DELIBERATELY precise, so the automatic fuzzy fallback is suppressed for it (fuzzily
    'correcting' `"exact phrase"` or `a -b` would violate the user's stated intent). Plain space-separated terms
    are implicit-AND, NOT an operator — those still get the fuzzy fallback when exact is thin."""
    if '"' in q:
        return True
    for tok in q.split():
        if (tok.startswith("-") and len(tok) > 1) or tok.lower() == "or":
            return True
    return False


def _longest_literal_run(q: str) -> int:
    """Longest maximal substring of `q` containing no wildcard metachar — the trigram-guard measure. A wildcard
    query needs one >=3-char literal run for the GIN to serve it; a plain (fuzzy) query is one run = len(q)."""
    return max((len(part) for part in re.split(r"[*?]", q)), default=0)


def _wildcard_to_ilike(q: str) -> str:
    """Translate ArcGIS wildcards to an ILIKE pattern: escape the user's literal %/_/\\ FIRST, then map * -> %
    and ? -> _ (so a literal % the user typed can't become a wildcard). Used with ESCAPE '\\'."""
    out: list[str] = []
    for ch in q:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "%":
            out.append(r"\%")
        elif ch == "_":
            out.append(r"\_")
        elif ch == "*":
            out.append("%")
        elif ch == "?":
            out.append("_")
        else:
            out.append(ch)
    return "".join(out)


async def _resolve_coded_codes(
    conn: AsyncConnection, layer_id: str, q: str, has_wildcard: bool
) -> dict[str, list[str]]:
    """F4 — slot coded-value LABEL search WITHOUT a new index. Slot props store the coded CODE; the LABEL lives
    in platform.domain_values. Resolve at query time: the layer's coded fields -> their domains -> the codes
    whose LABEL matches the user's term. The caller ORs `(props->>field) = ANY(codes)` into the exact/wildcard
    WHERE, so a label search becomes a code search the existing index already serves. Codes are matched against
    props DIRECTLY (not fed back through websearch_to_tsquery) — a hyphenated code like 'R7-8'/'D10-1' does not
    survive `simple` tokenization cleanly. All-small-tables join (layer_fields per layer, domain_values per
    domain), returned grouped by field_key so the match is precise (a code only matches its own field)."""
    if has_wildcard:
        pattern = "%" + _wildcard_to_ilike(q) + "%"   # contains, consistent with the slot document path
    else:
        pattern = "%" + _escape_like(q) + "%"   # plain term = label substring
    grouped: dict[str, list[str]] = {}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT lf.field_key, dv.code "
            "FROM platform.layer_fields lf "
            "JOIN platform.domain_values dv ON dv.domain_id = lf.domain_id AND dv.active "
            "WHERE lf.layer_id = %(lid)s AND lf.active AND lf.domain_id IS NOT NULL "
            "  AND platform.f_unaccent(dv.label) ILIKE platform.f_unaccent(%(pat)s::text) ESCAPE '\\'",
            {"lid": layer_id, "pat": pattern},
        )
        for field_key, code in await cur.fetchall():
            grouped.setdefault(field_key, []).append(code)
    return grouped


def _coded_or_clauses(
    coded: dict[str, list[str]], params: dict[str, Any]
) -> list[sql.Composed]:
    """`(props ->> :field) = ANY(:codes)` per coded field (F4), binding field + codes as params (never
    interpolated). Appended with OR to the exact/wildcard base predicate — a coded-label hit rides the same tier."""
    clauses: list[sql.Composed] = []
    for i, (field_key, codes) in enumerate(coded.items()):
        clauses.append(
            sql.SQL("(props ->> {fk}) = ANY({codes})").format(
                fk=sql.Placeholder(f"cfk_{i}"), codes=sql.Placeholder(f"ccodes_{i}")
            )
        )
        params[f"cfk_{i}"] = field_key
        params[f"ccodes_{i}"] = codes
    return clauses


async def _registered_layer(conn: AsyncConnection, layer_id: str) -> bool:
    """True iff layer_id matches one of the two server-generated id shapes AND is a real
    platform.layers row (bound param) — the same shape-then-registry proof as
    layer_query._require_registered, minus the raise (a missing/unknown/unshaped id is silently
    skipped, not an error, since the panel may pass a mix of layer kinds). The shape gate also
    proves the dispatch below: any id that passes and fails _SLOT_RE IS a vw_* view.
    Safe on its own: the id then names a relation only via psycopg sql.Identifier, which quotes it."""
    if not (_SLOT_RE.fullmatch(layer_id) or _VIEW_RE.fullmatch(layer_id)):
        return False
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.layers WHERE layer_id = %s", (layer_id,))
        return (await cur.fetchone()) is not None


async def _view_columns(conn: AsyncConnection, layer_id: str) -> list[tuple[str, str]]:
    """(column_name, data_type) for assets.<layer_id>, ordinal order (bound param) — the introspection
    layer_query uses. The whitelist for the view-layer search core: only a name in here names a column."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'assets' AND table_name = %s ORDER BY ordinal_position",
            (layer_id,),
        )
        return [(r[0], r[1]) for r in await cur.fetchall()]


async def _search_view_layer(
    conn: AsyncConnection, layer_id: str, mode: str, q: str, cap: int
) -> list[dict[str, Any]]:
    """Search a Talus 38 view layer (`vw_*`) — attributes in the view's real relation columns, NOT
    props, so search_document/V058 don't apply. Introspect the columns and build one document `doc`
    on the fly from every text + number column (cast to text). `mode` (v2) picks the engine over
    that SAME `doc`, mirroring `_search_slot`:
      - 'exact'    : v1 verbatim — `to_tsvector('simple', doc) @@ websearch_to_tsquery('simple', q)`.
      - 'wildcard' : `f_unaccent(doc) ILIKE f_unaccent('%%<translated>%%')` (contains; accent+case folded).
      - 'fuzzy'    : `f_unaccent(doc) %> f_unaccent(q)` + `word_similarity() >= threshold`.
    Unindexed — the document composes per request, so a search here scans the view (this was the
    retired built-ins' search core; it survives because view layers land in the same non-slot
    branch, decision-retire-builtins.md §2.3). Same view-gate (caller), same point/bbox contract
    as the slot path; `label` = first non-empty display value."""
    cols = await _view_columns(conn, layer_id)
    names = {c for c, _ in cols}
    if "geom" not in names or "id" not in names:
        return []  # feature search is map-oriented — a geometry-less / id-less table has nothing to place
    doc_parts: list[sql.Composed] = []   # concatenated into the searchable tsvector
    disp_parts: list[sql.Composed] = []  # first-non-empty of these = the fallback display label
    prop_kv: list[sql.Composed] = []     # name,value pairs -> a `properties` jsonb (frontend labels by titleField)
    joins: list[sql.Composed] = []
    di = 0
    for name, dtype in cols:
        if name in _SKIP_COLS:
            continue
        col = sql.Identifier("t", name)
        if dtype in _TEXT_TYPES or dtype in _NUM_TYPES:
            doc_parts.append(sql.SQL("coalesce({c}::text,'')").format(c=col))
            prop_kv.append(sql.Literal(name))
            prop_kv.append(col)
            if dtype in _TEXT_TYPES:
                disp_parts.append(sql.SQL("{c}").format(c=col))
    if not doc_parts:
        return []

    doc = sql.SQL(" || ' ' || ").join(doc_parts)
    # Fallback label = first non-empty display value, else a short id (SQL — jsonb key order isn't stable).
    # The frontend prefers properties[titleField]; this is only used when that is absent/empty.
    label_expr = sql.SQL("coalesce({parts}, '#' || left(t.id::text, 8))").format(
        parts=sql.SQL(", ").join(
            sql.SQL("nullif(btrim({e}::text), '')").format(e=e) for e in disp_parts
        )
        if disp_parts
        else sql.SQL("NULL")
    )
    props_obj = (
        sql.SQL("jsonb_build_object({kv})").format(kv=sql.SQL(", ").join(prop_kv))
        if prop_kv
        else sql.SQL("'{}'::jsonb")
    )
    cfg = sql.Literal(_TSCONFIG)
    params: dict[str, Any] = {"q": q, "cap": cap}
    if mode == "exact":
        score = sql.SQL("ts_rank(to_tsvector({cfg}, {doc}), websearch_to_tsquery({cfg}, %(q)s))").format(
            cfg=cfg, doc=doc
        )
        where = sql.SQL("to_tsvector({cfg}, {doc}) @@ websearch_to_tsquery({cfg}, %(q)s)").format(
            cfg=cfg, doc=doc
        )
    elif mode == "wildcard":
        params["pattern"] = "%" + _wildcard_to_ilike(q) + "%"   # contains, like the slot path
        score = sql.SQL(
            "word_similarity(platform.f_unaccent(%(q)s::text), platform.f_unaccent({doc}))"
        ).format(doc=doc)
        where = sql.SQL(
            "platform.f_unaccent({doc}) ILIKE platform.f_unaccent(%(pattern)s::text) ESCAPE '\\'"
        ).format(doc=doc)
    else:  # fuzzy — `%%>` is psycopg's doubled word_similarity operator
        params["thr"] = _WORD_SIM_THRESHOLD
        score = sql.SQL(
            "word_similarity(platform.f_unaccent(%(q)s::text), platform.f_unaccent({doc}))"
        ).format(doc=doc)
        where = sql.SQL(
            "platform.f_unaccent({doc}) %%> platform.f_unaccent(%(q)s::text) "
            "AND word_similarity(platform.f_unaccent(%(q)s::text), platform.f_unaccent({doc})) >= %(thr)s"
        ).format(doc=doc)

    stmt = sql.SQL(
        "SELECT t.id::text AS id, "
        "ST_X(ST_PointOnSurface(g)) AS lon, ST_Y(ST_PointOnSurface(g)) AS lat, "
        "ST_XMin(g) AS minx, ST_YMin(g) AS miny, ST_XMax(g) AS maxx, ST_YMax(g) AS maxy, "
        "{label} AS label, {props} AS properties, {score} AS rank "
        "FROM {tbl} AS t {joins} "
        "CROSS JOIN LATERAL (SELECT ST_Transform(t.geom, 4326) AS g) gt "
        "WHERE {where} "
        "ORDER BY rank DESC "
        "LIMIT %(cap)s"
    ).format(
        label=label_expr,
        props=props_obj,
        score=score,
        where=where,
        tbl=sql.Identifier("assets", layer_id),
        joins=sql.SQL(" ").join(joins) if joins else sql.SQL(""),
    )
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(stmt, params)
        return await cur.fetchall()


# The slot projection is identical across the three match modes — only the score expression and the WHERE
# predicate change. geom is reprojected to 4326 once per row (LATERAL `g`) for the representative point + bbox.
_SLOT_SELECT = (
    "SELECT id::text AS id, "
    "ST_X(ST_PointOnSurface(g)) AS lon, ST_Y(ST_PointOnSurface(g)) AS lat, "
    "ST_XMin(g) AS minx, ST_YMin(g) AS miny, ST_XMax(g) AS maxx, ST_YMax(g) AS maxy, "
    "notes, props, {score} AS rank "
    "FROM {tbl} "
    "CROSS JOIN LATERAL (SELECT ST_Transform(geom, 4326) AS g) gt "
    "WHERE {where} "
    "ORDER BY rank DESC "
    "LIMIT %(cap)s"
)


async def _search_slot(
    conn: AsyncConnection,
    layer_id: str,
    mode: str,
    q: str,
    coded: dict[str, list[str]],
    cap: int,
) -> list[dict[str, Any]]:
    """Run ONE matching pass on a validated, view-gated slot. `mode` picks the engine (decision-feature-search-v2):
      - 'exact'    : v1 VERBATIM — `assets.search_document(notes, props) @@ websearch_to_tsquery('simple', q)`,
                     ts_rank ordered. Hits the V058 tsvector GIN (token-exact; NOT accent-folded — v1 behavior).
      - 'wildcard' : `assets.search_text(notes, props) ILIKE <translated pattern>`. Hits the V067 trigram GIN
                     (caller has already checked the >=3-char literal-run guard).
      - 'fuzzy'    : `assets.search_text(...) %> f_unaccent(q) AND word_similarity(...) >= threshold`. V067 GIN.
    For 'exact'/'wildcard', any F4 coded-label codes are ORed into the WHERE (same tier). `search_text`/
    f_unaccent MUST match the V067 index expression verbatim or the GIN is bypassed."""
    tbl = sql.Identifier("assets", layer_id)
    params: dict[str, Any] = {"q": q, "cap": cap}
    if mode == "exact":
        score = sql.SQL(
            "ts_rank(assets.search_document(notes, props), websearch_to_tsquery({cfg}, %(q)s))"
        ).format(cfg=sql.Literal(_TSCONFIG))
        base = sql.SQL(
            "assets.search_document(notes, props) @@ websearch_to_tsquery({cfg}, %(q)s)"
        ).format(cfg=sql.Literal(_TSCONFIG))
    elif mode == "wildcard":
        # CONTAINS semantics (%…%): search_text is a CONCATENATED multi-field document ("12345 100 COLLEGE DR
        # …"), so an anchored ILIKE would never match a field in the middle. `COLL*` -> `%COLL%`, `COLLEG?` ->
        # `%COLLEG_%`. Deviates from ArcGIS's per-field prefix semantics deliberately — anchoring is meaningless
        # against a document; contains is the useful behavior AND stays index-backed (the >=3-char literal run).
        params["pattern"] = "%" + _wildcard_to_ilike(q) + "%"
        score = sql.SQL(
            "word_similarity(platform.f_unaccent(%(q)s::text), assets.search_text(notes, props))"
        )
        base = sql.SQL(
            "assets.search_text(notes, props) ILIKE platform.f_unaccent(%(pattern)s::text) ESCAPE '\\'"
        )
    else:  # fuzzy — `%%>` is psycopg's doubled `%>` (word_similarity operator); GUC-default threshold re-pinned.
        params["thr"] = _WORD_SIM_THRESHOLD
        score = sql.SQL(
            "word_similarity(platform.f_unaccent(%(q)s::text), assets.search_text(notes, props))"
        )
        base = sql.SQL(
            "assets.search_text(notes, props) %%> platform.f_unaccent(%(q)s::text) "
            "AND word_similarity(platform.f_unaccent(%(q)s::text), assets.search_text(notes, props)) >= %(thr)s"
        )

    where: sql.Composable = base
    if mode in ("exact", "wildcard"):
        coded_clauses = _coded_or_clauses(coded, params)
        if coded_clauses:
            where = sql.SQL(" OR ").join([sql.SQL("({b})").format(b=base), *coded_clauses])

    stmt = sql.SQL(_SLOT_SELECT).format(tbl=tbl, score=score, where=where)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(stmt, params)
        return await cur.fetchall()


_TIER_RANK = {"exact": 0, "partial": 1, "similar": 2}   # display order within a layer (decision §6)


@router.get(
    "/search",
    summary="Cross-layer feature search by attribute (view-gated) — the map Locate panel",
)
async def search_features(
    q: str = Query(
        ...,
        description='Search query. Exact websearch syntax (phrase "...", AND, OR, -NOT); */? wildcards; '
        "automatic fuzzy fallback when exact is thin; slot coded-value labels.",
    ),
    layers: str = Query("", description="Comma-separated layer ids to search (the map's visible layers)."),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT, description="Overall result cap across layers."),
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    q = _normalize_q(q or "")
    # de-dup layer ids preserving the caller's (visible) order, and bound the fan-out
    ordered: list[str] = []
    seen_layers: set[str] = set()
    for lid in (s for s in layers.split(",") if s):
        if lid not in seen_layers:
            seen_layers.add(lid)
            ordered.append(lid)
    ordered = ordered[:_MAX_LAYERS]

    results: list[dict[str, Any]] = []
    capped: list[str] = []  # layers with more than _PER_LAYER_CAP primary hits (UI hint: "refine to narrow")
    if not q or not ordered:
        return {"query": q, "results": results, "counts": {}, "capped": capped}

    has_wc = _has_wildcard(q)
    # Trigram (wildcard/fuzzy) needs a >=3-char literal run, else the GIN degenerates to a per-keystroke seq scan
    # over every slot — below the floor those paths are skipped (the exact tsquery path is NOT affected).
    trigram_ok = _longest_literal_run(q) >= _MIN_LITERAL_RUN
    seen: set[tuple[str, str]] = set()  # (layer_id, feature_id) — dedup keeps the first (best-tier) hit

    def add_rows(lid: str, rows: list[dict[str, Any]], kind: str) -> None:
        for r in rows:
            key = (lid, r["id"])
            if key in seen:  # already found in a higher tier (exact before fuzzy) — keep that one
                continue
            seen.add(key)
            slot_props = r.get("props") or {}
            # Built-ins return `properties` (their real fields); slots use props (+ notes) — so the frontend can
            # label each result by the layer's title field (popupTitleField) either way.
            properties = r.get("properties")
            if properties is None:
                properties = {**slot_props, **({"notes": r["notes"]} if r.get("notes") else {})}
            lon, lat = r.get("lon"), r.get("lat")
            results.append(
                {
                    "layer_id": lid,
                    "id": r["id"],
                    "label": r.get("label") or _label(r.get("notes"), slot_props) or f"#{str(r['id'])[:8]}",
                    "properties": properties,
                    "rank": float(r["rank"]) if r.get("rank") is not None else 0.0,
                    "match_kind": kind,  # 'exact' | 'partial' (wildcard) | 'similar' (fuzzy) — the panel badges it
                    "point": [lon, lat] if lon is not None and lat is not None else None,
                    "bbox": (
                        [r["minx"], r["miny"], r["maxx"], r["maxy"]] if r.get("minx") is not None else None
                    ),
                }
            )

    # ── Primary pass: wildcard (if the query has */?) else exact; each layer view-gated + fail-closed. Fetch
    #    one past the cap to detect (and flag) truncation without a second count query. ──
    for lid in ordered:
        if not await _registered_layer(conn, lid):  # proves the id is a real layer BEFORE it names a table
            continue
        if not await view_allowed(user, lid):       # FAIL-CLOSED: never search a layer the caller can't view
            continue
        try:
            if _SLOT_RE.match(lid):
                if has_wc:
                    if not trigram_ok:
                        continue  # wildcard term too short to index — skip rather than scan the slot
                    coded = await _resolve_coded_codes(conn, lid, q, True)
                    rows = await _search_slot(conn, lid, "wildcard", q, coded, _PER_LAYER_CAP + 1)
                    kind = "partial"
                else:
                    coded = await _resolve_coded_codes(conn, lid, q, False) if trigram_ok else {}
                    rows = await _search_slot(conn, lid, "exact", q, coded, _PER_LAYER_CAP + 1)
                    kind = "exact"
            else:
                # View layers: the same three modes over their on-the-fly document (F4 query-time
                # coded-label resolution is a slot-only concern — a view has no props fields).
                if has_wc:
                    if not trigram_ok:
                        continue
                    rows = await _search_view_layer(conn, lid, "wildcard", q, _PER_LAYER_CAP + 1)
                    kind = "partial"
                else:
                    rows = await _search_view_layer(conn, lid, "exact", q, _PER_LAYER_CAP + 1)
                    kind = "exact"
        except Exception:  # one odd layer must not 500 the whole panel — log + skip (view gate already passed)
            log.warning("feature_search_layer_failed", layer_id=lid, exc_info=True)
            continue
        if len(rows) > _PER_LAYER_CAP:
            rows = rows[:_PER_LAYER_CAP]
            capped.append(lid)
        add_rows(lid, rows, kind)
        if len(results) >= limit:
            break

    # ── Automatic fuzzy fallback (F2): plain queries (no wildcard, no explicit operator), ONLY when exact came
    #    back thin. A `"phrase"` / `-NOT` / `OR` query is deliberately precise, so fuzzy is suppressed for it —
    #    respect the exact zero. Re-gates every layer (EVERY match path is view-gated, fail-closed) and dedups
    #    against the exact hits above. ──
    if not has_wc and not _has_operators(q) and trigram_ok and len(results) < _FUZZY_TRIGGER and len(results) < limit:
        # Lower the word_similarity operator's threshold for THIS request only. get_db yields a non-autocommit
        # pooled connection (an implicit per-request transaction), so is_local=true scopes the change to it and
        # auto-resets on return to the pool — the `%>` operator (index-driving) and the re-filter then agree.
        async with conn.cursor() as _cur:
            await _cur.execute(
                "SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)",
                (str(_WORD_SIM_THRESHOLD),),
            )
        for lid in ordered:
            if not await _registered_layer(conn, lid):
                continue
            if not await view_allowed(user, lid):
                continue
            try:
                rows = await (
                    _search_slot(conn, lid, "fuzzy", q, {}, _PER_LAYER_CAP)
                    if _SLOT_RE.match(lid)
                    else _search_view_layer(conn, lid, "fuzzy", q, _PER_LAYER_CAP)
                )
            except Exception:
                log.warning("feature_search_fuzzy_failed", layer_id=lid, exc_info=True)
                continue
            add_rows(lid, rows, "similar")
            if len(results) >= limit:
                break

    # Tiered order the panel can trust: grouped by the caller's visible-layer order, exact above partial above
    # fuzzy within each layer, rank desc within a tier (decision §6).
    layer_pos = {lid: i for i, lid in enumerate(ordered)}
    results.sort(
        key=lambda r: (layer_pos.get(r["layer_id"], _MAX_LAYERS), _TIER_RANK[r["match_kind"]], -r["rank"])
    )
    results = results[:limit]

    counts: dict[str, int] = {}
    for r in results:
        counts[r["layer_id"]] = counts.get(r["layer_id"], 0) + 1
    log.info(
        "feature_search",
        user_id=str(user.id),
        n_layers=len(ordered),
        n_searched=len(counts),
        n_results=len(results),
        wildcard=has_wc,
        fuzzy=any(r["match_kind"] == "similar" for r in results),
    )
    return {"query": q, "results": results, "counts": counts, "capped": capped}
