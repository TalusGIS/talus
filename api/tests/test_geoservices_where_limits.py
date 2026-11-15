"""The Esri `where` compiler cannot be made to spend more than a moment on one request.

The negative tests for once-over finding A2 (decision-once-over-security-fixes.md). Before the fix
`_strip_outer_parens` rescanned the whole string once per nested wrapper (4,000 nested parentheses,
7.8 KB: about a second; 1 MB: hours), and several of the compiler's regexes are quadratic in the
length of a whitespace run. The door is anonymous wherever a layer is public, and the api is one
process on one event loop, so one such request stopped every other.

Three locks, each tested on its own: the wrapper stripper is linear; a `where` past the size, depth
or whitespace caps is an honest Esri 400 before any parsing; and at the caps, the worst shapes we
know how to build still compile (or are refused) in well under a second.

Pure logic: no DB, no HTTP. Timings use generous bounds — the defects they guard were seconds to
hours, so a bound of a second or two cannot flake on a slow machine yet cannot pass on the old code.
"""
import random
import time

import pytest

from app.routers.geoservices import (
    _WHERE_MAX_CHARS,
    _WHERE_MAX_DEPTH,
    _WHERE_MAX_WS_RUN,
    _EsriBadRequest,
    _compile_where,
    _scan_grammar,
    _strip_outer_parens,
)

LOOKUP = {
    "OBJECTID": {"source": "objectid", "kind": "real", "esri_type": "esriFieldTypeOID"},
    "status": {"source": "status", "kind": "props", "esri_type": "esriFieldTypeString"},
    "name": {"source": "name", "kind": "props", "esri_type": "esriFieldTypeString"},
}


def _elapsed(fn) -> float:
    t0 = time.perf_counter()
    try:
        fn()
    except _EsriBadRequest:
        pass
    return time.perf_counter() - t0


# --- the stripper: same answers as before, in one pass ------------------------------------------


def _strip_outer_parens_reference(text: str) -> str:
    """The function as it stood before the fix, kept verbatim as the oracle. Quadratic — only ever
    called here on short strings."""
    while text.startswith("("):
        depth, close_at = 0, -1
        for i, ch in _scan_grammar(text):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close_at = i
                    break
        if close_at != len(text) - 1:
            break
        inner = text[1:-1].strip()
        if not inner:
            break
        text = inner
    return text


_CASES = [
    "1=1", "(1=1)", "((1=1))", "( ( 1=1 ) )", "(A) AND (B)", "((A) AND (B))", "(1=1) OR 1=1",
    "()", "(())", "( )", "((  ))", "(", ")", "((a)", "(a))", ")(", "(a)(b)", "((a)(b))",
    "(OBJECTID IN(1,2,3) AND OBJECTID NOT IN(4))", "((OBJECTID IN (1,2)))",
    "(status = ')')", "(status = '(')", "(status = 'it''s (fine)')", "('(' = status)",
    "(status = 'a') ", " (status = 'a')", "(\tstatus = 'a'\n)", "((status = 'x') )",
    "'(' ", "('unterminated)", "(a) -- (b)", "((a) AND ((b)))", "(((a)) AND b)",
]


@pytest.mark.parametrize("text", _CASES)
def test_stripper_matches_the_previous_function_on_known_cases(text):
    stripped = text.strip()  # the compiler always strips before calling
    assert _strip_outer_parens(stripped) == _strip_outer_parens_reference(stripped)


def test_stripper_matches_the_previous_function_on_random_input():
    rng = random.Random(20261114)
    alphabet = "(()))  '' a=1\t"
    for _ in range(4000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))).strip()
        assert _strip_outer_parens(text) == _strip_outer_parens_reference(text), repr(text)


def test_stripper_is_linear_in_nesting_depth():
    """50,000 nested wrappers: about 4 minutes on the old code (it quadruples per doubling from
    1.08 s at 4,000). Called directly, so the depth cap is not what is being measured."""
    n = 50_000
    text = "(" * n + "1=1" + ")" * n
    t0 = time.perf_counter()
    assert _strip_outer_parens(text) == "1=1"
    assert time.perf_counter() - t0 < 2.0


# --- the caps -----------------------------------------------------------------------------------


def test_deep_nesting_is_refused_fast():
    where = "(" * 100_000 + "1=1" + ")" * 100_000
    t0 = time.perf_counter()
    with pytest.raises(_EsriBadRequest):
        _compile_where(where, LOOKUP)
    assert time.perf_counter() - t0 < 0.5


def test_a_megabyte_of_where_is_refused_fast():
    where = "OBJECTID IN (" + ",".join(str(i) for i in range(160_000)) + ")"
    assert len(where) > 1_000_000
    t0 = time.perf_counter()
    with pytest.raises(_EsriBadRequest) as exc:
        _compile_where(where, LOOKUP)
    assert time.perf_counter() - t0 < 0.5
    assert "too long" in exc.value.message


def test_depth_cap_is_exact():
    ok = "(" * _WHERE_MAX_DEPTH + "OBJECTID = 1" + ")" * _WHERE_MAX_DEPTH
    assert _compile_where(ok, LOOKUP) == ["objectid:eq:1"]
    with pytest.raises(_EsriBadRequest):
        _compile_where("(" + ok + ")", LOOKUP)


def test_parentheses_inside_a_literal_do_not_count_toward_depth():
    where = "status = '" + "(" * (_WHERE_MAX_DEPTH * 4) + "'"
    assert _compile_where(where, LOOKUP) == ["status:eq:" + "(" * (_WHERE_MAX_DEPTH * 4)]


def test_whitespace_run_cap_is_exact():
    ok = "OBJECTID" + " " * _WHERE_MAX_WS_RUN + "= 1"
    assert _compile_where(ok, LOOKUP) == ["objectid:eq:1"]
    with pytest.raises(_EsriBadRequest):
        _compile_where("OBJECTID" + " " * (_WHERE_MAX_WS_RUN + 1) + "= 1", LOOKUP)
    with pytest.raises(_EsriBadRequest):  # inside a literal too: the clause regexes read it as well
        _compile_where("status = 'a" + " " * (_WHERE_MAX_WS_RUN + 1) + "b'", LOOKUP)


def test_length_cap_is_exact():
    base = "OBJECTID IN ("
    ids = []
    while len(base + ",".join(ids) + ")") <= _WHERE_MAX_CHARS:
        ids.append("1234567")
    over = base + ",".join(ids) + ")"
    under = base + ",".join(ids[:-1]) + ")"
    assert len(under) <= _WHERE_MAX_CHARS < len(over)
    assert len(_compile_where(under, LOOKUP)) == 1
    with pytest.raises(_EsriBadRequest):
        _compile_where(over, LOOKUP)


# --- what a real client sends still compiles ----------------------------------------------------


def test_arcgis_pro_refresh_at_the_page_ceiling_compiles():
    """Pro's post-edit refresh: every object id of a full page, wrapped, with its dirty set excluded.
    1,000 ids is the service's advertised maxRecordCount."""
    ids = ",".join(str(1_000_000 + i) for i in range(1000))
    where = f"((OBJECTID IN({ids}) AND OBJECTID NOT IN(7,8,9)))"
    assert len(where) < _WHERE_MAX_CHARS
    t0 = time.perf_counter()
    specs = _compile_where(where, LOOKUP)
    assert time.perf_counter() - t0 < 0.5
    assert specs == [f"objectid:in:{ids}", "objectid:notin:7,8,9"]


# --- at the caps, the worst shapes we can build are still quick ---------------------------------


def _pad(unit: str) -> str:
    return (unit * (_WHERE_MAX_CHARS // len(unit) + 1))[:_WHERE_MAX_CHARS]


_WS = " " * 64  # asserted equal to the cap below, so a raised cap cannot quietly weaken these


@pytest.mark.parametrize(
    "label,where",
    [
        ("whitespace runs that never reach AND", "OBJECTID = 1" + _pad(_WS + "x")),
        ("whitespace runs after a field name", _pad("status" + _WS)),
        ("whitespace runs inside one literal", "status = '" + _pad("a" + _WS)[:-12] + "'"),
        ("IS followed by whitespace, never NULL", _pad("IS" + _WS + "x ")),
        ("thousands of AND clauses", _pad("OBJECTID = 1 AND ")[:-5]),
        ("AND with maximal whitespace both sides", _pad("OBJECTID = 1" + _WS + "AND" + _WS)),
        ("one long IN list", "OBJECTID IN (" + _pad("1,")[:-20] + "1)"),
        ("IN list of quoted parens", "status IN (" + _pad("')' ,")[:-20] + "'x')"),
        ("many IN openers", _pad("OBJECTID IN (1) AND ")[:-5]),
        ("unterminated literal", "status = '" + _pad("x ")),
        ("escaped quotes", "status = '" + _pad("''")[:-12] + "'"),
        ("max depth around a long body", "(" * 16 + _pad("OBJECTID = 1 AND ")[:-60] + " OBJECTID = 1" + ")" * 16),
        ("LIKE with whitespace runs", "name LIKE '" + _pad("%" + _WS)[:-14] + "'"),
        ("NOT without IN, repeated", _pad("NOT" + _WS)),
    ],
)
def test_worst_shapes_at_the_caps_are_quick(label, where):
    assert len(_WS) == _WHERE_MAX_WS_RUN
    assert len(where) <= _WHERE_MAX_CHARS + 64
    took = _elapsed(lambda: _compile_where(where, LOOKUP))
    assert took < 1.5, f"{label}: {took:.2f} s for {len(where)} characters"
