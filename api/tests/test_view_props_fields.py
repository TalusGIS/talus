"""View layers over owner-defined (props) fields — the widened plain-view compiler
(decision-view-props-fields.md, forks A + C).

`_resolve_view_columns` / `_compile_view_where` / `_source_out_expr` / `_create_view_sql` are
pure builders, so these tests render their output with ``as_string(None)`` and assert the exact
SQL shape — the F-DDL invariants in text form: field keys reach the DDL only as ``sql.Literal``
(json path) + ``sql.Identifier`` (output name), values only as ``sql.Literal``, and nothing
outside the server-derived column ∪ ACTIVE-field-key sets compiles at all.

Also pins the op-parity fix that rode this slice: the pre-slice WHERE compiler handled five ops
and silently baked the other five (`ne`/`notin`/`gt`/`lt`/`like`) as ILIKE — `gt` on a number
became a substring match. Every ``_VALID_OPS`` member now compiles explicitly, and the `ne` test
here goes red if the fallthrough ever returns.

Mutation checks: drop the fork-C `props` refusal, swap the extraction's Literal for raw
interpolation, lose a declared-type cast, or re-widen the else-branch, and a named test goes red.
"""
import pytest
from psycopg import sql

from app.layer_views import (
    _compile_view_where,
    _create_view_sql,
    _resolve_view_columns,
    _source_out_expr,
)

# A Signs-twin-shaped source: real spine columns + owner-defined fields of every declared type.
SOURCE_COLS = {
    "id": "uuid",
    "geom": "USER-DEFINED",
    "objectid": "integer",
    "notes": "text",
    "props": "jsonb",
    "row_version": "integer",
}
SOURCE_FIELDS = {
    "condition_code": ("coded", "3f9f6f0a-0000-0000-0000-000000000001", "Condition"),
    "install_date": ("date", None, "Install date"),
    "spans": ("number", None, "Spans"),
    "owner_name": ("text", None, "Owner"),
}


def _render(composed: sql.Composable) -> str:
    return composed.as_string(None)


# --- _resolve_view_columns -------------------------------------------------------


def test_resolve_admits_field_keys_after_real_columns():
    cols = _resolve_view_columns(
        ["notes", "install_date", "condition_code"], SOURCE_COLS, SOURCE_FIELDS
    )
    # id/geom auto-included; real columns in ordinal order, then field keys in designer order.
    assert cols == ["id", "geom", "notes", "condition_code", "install_date"]


def test_resolve_unknown_name_still_fails_closed():
    with pytest.raises(ValueError, match="unknown column"):
        _resolve_view_columns(["condition_cod"], SOURCE_COLS, SOURCE_FIELDS)


def test_resolve_inactive_or_foreign_key_fails_closed():
    # An INACTIVE field never appears in source_fields (_props_fields filters `AND active`),
    # so membership refusal IS the inactive refusal.
    with pytest.raises(ValueError, match="unknown column"):
        _resolve_view_columns(["retired_field"], SOURCE_COLS, SOURCE_FIELDS)


def test_resolve_refuses_raw_props_container():
    # Fork C: `props` IS a real column, and is refused anyway — an open container would let a
    # field added to the source later flow through a view authored today.
    with pytest.raises(ValueError, match="not exposable"):
        _resolve_view_columns(["notes", "props"], SOURCE_COLS, SOURCE_FIELDS)


# --- _source_out_expr ------------------------------------------------------------


def test_out_expr_real_column_is_bare_identifier():
    assert _render(_source_out_expr("notes", SOURCE_COLS, SOURCE_FIELDS)) == '"t"."notes"'


def test_out_expr_props_field_extracts_names_and_casts():
    # Coded stays text (no cast); the key appears ONLY as a quoted literal + quoted output name.
    assert (
        _render(_source_out_expr("condition_code", SOURCE_COLS, SOURCE_FIELDS))
        == "((\"t\".\"props\" ->> 'condition_code')) AS \"condition_code\""
    )
    assert (
        _render(_source_out_expr("spans", SOURCE_COLS, SOURCE_FIELDS))
        == "((\"t\".\"props\" ->> 'spans')::numeric) AS \"spans\""
    )
    assert (
        _render(_source_out_expr("install_date", SOURCE_COLS, SOURCE_FIELDS))
        == "((\"t\".\"props\" ->> 'install_date')::date) AS \"install_date\""
    )


# --- _compile_view_where ---------------------------------------------------------


def _where(field: str, op: str, value: str) -> str:
    return _render(
        _compile_view_where([{"field": field, "op": op, "value": value}], SOURCE_COLS, SOURCE_FIELDS)
    )


def test_where_props_text_ops_compare_raw_extraction():
    assert _where("condition_code", "eq", "good") == "(t.props ->> 'condition_code') = 'good'"
    assert (
        _where("condition_code", "in", "good,fair")
        == "(t.props ->> 'condition_code') = ANY(ARRAY['good', 'fair']::text[])"
    )
    assert (
        _where("condition_code", "notin", "poor")
        == "NOT ((t.props ->> 'condition_code') = ANY(ARRAY['poor']::text[]))"
    )


def test_where_props_range_ops_cast_by_declared_type():
    assert _where("spans", "gte", "3") == "((t.props ->> 'spans')::numeric) >= '3'"
    assert _where("install_date", "lt", "2020-01-01") == "((t.props ->> 'install_date')::date) < '2020-01-01'"
    # A text field's range op stays on the raw extraction — no cast to lose.
    assert _where("owner_name", "gt", "M") == "((t.props ->> 'owner_name')) > 'M'"


def test_where_real_column_semantics_unchanged():
    assert _where("notes", "eq", "x") == "\"t\".\"notes\"::text = 'x'"
    assert _where("objectid", "gte", "5") == "\"t\".\"objectid\" >= '5'"


def test_where_op_parity_ne_is_not_ilike():
    # THE regression test for the pre-slice fallthrough: ne/gt/lt/notin/like baked as ILIKE.
    assert _where("notes", "ne", "x") == "\"t\".\"notes\"::text <> 'x'"
    assert _where("condition_code", "ne", "good") == "(t.props ->> 'condition_code') <> 'good'"


def test_where_like_is_verbatim_ilike_wraps():
    assert _where("notes", "like", "Main%") == "\"t\".\"notes\"::text LIKE 'Main%'"
    assert _where("notes", "ilike", "main") == "\"t\".\"notes\"::text ILIKE '%main%'"


def test_where_refuses_unknowns_and_props():
    with pytest.raises(ValueError, match="unknown filter field"):
        _where("nope", "eq", "x")
    with pytest.raises(ValueError, match="unknown filter field"):
        _where("props", "ilike", "x")  # fork C: the container is not a filter field either
    with pytest.raises(ValueError, match="unknown filter op"):
        _where("notes", "regex", "x")
    with pytest.raises(ValueError, match="must be a string"):
        _compile_view_where(
            [{"field": "spans", "op": "gte", "value": 3}], SOURCE_COLS, SOURCE_FIELDS
        )


def test_where_value_injection_is_literal_escaped():
    rendered = _where("condition_code", "eq", "g'; DROP VIEW x; --")
    # sql.Literal doubles the quote; the payload stays INSIDE one string literal.
    assert "'g''; DROP VIEW x; --'" in rendered


def test_where_hostile_field_name_never_compiles():
    # A key can only reach the extraction via membership in the server-derived field set —
    # a hostile name fails validation BEFORE any SQL is assembled.
    with pytest.raises(ValueError, match="unknown filter field"):
        _where("x') --", "eq", "v")


# --- _create_view_sql ------------------------------------------------------------


def test_create_view_sql_end_to_end_shape():
    columns = _resolve_view_columns(["notes", "condition_code"], SOURCE_COLS, SOURCE_FIELDS)
    where = _compile_view_where(
        [{"field": "condition_code", "op": "ne", "value": "replace"}], SOURCE_COLS, SOURCE_FIELDS
    )
    ddl = _render(
        _create_view_sql("vw_0123456789ab", "tal_11223344", columns, where, SOURCE_COLS, SOURCE_FIELDS)
    )
    assert ddl == (
        'CREATE VIEW "assets"."vw_0123456789ab" AS SELECT "t"."id", "t"."geom", "t"."notes", '
        "((\"t\".\"props\" ->> 'condition_code')) AS \"condition_code\" "
        'FROM "assets"."tal_11223344" AS t  '
        "WHERE (t.props ->> 'condition_code') <> 'replace'"
    )
