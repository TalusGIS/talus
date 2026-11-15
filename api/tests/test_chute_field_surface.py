"""The QGIS write-chute field's advertisement rule (decision-editing-toggle.md §9).

`_chute_ok` decides whether a layer advertises `_talus_attach`, and it feeds THREE surfaces — the
layer document, `query` responses, and the applyEdits path. That reach is why the rule matters: it
determines a layer's advertised FIELD LIST, not just its capabilities.

★ The regression this file guards. `_chute_ok` used to gate on `editing_enabled`, so an admin
flipping Editing off in Manage → Settings silently removed a field from every already-connected
client's schema. Measured 2026-07-25 on tal_4c4d0b1e: 6 fields → 5 → 6, toggle as the only variable.
Connected ArcGIS Pro then threw while serializing against a field that no longer existed —
"The index passed was not within the valid range", and after a re-add "The value type is incompatible
with the field type [_talus_attach]" — before dispatch, so nothing reached the server. It also
disqualified the editing fence as a deny vector and cost two outcome-C nulls.

The line: capabilities MAY depend on `editing_enabled` (it is a property of the layer, and a client
can represent "this layer is now read-only"). The field list may NOT — a client cannot represent
"a field I cached for serialization has ceased to exist".

Pure-logic only: no DB, no HTTP.
"""
from app.routers.geoservices import _chute_ok

SLOT = "tal_4c4d0b1e"      # hosted, generic-write-path
NON_SLOT = "vw_0123456789ab"  # a view layer — the only registered non-slot kind; read-only through this door


def meta(**kw):
    """A layer that SHOULD advertise the chute; each test overrides one thing."""
    base = {"attachments_enabled": True, "editable": True, "editing_enabled": True}
    base.update(kw)
    return base


# ---- ★ the regression guard ---------------------------------------------------------------------

def test_editing_toggle_does_not_change_the_field_surface():
    """THE point of the fix: flipping Editing off must not remove `_talus_attach`.

    Same layer, same structural properties, only the owner's soft on/off switch moved. Advertisement
    must be identical — the deny happens at request time in `_edit_deny_reason`, which checks
    `editing_enabled` independently.
    """
    assert _chute_ok(SLOT, meta(editing_enabled=True)) is True
    assert _chute_ok(SLOT, meta(editing_enabled=False)) is True, (
        "the editing toggle must not mutate the advertised field surface — this is the 6->5->6 "
        "drift that made connected clients throw before dispatch"
    )


def test_toggling_back_and_forth_is_stable():
    """A connected client's cached schema stays valid across any number of flips."""
    surfaces = [_chute_ok(SLOT, meta(editing_enabled=flag))
                for flag in (True, False, True, False, True)]
    assert surfaces == [True] * 5


# ---- the STRUCTURAL gates that DO still apply ---------------------------------------------------
# These are fixed for the life of the layer, so gating on them keeps the surface stable.

def test_attachments_disabled_means_no_chute():
    """The chute exists to carry attachments; with attachments off it would be a lie."""
    assert _chute_ok(SLOT, meta(attachments_enabled=False)) is False


def test_builtin_layer_never_advertises_the_chute():
    """Built-ins write through their own bespoke routers the façade does not emulate."""
    assert _chute_ok(NON_SLOT, meta()) is False


def test_non_editable_layer_never_advertises_the_chute():
    """`editable` is structural — a view layer or read-only slot never gains a write chute."""
    assert _chute_ok(SLOT, meta(editable=False)) is False


def test_builtin_with_attachments_still_no_chute():
    """Both structural gates are required, not either."""
    assert _chute_ok(NON_SLOT, meta(attachments_enabled=True, editable=True)) is False


# ---- absent keys must not crash or silently advertise --------------------------------------------

def test_missing_meta_keys_fail_closed():
    """A partial meta dict must not accidentally advertise the field."""
    assert _chute_ok(SLOT, {}) is False
    assert _chute_ok(SLOT, {"attachments_enabled": True}) is False
    assert _chute_ok(SLOT, {"editable": True}) is False
