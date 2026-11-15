"""The `_capabilities` ↔ `_edit_deny_reason` correspondence, and the capabilities/field-list asymmetry.

`_capabilities` advertises the editing surface only when the slot regex matches AND `editable` AND
`editing_enabled` — which is *exactly* the set `_edit_deny_reason` checks in its first three
branches, before the PAT-scope check. That correspondence is load-bearing and was, until this file,
asserted nowhere.

★ What it costs when it drifts. Because every pre-PAT-scope deny branch ALSO strips `Editing` from
`capabilities`, ArcGIS Pro never dispatches a save for any of them — so those three denial sentences
are UNREACHABLE FROM PRO BY CONSTRUCTION. The editing fence is a client-side no-op. Measured
2026-07-26: `grep -i applyedits` across an eleven-minute fence-closed window returned zero hits. Two
independent rig test designs had already been built against a bar the code cannot produce before
anyone noticed. The only denial Pro can render with a read-only PAT is the read-only-token sentence;
the sole reachable alternative is the final authz branch, which needs a write-scoped PAT whose owner
lacks `can_edit`.

Reorder `_edit_deny_reason` or loosen `_capabilities` and the relationship changes *silently* — no
error, no failing integration test, just a denial sentence that quietly starts or stops being
reachable. Hence: assert the correspondence directly, both halves, per condition.

Pure-logic only: no DB, no HTTP, no Keycloak. `_edit_deny_reason`'s first four branches all return
before `conn` is ever touched (`conn` is first used by `edit_allowed(...)` at the end), so `conn=None`
reaches every branch this file exercises. A DB fixture would buy nothing.
"""
import asyncio

import pytest

from app.config import settings
from app.models.user import User
from app.routers.geoservices import _capabilities, _chute_ok, _edit_deny_reason, _op_deny

SLOT = "tal_4c4d0b1e"        # hosted, generic-write-path
NON_SLOT = "vw_0123456789ab"  # a view layer — the only registered non-slot kind; read-only through this door

# Which BRANCH fired is what this file asserts — not the marketing copy. Matching on a distinctive
# substring keeps the tests alive through wording edits while still telling the four sentences apart.
SAYS_NOT_A_HOSTED_LAYER = "only hosted layers you created"
SAYS_READ_ONLY_LAYER = "This layer is read-only."
SAYS_EDITING_TURNED_OFF = "Editing is turned off on this layer"
SAYS_READ_ONLY_TOKEN = "This access token is read-only."


def meta(**kw):
    """A layer that SHOULD advertise editing; each test overrides exactly one thing."""
    base = {"attachments_enabled": True, "editable": True, "editing_enabled": True,
            # V109: the four capability flags, all on (the column defaults)
            "edit_add": True, "edit_attributes": True, "edit_geometry": True, "edit_delete": True}
    base.update(kw)
    return base


class _Request:
    """The only thing `_edit_deny_reason` reads off the request is `state.pat_scope`."""

    def __init__(self, pat_scope):
        self.state = type("_State", (), {"pat_scope": pat_scope})()


USER = User(
    id="00000000-0000-0000-0000-0000000000ed",
    username="edit-gate-test",
    email="edit-gate-test@talusgis.invalid",
    display_name="Edit Gate Test",
)


def deny_reason(layer_id, layer_meta, *, pat_scope="read"):
    """`_edit_deny_reason` with a read-only PAT by default.

    A read-only PAT is the useful default precisely because branch 4 then returns without I/O: any
    call that gets past the three STRUCTURAL branches lands on the read-only-token sentence, which
    makes "this input reaches branch 4" observable without a database or a live Keycloak.

    `bearer_claims=None` selects the PAT path — the only claims-less authentication.
    """
    return asyncio.run(_edit_deny_reason(
        _Request(pat_scope), None, USER, None, layer_id, layer_meta,
    ))


@pytest.fixture(autouse=True)
def _pat_branch_is_reachable():
    """The PAT branch is guarded by `settings.auth_mode != "stub"`; conftest's autouse fixture
    forces "keycloak". Asserted rather than assumed, so a conftest change fails here loudly instead
    of silently routing every case into `edit_allowed(None, ...)`."""
    assert settings.auth_mode != "stub", "conftest must force a non-stub auth_mode"


# ---- ★ 4.1 the correspondence: each structural condition gates BOTH surfaces -----------------------
# Parametrized as (condition-name, layer_id, meta, the sentence that branch produces). Turning any
# one off must strip `Editing` AND produce that branch's sentence — the two halves that together
# make the denial unreachable from Pro.

STRUCTURAL_CONDITIONS = [
    ("slot_regex", NON_SLOT, meta(), SAYS_NOT_A_HOSTED_LAYER),
    ("editable", SLOT, meta(editable=False), SAYS_READ_ONLY_LAYER),
    ("editing_enabled", SLOT, meta(editing_enabled=False), SAYS_EDITING_TURNED_OFF),
]


@pytest.mark.parametrize(
    "condition,layer_id,layer_meta,sentence",
    STRUCTURAL_CONDITIONS,
    ids=[c[0] for c in STRUCTURAL_CONDITIONS],
)
def test_each_structural_deny_also_strips_editing(condition, layer_id, layer_meta, sentence):
    """Both halves, per condition. The `Editing` half is why the sentence half never reaches a user.

    If this ever fails on the capabilities half only, the fence has become a REAL server-side deny
    vector and the rig's outcome-C nulls become reproducible — that is a behaviour change, not a
    test bug.
    """
    assert "Editing" not in _capabilities(layer_id, layer_meta), (
        f"{condition} denies the edit, so capabilities must not advertise Editing — otherwise Pro "
        f"dispatches a save that can only fail"
    )
    assert sentence in deny_reason(layer_id, layer_meta), (
        f"{condition} must produce its own denial sentence, not fall through to a later branch"
    )


def test_all_three_on_reaches_the_pat_scope_check():
    """The positive control: with every structural condition satisfied, the gate gets PAST all three.

    Observable without I/O because a read-only PAT stops at branch 4. This is the assertion that
    makes the three above meaningful — otherwise a gate that denied everything would pass them all.
    """
    assert "Editing" in _capabilities(SLOT, meta())
    assert SAYS_READ_ONLY_TOKEN in deny_reason(SLOT, meta())


def test_read_only_token_is_the_only_denial_pro_can_render():
    """★ The reachability claim, stated as one executable assertion.

    Every layer state that denies BEFORE the PAT-scope check also strips `Editing`, so Pro never
    dispatches. The one denial that survives to a user is the read-only-token sentence — reached
    only when capabilities DID advertise Editing.
    """
    reachable = [
        (layer_id, layer_meta) for _, layer_id, layer_meta, _ in STRUCTURAL_CONDITIONS
        if "Editing" in _capabilities(layer_id, layer_meta)
    ]
    assert reachable == [], (
        f"these layer states deny but still advertise Editing, so their denial IS reachable from "
        f"Pro and the fence is no longer a client-side no-op: {reachable}"
    )


def test_a_write_scoped_token_passes_the_scope_check():
    """The scope gate reads `read_write` specifically — the sole reachable alternative denial is the
    final authz branch, which needs a write-scoped PAT whose owner lacks can_edit.

    That final branch calls `edit_allowed(conn, ...)`, so with `conn=None` it raises rather than
    returning a sentence. Raising IS the observation: it proves the scope check let this caller
    through, which is the whole claim.
    """
    with pytest.raises(Exception) as exc:
        deny_reason(SLOT, meta(), pat_scope="read_write")
    assert SAYS_READ_ONLY_TOKEN not in str(exc.value)


# ---- 4.2 the asymmetry: capabilities MAY move with the fence; the field list may NOT ---------------

def test_editing_toggle_moves_capabilities_but_not_the_field_surface():
    """The invariant stated in `_chute_ok`'s docstring, as one executable claim.

    A client can represent "this layer is now read-only". It cannot represent "a field I cached for
    serialization has ceased to exist". Same layer, `editing_enabled` as the only variable — this is
    the 6→5→6 drift that made connected ArcGIS Pro throw before dispatch.
    """
    on, off = meta(editing_enabled=True), meta(editing_enabled=False)

    assert _capabilities(SLOT, on) != _capabilities(SLOT, off), (
        "capabilities MAY move with the fence — that is the honest half"
    )
    assert _chute_ok(SLOT, on) == _chute_ok(SLOT, off), (
        "the advertised FIELD LIST may not move with the fence — a client cannot represent a field "
        "that ceased to exist"
    )


def test_structural_conditions_move_both_surfaces_together():
    """The contrast that gives the asymmetry its shape: `editable` is STRUCTURAL — fixed for the
    life of the layer — so it is allowed to gate the field list too. Only the soft toggle is fenced
    off from the field surface."""
    assert "Editing" not in _capabilities(SLOT, meta(editable=False))
    assert _chute_ok(SLOT, meta(editable=False)) is False


# ---- 4.3 V109: the edit tokens come from the owner's four flags ------------------------------------
# decision-layer-edit-capabilities.md §3.4. Pro and QGIS shape their editing tools from this string,
# so each token must track exactly its flag — and `Update` is the one Esri token that covers two.

def tokens(m):
    return _capabilities(SLOT, m).split(",")


def test_all_four_on_is_the_pre_v109_string():
    """Byte-for-byte today's behaviour when nothing is switched off (the acceptance's 'all four on')."""
    assert _capabilities(SLOT, meta()) == "Query,Create,Update,Delete,Editing"


@pytest.mark.parametrize("flag,token", [("edit_add", "Create"), ("edit_delete", "Delete")])
def test_a_single_flag_drops_exactly_its_token(flag, token):
    t = tokens(meta(**{flag: False}))
    assert token not in t
    assert "Editing" in t and "Query" in t, "the other capabilities stay advertised"
    assert _op_deny(meta(**{flag: False}), flag.removeprefix("edit_")) is not None


def test_update_covers_attributes_or_geometry():
    """Esri has one `Update` token; allowGeometryUpdates carries the geometry half. `Update` drops only
    when BOTH are off — an attributes-only or a geometry-only layer still advertises it."""
    assert "Update" in tokens(meta(edit_geometry=False))
    assert "Update" in tokens(meta(edit_attributes=False))
    assert "Update" not in tokens(meta(edit_attributes=False, edit_geometry=False))


def test_all_four_off_is_query_only_with_the_master_on():
    off = meta(edit_add=False, edit_attributes=False, edit_geometry=False, edit_delete=False)
    assert _capabilities(SLOT, off) == "Query"
    assert _chute_ok(SLOT, off) is True, "the field surface still never moves with an edit flag"


def test_op_deny_names_the_op_not_the_data():
    assert _op_deny(meta(), "delete") is None
    assert "deleting" in _op_deny(meta(edit_delete=False), "delete")
    assert "moving or reshaping" in _op_deny(meta(edit_geometry=False), "geometry")
