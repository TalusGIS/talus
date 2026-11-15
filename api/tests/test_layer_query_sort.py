"""Unit tests for the ORDER BY grammar `_order_by` builds (decision-dashboards-polish.md §2).

No DB: `_order_by` returns a `sql.Composed`, so the generated SQL is asserted directly. That is the
point — the NULLS LAST decision is about WHICH BRANCH emits the clause, and a live-data test would
show the row order without proving where the clause landed.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers.layer_query import _order_by

# _props_fields shape: field_key -> (data_type, domain_id, label)
PROPS = {"height": ("number", None, "Height"), "name": ("text", None, "Name")}
REAL = {"id", "objectid", "updated_at", "notes"}


def rendered(sort: str | None, valid=None, props=None) -> str:
    composed, _params = _order_by(sort, valid if valid is not None else REAL, props or {})
    return composed.as_string(None)


# --- the props branch: NULLS LAST, so blanks never lead a descending sort --------------------------

def test_props_desc_puts_nulls_last():
    """The reported bug: 'sort biggest first' led with the empty rows, because Postgres defaults to
    NULLS FIRST for DESC and blanks live in the owner-defined props fields."""
    assert "DESC NULLS LAST" in rendered("height:desc", props=PROPS)


def test_props_asc_also_carries_the_clause():
    """A no-op for ASC (Postgres already defaults to NULLS LAST) but emitted for consistency, so the
    two directions cannot drift apart later."""
    assert "ASC NULLS LAST" in rendered("height:asc", props=PROPS)


def test_props_sort_key_is_a_bound_param_not_an_identifier():
    """The injection posture the NULLS LAST change must not disturb."""
    composed, params = _order_by("height:desc", REAL, PROPS)
    assert params == {"_sortpk": "height"}
    assert "%(_sortpk)s" in composed.as_string(None)
    assert "height" not in composed.as_string(None)  # the name is never inlined


# --- the real-column branch: deliberately UNCHANGED ------------------------------------------------

@pytest.mark.parametrize("field", ["objectid", "updated_at", "id"])
def test_real_columns_do_not_get_nulls_last(field):
    """★ Deliberate. Every real slot column is NOT NULL except notes/ai_provenance, so the clause
    would be a semantic no-op — and it is NOT free: Postgres does not use the NOT NULL constraint to
    prove it redundant, so the explicit clause defeats the btree. Measured on a 327k-row layer,
    `objectid DESC` is an Index Scan Backward at 4.9 ms and `objectid DESC NULLS LAST` is a Parallel
    Seq Scan + top-N heapsort at 80.6 ms. This test is the tripwire against 'tidying' that up."""
    out = rendered(f"{field}:desc")
    assert "DESC" in out and "NULLS" not in out


def test_real_column_is_an_identifier_not_a_param():
    assert '"t"."objectid"' in rendered("objectid:desc")


# --- shared grammar, unchanged by this slice -------------------------------------------------------

def test_stable_tiebreak_is_always_appended():
    assert rendered("objectid:desc").endswith(", t.id")
    assert rendered("height:desc", props=PROPS).endswith(", t.id")


def test_no_sort_orders_by_id():
    assert rendered(None) == "ORDER BY t.id"


def test_direction_defaults_to_asc():
    assert " ASC" in rendered("objectid")


def test_unknown_field_is_422():
    with pytest.raises(HTTPException) as e:
        _order_by("nope:desc", REAL, PROPS)
    assert e.value.status_code == 422


def test_bad_direction_is_422():
    with pytest.raises(HTTPException) as e:
        _order_by("objectid:sideways", REAL, PROPS)
    assert e.value.status_code == 422
