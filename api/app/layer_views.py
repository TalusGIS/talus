"""Talus 38 — view layers: the safe runtime-DDL builder + the view store.

A "view layer" is a sanitized lens over a SOURCE layer: a real PostgreSQL VIEW
(``assets.vw_<hex>``) exposing only an allow-listed column subset + a baked WHERE, registered
as its own ``platform.layers`` item with its own OpenFGA ``layer`` object and its own sharing.
This module owns the SECURITY-CRITICAL machinery — the scoped ``CREATE``/``DROP VIEW`` builder
and the ``platform.layer_views`` store. The HTTP surface + the author gate (admin OR the source
layer's owner) live in ``routers/layer_views.py``.

**F-DDL — the app's FIRST DDL outside Flyway.** A view over an
arbitrary source can't be pre-minted in a migration, so the view is created at runtime. The
scoping this module enforces:

* The view name is **server-generated**, never user-derived: ``vw_<12 hex>``, wrapped in
  ``sql.Identifier`` (a valid identifier, collision-resistant).
* **Columns** come from the source's ``information_schema`` ∪ its ACTIVE owner-defined field
  keys (allow-list ∩ that server-derived set; decision-view-props-fields.md). A real column is
  wrapped in ``sql.Identifier``; a props field bakes as an extraction + declared-type cast whose
  json key is ``sql.Literal`` and whose output name is ``sql.Identifier`` over the proven key
  (V044 grammar + reserved-key CHECK) — no free-text name reaches the DDL either way. The raw
  ``props`` container is refused (an open container would defeat the closed allow-list).
* The **WHERE** reuses the validated ``field:op:value`` grammar (``layer_query._VALID_OPS`` +
  the same column ∪ field-key set), emitting ``sql.Literal`` constants — a persistent view def
  has no bind parameters, so values are baked as properly-escaped literals, never raw SQL.
* The only DDL verbs are ``CREATE VIEW`` / ``DROP VIEW``, scoped to ``assets.vw_*``. The
  ``CREATE`` + the registry INSERTs ride **one transaction** (PostgreSQL DDL is transactional),
  so a half-created view can't persist.

Sanitization is therefore **DB-enforced** (F-A): the view physically only CONTAINS the
allow-listed columns + WHERE-passing rows, so the generic ``/layers/{id}/features`` + ``/export``
paths (which introspect ``assets.<view_id>``) can only ever serve — and only ever NARROW — the
sanitized set. Authz gates on the VIEW's own OpenFGA object, independent of the source.
"""
import json
import re
import secrets
from typing import Any
from uuid import UUID

import structlog
from psycopg import AsyncConnection, sql

from .authz import _openfga_delete, _openfga_read_object_tuples, _openfga_write
from .routers.layer_query import (
    _PROPS_TYPE_NAME,
    _VALID_OPS,
    _columns,
    _props_cast,
    _props_fields,
    _require_registered,
)

log = structlog.get_logger(__name__)

# Tags the audit row's client_info via the V003 trigger's app.current_client GUC (matches publisher.py).
_AUDIT_CLIENT = "portal-api"

# Always included in the view when present in the source, regardless of the allow-list: `id`
# (feature identity + the stable sort tiebreak the feature endpoint needs; a gen_random_uuid leaks
# nothing) and `geom` (required for map render). The authoring UI surfaces these as auto-included.
_ALWAYS_COLUMNS = ("id", "geom")

# --- join views (V080, decision-relates-and-join-views.md PR B) ------------------

# Fork 4½: the author-chosen joined-column prefix. Tight on purpose — combined `<alias>_<key>`
# must fit PG's 63-char identifier limit with headroom for real field keys.
_JOIN_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,20}\Z")
# Keys/columns on either join side must already satisfy the layer_fields key grammar (native
# columns like `id`/`notes` match it too). Membership in the side's validity set is the real gate;
# this regex just refuses garbage before any lookup. \Z (not $) — $ admits a trailing newline;
# harmless inside sql.Identifier, but no reason to allow it (security-review tidy, 2026-07-15).
_JOIN_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}\Z")
_MAX_IDENT = 63  # PostgreSQL identifier length limit — over-cap 422s, never silently truncates
_SORT_DIRS = {"asc": sql.SQL("ASC"), "desc": sql.SQL("DESC")}
# Never joinable/exposable through a join (mirrors relates._UNRELATABLE_COLUMNS): geometry can't
# exist on a table anyway, `props` is the jsonb container (its KEYS join, not the blob), and the
# bookkeeping pair is meaningless prefixed into another layer.
_UNJOINABLE_COLUMNS = frozenset({"geom", "props", "row_version", "ai_provenance"})


# --- view name (server-generated, never user-derived) ---------------------------


def _new_view_id() -> str:
    """A server-generated, collision-resistant view layer id: ``vw_<12 hex>``. Never derived from
    user input; a valid SQL identifier; the ``assets.<id>`` VIEW and the ``layer:<id>`` OpenFGA
    object share it. ``secrets`` (not ``random``) so the id isn't predictable."""
    return f"vw_{secrets.token_hex(6)}"


async def _require_unused(conn: AsyncConnection, view_id: str) -> None:
    """Guard the (vanishingly unlikely) generated-name collision BEFORE any write — a reused id must
    never re-point at an existing object. The PK constraint is the backstop; this keeps a collision
    from leaving a dead OpenFGA tuple (which is written before the registry INSERT)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.layers WHERE layer_id = %s", (view_id,))
        if await cur.fetchone() is not None:
            raise ValueError("generated view id collided; retry")


# --- column resolution + the validated WHERE compiler ---------------------------


def _resolve_view_columns(
    allowed: list[str],
    source_cols: dict[str, str],
    source_fields: dict[str, tuple[str, str | None, str]],
) -> list[str]:
    """The final column set the view SELECTs: the allow-list ∩ (the source's real columns ∪ its
    ACTIVE owner-defined field keys), plus ``id``/``geom`` when the source has them (auto-included).
    Real columns keep the source's ordinal order; field keys follow in the field designer's order
    (both inputs arrive ordered). The V044 reserved-key CHECK keeps the two sets disjoint, so a name
    resolves unambiguously to a column OR a props extraction. Raises ``ValueError`` for any
    requested name that is neither — fail-closed: a typo'd name 422s, it never silently widens or
    vanishes. The raw ``props`` container is refused outright (decision-view-props-fields.md fork
    C): an open container defeats the closed allow-list — a field the owner adds NEXT YEAR must not
    silently flow through a view authored today."""
    requested = list(allowed)
    if "props" in requested:
        raise ValueError("the raw 'props' container is not exposable — allow-list its fields individually")
    unknown = [c for c in requested if c not in source_cols and c not in source_fields]
    if unknown:
        raise ValueError(f"unknown column(s) for source: {sorted(set(unknown))}")
    keep = set(requested) | {c for c in _ALWAYS_COLUMNS if c in source_cols}
    cols = [c for c in source_cols if c in keep]  # source ordinal order
    cols += [k for k in source_fields if k in keep]  # then field keys, designer order
    if not cols:
        raise ValueError("a view must expose at least one column")
    return cols


def _compile_view_where(
    filters: list[dict[str, Any]],
    source_cols: dict[str, str],
    source_fields: dict[str, tuple[str, str | None, str]],
) -> sql.Composed:
    """Compile ``[{field,op,value}]`` into one validated WHERE predicate for baking into the persistent
    view def. Same validation as ``layer_query._parse_filters`` (field must be a real source column or
    an ACTIVE owner-defined field key; op must be in ``_VALID_OPS``) and the same op semantics — a real
    column's text ops compare via ``::text`` and its range ops stay native; a props field's text ops
    compare the raw ``->>`` extraction and its range ops cast via the FIXED declared-type map. It emits
    ``sql.Literal`` constants (the props KEY included), because a stored view has no bind parameters;
    ``sql.Literal`` escapes every value, so no raw SQL from the user ever reaches the DDL. All ten
    ``_VALID_OPS`` compile explicitly — the pre-slice version handled five and silently baked the rest
    as ILIKE (an op like ``gt`` became a substring match; API-reachable, never UI-offered). The raw
    ``props`` container is refused as a filter field (fork C — matching text against the whole jsonb
    blob is never a meaningful predicate). Returns ``TRUE`` for an empty filter list."""
    valid = set(source_cols) - {"props"}
    clauses: list[sql.Composed] = []
    for f in filters:
        field, op, value = f.get("field"), f.get("op"), f.get("value")
        if not isinstance(field, str) or (field not in valid and field not in source_fields):
            raise ValueError(f"unknown filter field {field!r}")
        if op not in _VALID_OPS:
            raise ValueError(f"unknown filter op {op!r}")
        if not isinstance(value, str):
            raise ValueError(f"filter value for {field!r} must be a string")
        if field in valid:
            col = sql.Identifier("t", field)
            lhs_text = sql.SQL("{c}::text").format(c=col)
            lhs_ranged = sql.SQL("{c}").format(c=col)
        else:  # owner-defined props field — key baked as a LITERAL, cast from the declared-type map
            key = sql.Literal(field)
            lhs_text = sql.SQL("(t.props ->> {k})").format(k=key)
            lhs_ranged = sql.SQL("((t.props ->> {k}){cast})").format(
                k=key, cast=_props_cast(source_fields[field][0])
            )
        if op in ("in", "notin"):
            arr = sql.SQL("ARRAY[{vals}]::text[]").format(
                vals=sql.SQL(", ").join(sql.Literal(v) for v in value.split(","))
            )
            tmpl = "{l} = ANY({a})" if op == "in" else "NOT ({l} = ANY({a}))"
            clauses.append(sql.SQL(tmpl).format(l=lhs_text, a=arr))
        elif op == "eq":
            clauses.append(sql.SQL("{l} = {v}").format(l=lhs_text, v=sql.Literal(value)))
        elif op == "ne":
            clauses.append(sql.SQL("{l} <> {v}").format(l=lhs_text, v=sql.Literal(value)))
        elif op in ("gte", "lte", "gt", "lt"):
            cmp = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}[op]
            clauses.append(
                sql.SQL("{l} " + cmp + " {v}").format(l=lhs_ranged, v=sql.Literal(value))
            )
        elif op == "like":
            clauses.append(sql.SQL("{l} LIKE {v}").format(l=lhs_text, v=sql.Literal(value)))
        else:  # ilike
            clauses.append(sql.SQL("{l} ILIKE {v}").format(l=lhs_text, v=sql.Literal(f"%{value}%")))
    return sql.SQL(" AND ").join(clauses) if clauses else sql.SQL("TRUE")


# --- the join compiler (V080 — one LEFT JOIN LATERAL, 1:first) -------------------


def _join_side_valid(cols: dict[str, str], fields: dict[str, tuple[str, str | None, str]]) -> set[str]:
    """The keys one join side may bind or expose: real scalar columns (minus the unjoinable set)
    ∪ ACTIVE owner-defined field keys. Both server-derived; the V044 reserved-key CHECK keeps the
    two sets disjoint, so a key resolves unambiguously to a column OR a props extraction."""
    return {c for c in cols if c not in _UNJOINABLE_COLUMNS} | set(fields)


def _key_expr_text(alias: str, key: str, cols: dict[str, str]) -> sql.Composed:
    """One side of the join equality, AS TEXT. A physical column compares ``::text``; a props
    field's ``->>`` extraction is already text. Text-on-both-sides is deliberate (matches the
    filter grammar's ``eq`` semantics): the equality can never fail to compile for type reasons,
    so a bad pairing surfaces as an empty join, never a 500 at CREATE."""
    if key in cols:
        return sql.SQL("{c}::text").format(c=sql.Identifier(alias, key))
    return sql.SQL("({p} ->> {k})").format(p=sql.Identifier(alias, "props"), k=sql.Literal(key))


def _source_out_expr(
    key: str, source_cols: dict[str, str], source_fields: dict[str, tuple[str, str | None, str]]
) -> sql.Composed:
    """One item of the view's SELECT list, on the source alias ``t`` (the plain-view mirror of
    ``_table_out_expr``). A real column passes through natively (its name is its output name); a
    props field extracts + casts via the FIXED declared-type map and is named ``AS`` its key — so
    the view's introspected column carries the field key and its declared type, and every
    downstream door (features/filters/sort/export/tiles) serves it as an ordinary column. The key
    reaches the DDL only as ``sql.Literal`` (json path) and ``sql.Identifier`` (output name), and
    only AFTER ``_resolve_view_columns`` proved it a member of the server-derived ACTIVE field set."""
    if key in source_cols:
        return sql.SQL("{c}").format(c=sql.Identifier("t", key))
    return sql.SQL("(({p} ->> {k}){cast}) AS {n}").format(
        p=sql.Identifier("t", "props"), k=sql.Literal(key),
        cast=_props_cast(source_fields[key][0]), n=sql.Identifier(key),
    )


def _table_out_expr(key: str, cols: dict[str, str], fields: dict[str, tuple[str, str | None, str]]) -> sql.Composed:
    """A joined table column/field as a typed output expression on the lateral alias ``t2``. A
    physical column passes through natively; a props field extracts + casts via the FIXED declared-
    type map, so the view's introspected column carries the real type (filters/sort work on it)."""
    if key in cols:
        return sql.SQL("{c}").format(c=sql.Identifier("t2", key))
    return sql.SQL("(({p} ->> {k}){cast})").format(
        p=sql.Identifier("t2", "props"), k=sql.Literal(key), cast=_props_cast(fields[key][0])
    )


def _compile_join(
    *,
    table_id: str,
    spec: dict[str, Any],
    source_cols: dict[str, str],
    source_fields: dict[str, tuple[str, str | None, str]],
    table_cols: dict[str, str],
    table_fields: dict[str, tuple[str, str | None, str]],
) -> tuple[sql.Composed, list[str]]:
    """Validate a join spec against BOTH live sides and compile the ``LEFT JOIN LATERAL`` clause.
    Returns ``(join_clause, prefixed_output_names)``. Fork 4: 1:first — the lateral subquery sorts
    by the author-picked table field (``NULLS LAST``) with a deterministic ``t2.id`` tiebreak and
    takes ``LIMIT 1``. Fork 4½: every output column is ``<alias>_<key>`` (collision-proof by
    construction). All validation raises ``ValueError`` (→ 422) BEFORE any DDL is assembled."""
    layer_key = spec.get("layer_key")
    table_key = spec.get("table_key")
    alias = spec.get("alias")
    columns = spec.get("columns")
    sort_key = spec.get("sort_key")
    sort_dir = spec.get("sort_dir", "desc")

    if not isinstance(alias, str) or not _JOIN_ALIAS_RE.match(alias):
        raise ValueError("join alias must match ^[a-z][a-z0-9_]{0,20}$")
    for name, key in (("layer_key", layer_key), ("table_key", table_key), ("sort_key", sort_key)):
        if not isinstance(key, str) or not _JOIN_KEY_RE.match(key):
            raise ValueError(f"join {name} must be a valid field key")
    if sort_dir not in _SORT_DIRS:
        raise ValueError("join sort_dir must be 'asc' or 'desc'")
    if not isinstance(columns, list) or not columns or not all(isinstance(c, str) for c in columns):
        raise ValueError("join columns must be a non-empty list of field keys")

    source_valid = _join_side_valid(source_cols, source_fields)
    table_valid = _join_side_valid(table_cols, table_fields)
    if layer_key not in source_valid:
        raise ValueError(f"unknown join layer_key {layer_key!r} for the source layer")
    if table_key not in table_valid:
        raise ValueError(f"unknown join table_key {table_key!r} for table {table_id!r}")
    if sort_key not in table_valid:
        raise ValueError(f"unknown join sort_key {sort_key!r} for table {table_id!r}")
    unknown = sorted({c for c in columns if c not in table_valid})
    if unknown:
        raise ValueError(f"unknown join column(s) for table {table_id!r}: {unknown}")

    out_names: list[str] = []
    sel_items: list[sql.Composed] = []
    seen: set[str] = set()
    for key in columns:
        if key in seen:  # duplicates collapse silently-nowhere; refuse instead
            raise ValueError(f"duplicate join column {key!r}")
        seen.add(key)
        out = f"{alias}_{key}"
        if len(out) > _MAX_IDENT:
            raise ValueError(
                f"joined column name {out!r} exceeds {_MAX_IDENT} chars — shorten the alias"
            )
        out_names.append(out)
        sel_items.append(
            sql.SQL("{e} AS {n}").format(e=_table_out_expr(key, table_cols, table_fields), n=sql.Identifier(out))
        )

    # ORDER BY: physical column sorts natively; props field sorts via the declared-type cast.
    if sort_key in table_cols:
        sort_expr: sql.Composable = sql.Identifier("t2", sort_key)
    else:
        sort_expr = sql.SQL("(({p} ->> {k}){cast})").format(
            p=sql.Identifier("t2", "props"),
            k=sql.Literal(sort_key),
            cast=_props_cast(table_fields[sort_key][0]),
        )

    clause = sql.SQL(
        "LEFT JOIN LATERAL (SELECT {sel} FROM {tbl} AS t2 WHERE {tk} = {lk} "
        "ORDER BY {sort} {dir} NULLS LAST, t2.id LIMIT 1) AS j ON TRUE"
    ).format(
        sel=sql.SQL(", ").join(sel_items),
        tbl=sql.Identifier("assets", table_id),
        tk=_key_expr_text("t2", table_key, table_cols),
        lk=_key_expr_text("t", layer_key, source_cols),
        sort=sort_expr,
        dir=_SORT_DIRS[sort_dir],
    )
    return clause, out_names


async def _require_join_table(conn: AsyncConnection, table_id: str) -> None:
    """The joined end must be a registered, ready, standalone TABLE (geom_type='none'). Positive
    membership like relates' origin check — a view layer's NULL geom_type must not slip in."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT geom_type, status FROM platform.layers WHERE layer_id = %s", (table_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise ValueError(f"join table {table_id!r} not found")
    geom_type, l_status = row
    if geom_type != "none" or l_status != "ready":
        raise ValueError("a join view joins exactly one standalone table (geom_type='none')")


async def _resolve_join(
    conn: AsyncConnection,
    source_id: str,
    source_cols: dict[str, str],
    source_fields: dict[str, tuple[str, str | None, str]],
    join_table_id: str | None,
    join_spec: dict[str, Any] | None,
) -> tuple[sql.Composed | None, list[str]]:
    """Both-or-neither guard + live-side validation + compile. ``(None, [])`` for a plain view.
    ``source_fields`` arrives from the caller — register/update already fetch it for the column
    resolver and WHERE compiler, so the live read happens exactly once."""
    if (join_table_id is None) != (join_spec is None):
        raise ValueError("join_table_id and join_spec must be provided together")
    if join_table_id is None or join_spec is None:
        return None, []
    if join_table_id == source_id:
        raise ValueError("a view cannot join its own source")
    await _require_join_table(conn, join_table_id)
    table_cols = await _columns(conn, join_table_id)
    if not table_cols:
        raise ValueError(f"join table {join_table_id!r} has no backing table")
    return _compile_join(
        table_id=join_table_id,
        spec=join_spec,
        source_cols=source_cols,
        source_fields=source_fields,
        table_cols=table_cols,
        table_fields=await _props_fields(conn, join_table_id),
    )


# --- the scoped DDL builders (assets.vw_* only) ---------------------------------


def _create_view_sql(
    view_id: str,
    source_id: str,
    columns: list[str],
    where: sql.Composed,
    source_cols: dict[str, str],
    source_fields: dict[str, tuple[str, str | None, str]],
    join_clause: sql.Composed | None = None,
    joined_names: list[str] | None = None,
) -> sql.Composed:
    """``CREATE VIEW assets.<view_id> AS SELECT <cols>[, j.<joined>] FROM assets.<source_id> AS t
    [LEFT JOIN LATERAL … AS j ON TRUE] WHERE <where>``. Every identifier is ``sql.Identifier``-
    wrapped (view name server-generated; columns proven real OR proven ACTIVE field keys — the
    V044 key grammar caps them at identifier length and the reserved-key CHECK keeps them from
    shadowing a real column); a props field's SELECT item comes only from ``_source_out_expr``
    (extraction + declared-type cast, key as ``sql.Literal``); the predicate is the
    validated/literal-only WHERE; the optional join clause comes ONLY from ``_compile_join``
    (validated + literal-baked). No raw user text."""
    parts: list[sql.Composable] = [_source_out_expr(c, source_cols, source_fields) for c in columns]
    parts += [sql.Identifier("j", n) for n in (joined_names or [])]
    return sql.SQL("CREATE VIEW {vw} AS SELECT {sel} FROM {src} AS t {join} WHERE {where}").format(
        vw=sql.Identifier("assets", view_id),
        sel=sql.SQL(", ").join(parts),
        src=sql.Identifier("assets", source_id),
        join=join_clause if join_clause is not None else sql.SQL(""),
        where=where,
    )


def _drop_view_sql(view_id: str) -> sql.Composed:
    """``DROP VIEW IF EXISTS assets.<view_id>`` — the only drop verb, scoped to the assets schema."""
    return sql.SQL("DROP VIEW IF EXISTS {vw}").format(vw=sql.Identifier("assets", view_id))


# --- audit context (the V020 registry trigger reads these GUCs) -----------------


async def _set_audit_context(cur: Any, actor: UUID) -> None:
    """Set the transaction-local GUCs the platform.layers audit trigger (V020) stamps onto the row, so
    a view's create/edit/delete lands in audit.layer_registry_audit attributed to the actor."""
    await cur.execute("SELECT set_config('app.current_user_id', %s, true)", (str(actor),))
    await cur.execute("SELECT set_config('app.current_client', %s, true)", (_AUDIT_CLIENT,))


# --- introspection for the authoring picker + the lifecycle guard ---------------


async def list_source_columns(conn: AsyncConnection, source_id: str) -> list[dict[str, Any]]:
    """The source's real columns + ACTIVE owner-defined fields for the authoring pickers. ``geom``
    is flagged (auto-included + required for map render); ``id`` is flagged (auto-included). Fields
    ride behind the columns with ``field: true`` + the owner's label (+ ``customDomain`` for coded),
    typed via the same pg-style map ``/columns`` uses so the client's controls apply unchanged. The
    raw ``props`` container is OMITTED — not exposable (decision-view-props-fields.md fork C), so
    the picker never offers what the resolver would refuse."""
    cols = await _columns(conn, source_id)
    out: list[dict[str, Any]] = [
        {"name": name, "type": dtype, "auto": name in _ALWAYS_COLUMNS, "geometry": name == "geom"}
        for name, dtype in cols.items()
        if name != "props"
    ]
    for key, (dtype, domain_id, label) in (await _props_fields(conn, source_id)).items():
        entry: dict[str, Any] = {
            "name": key, "type": _PROPS_TYPE_NAME.get(dtype, "text"),
            "auto": False, "geometry": False, "field": True, "label": label,
        }
        if domain_id:
            entry["customDomain"] = domain_id
        out.append(entry)
    return out


async def dependent_views_of_field(
    conn: AsyncConnection, layer_id: str, field_key: str
) -> list[str]:
    """Display labels ("Name (vw_id)") of the view layers whose STORED DEF references
    ``field_key`` of ``layer_id`` — exposed (``allowed_columns``), filter-baked (``filter_def``),
    or join-bound (``join_spec``, either side: this layer as the view's source OR as its joined
    table). The field-lifecycle guard consults this before deactivate/hard-delete
    (decision-view-props-fields.md fork D): a baked view keeps serving a retired key from its DDL
    while the next recompile 422s on it — the quiet divergence class the guard exists to refuse."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT v.view_layer_id, l.display_name "
            "FROM platform.layer_views v "
            "JOIN platform.layers l ON l.layer_id = v.view_layer_id "
            "WHERE (v.source_layer_id = %(src)s AND ("
            "         %(key)s = ANY(v.allowed_columns)"
            "         OR EXISTS (SELECT 1 FROM jsonb_array_elements(v.filter_def) f"
            "                    WHERE f ->> 'field' = %(key)s)"
            "         OR v.join_spec ->> 'layer_key' = %(key)s))"
            "   OR (v.join_table_id = %(src)s AND ("
            "         v.join_spec ->> 'table_key' = %(key)s"
            "         OR v.join_spec ->> 'sort_key' = %(key)s"
            "         OR v.join_spec -> 'columns' ? %(key)s)) "
            "ORDER BY l.display_name, v.view_layer_id",
            {"src": layer_id, "key": field_key},
        )
        rows = await cur.fetchall()
    return [f"{name} ({view_id})" if name else view_id for (view_id, name) in rows]


async def count_dependent_views(conn: AsyncConnection, source_id: str) -> int:
    """How many view layers depend on ``source_id`` — as their SOURCE or (V080) as their JOINED
    table. The F-lifecycle guard release_layer consults this before any delete; the two FKs
    (RESTRICT) are the raw-error belt behind this friendly count."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM platform.layer_views "
            "WHERE source_layer_id = %s OR join_table_id = %s",
            (source_id, source_id),
        )
        return (await cur.fetchone())[0]


# --- register / edit / drop -----------------------------------------------------


async def register_view(
    conn: AsyncConnection,
    *,
    actor: UUID,
    source_id: str,
    display_name: str,
    display_color: str | None,
    allowed_columns: list[str],
    filters: list[dict[str, Any]],
    join_table_id: str | None = None,
    join_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a view layer over ``source_id``: build + run the scoped CREATE VIEW, register it in
    ``platform.layers`` + ``platform.layer_views``, and grant the author OpenFGA ``owner`` on
    ``layer:<view_id>``. With ``join_table_id`` + ``join_spec`` (both or neither — V080), the view
    additionally LEFT-JOINs one standalone table 1:first (the router enforces the Fork-6
    owner-of-both gate BEFORE calling here).

    Validation + compilation run BEFORE any write (fail-closed: a bad column/op/filter raises
    ``ValueError`` and nothing is created; an unknown source raises 404 via ``_require_registered``).
    The OpenFGA owner tuple is written FIRST — the access authority, mirroring ``create_group``: a view
    whose ownership can't be established must fail closed (503), never exist ownerless. The DB writes
    (registry INSERTs + the CREATE VIEW DDL) then ride ONE transaction (transactional DDL) so a
    half-created view can't persist. The ``vw_<hex>`` id is freshly generated + proven unused, so the
    only residue of a post-tuple DB failure is a harmless dead owner tuple on a nonexistent object,
    which we best-effort compensate. Returns the new view layer's summary."""
    # 1) Validate against the LIVE source + compile the def (no writes yet).
    await _require_registered(conn, source_id)  # 404 if the source isn't a registered layer
    source_cols = await _columns(conn, source_id)
    if not source_cols:
        raise ValueError(f"source layer {source_id!r} has no backing table")
    source_fields = await _props_fields(conn, source_id)
    columns = _resolve_view_columns(allowed_columns, source_cols, source_fields)
    where = _compile_view_where(filters, source_cols, source_fields)
    join_clause, joined_names = await _resolve_join(
        conn, source_id, source_cols, source_fields, join_table_id, join_spec
    )
    collide = sorted(set(joined_names) & set(columns))
    if collide:  # a source column literally named like a prefixed join output — 422, not a DDL 500
        raise ValueError(f"joined column name(s) collide with source columns: {collide} — change the alias")

    view_id = _new_view_id()
    await _require_unused(conn, view_id)

    # 2) OpenFGA owner FIRST — the access authority (fails closed 503 if unwritable).
    owner_tuple = (f"user:{actor}", "owner", f"layer:{view_id}")
    await _openfga_write([owner_tuple])

    # 3) DB writes in ONE transaction: registry row + def row + the CREATE VIEW DDL.
    try:
        async with conn.cursor() as cur:
            await _set_audit_context(cur, actor)
            await cur.execute(
                "INSERT INTO platform.layers "
                "(layer_id, editable, status, display_name, display_color) "
                "VALUES (%s, false, 'ready', %s, %s)",
                (view_id, display_name, display_color),
            )
            await cur.execute(
                "INSERT INTO platform.layer_views "
                "(view_layer_id, source_layer_id, allowed_columns, filter_def, created_by, "
                " join_table_id, join_spec) "
                "VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)",
                (
                    view_id, source_id, columns, json.dumps(filters), actor,
                    join_table_id, json.dumps(join_spec) if join_spec is not None else None,
                ),
            )
            await cur.execute(
                _create_view_sql(
                    view_id, source_id, columns, where,
                    source_cols, source_fields, join_clause, joined_names,
                )
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        try:  # compensate the owner tuple so a failed create leaves no dead grant
            await _openfga_delete([owner_tuple])
        except Exception:
            log.warning("view_register_tuple_orphan", view_id=view_id, actor=str(actor))
        raise

    log.info(
        "view_registered", view_id=view_id, source_id=source_id, actor=str(actor),
        columns=len(columns), filters=len(filters),
    )
    return {
        "layer_id": view_id,
        "source_layer_id": source_id,
        "display_name": display_name,
        "display_color": display_color,
        "allowed_columns": columns,
        "filters": filters,
        "join_table_id": join_table_id,
        "join_spec": join_spec,
        "joined_columns": joined_names,
        "renderMode": "features",
        "status": "ready",
    }


async def update_view(
    conn: AsyncConnection,
    *,
    actor: UUID,
    view_id: str,
    allowed_columns: list[str],
    filters: list[dict[str, Any]],
    join_table_id: str | None = None,
    join_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit a view's columns/filter/join = DROP + re-CREATE the PG view + UPDATE the stored def,
    ONE transaction. Sharing + ownership are unchanged (no OpenFGA change). The source is read from
    the immutable stored def (a view never changes its source). ``join_table_id``/``join_spec`` are
    the FULL new join state (None/None = plain view) — the ROUTER resolves keep/replace/clear
    against the stored def and re-enforces the Fork-6 gate before calling here. Raises
    ``ValueError`` if ``view_id`` isn't a view layer or anything fails validation."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT source_layer_id FROM platform.layer_views WHERE view_layer_id = %s", (view_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise ValueError(f"{view_id!r} is not a view layer")
    source_id = row[0]
    source_cols = await _columns(conn, source_id)
    source_fields = await _props_fields(conn, source_id)
    columns = _resolve_view_columns(allowed_columns, source_cols, source_fields)
    where = _compile_view_where(filters, source_cols, source_fields)
    join_clause, joined_names = await _resolve_join(
        conn, source_id, source_cols, source_fields, join_table_id, join_spec
    )
    collide = sorted(set(joined_names) & set(columns))
    if collide:  # a source column literally named like a prefixed join output — 422, not a DDL 500
        raise ValueError(f"joined column name(s) collide with source columns: {collide} — change the alias")

    try:
        async with conn.cursor() as cur:
            await _set_audit_context(cur, actor)
            await cur.execute(_drop_view_sql(view_id))
            await cur.execute(
                _create_view_sql(
                    view_id, source_id, columns, where,
                    source_cols, source_fields, join_clause, joined_names,
                )
            )
            await cur.execute(
                "UPDATE platform.layer_views "
                "SET allowed_columns = %s, filter_def = %s::jsonb, "
                "    join_table_id = %s, join_spec = %s::jsonb, updated_at = now() "
                "WHERE view_layer_id = %s",
                (
                    columns, json.dumps(filters),
                    join_table_id, json.dumps(join_spec) if join_spec is not None else None,
                    view_id,
                ),
            )
            await cur.execute(
                "UPDATE platform.layers SET updated_at = now() WHERE layer_id = %s", (view_id,)
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise

    log.info(
        "view_updated", view_id=view_id, source_id=source_id, actor=str(actor),
        columns=len(columns), filters=len(filters),
    )
    return {
        "layer_id": view_id,
        "source_layer_id": source_id,
        "allowed_columns": columns,
        "filters": filters,
        "join_table_id": join_table_id,
        "join_spec": join_spec,
        "joined_columns": joined_names,
        "renderMode": "features",
    }


async def drop_view(conn: AsyncConnection, *, actor: UUID, view_id: str) -> None:
    """Delete a view layer: DROP the PG view + remove its registry/def rows + delete its OpenFGA
    tuples. OpenFGA-FIRST (mirrors ``release_layer``): delete ALL tuples on ``layer:<view_id>`` (owner
    + any org_shared/shared_group), then the DB drop — FGA-first fails safe (a crash after tuple delete
    leaves a tuple-less row an admin can finish; Postgres-first could leave a stale grant). The
    ``platform.layer_views`` def row removes via the ``view_layer_id`` ON DELETE CASCADE on the
    ``platform.layers`` delete. Raises ``ValueError`` if ``view_id`` isn't a view layer."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.layer_views WHERE view_layer_id = %s", (view_id,))
        if await cur.fetchone() is None:
            raise ValueError(f"{view_id!r} is not a view layer")

    tuples = await _openfga_read_object_tuples(f"layer:{view_id}")
    await _openfga_delete(tuples)

    async with conn.cursor() as cur:
        await _set_audit_context(cur, actor)
        await cur.execute(_drop_view_sql(view_id))
        # Clear catalog metadata (mirrors release_slot); the registry DELETE cascades layer_views.
        await cur.execute(
            "DELETE FROM platform.item_metadata WHERE item_type = 'layer' AND item_id = %s", (view_id,)
        )
        await cur.execute("DELETE FROM platform.layers WHERE layer_id = %s", (view_id,))
    await conn.commit()

    log.info("view_dropped", view_id=view_id, actor=str(actor), tuples_deleted=len(tuples))
