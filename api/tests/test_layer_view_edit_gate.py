"""Only the SOURCE layer's owner (or an administrator) may change what a view exposes.

A view republishes some columns and rows of a source layer under the view's own sharing; that is the
point of it ("share a sanitized view, the original stays private"). A view is CREATED only by the
source's owner or an administrator. But the EDIT door also admitted the view's own owner — and after
a transfer of the view, or of the source, the view's owner need not be someone who may see the
source at all. Such an owner could PUT every column and no filter: the whole source, published.

The view's owner keeps what cannot widen the view: delete (and rename, sharing, metadata, which live
on other doors and are untouched).

Pure logic: the gates are called directly with `is_owner` replaced.
"""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from app.models.user import User
from app.routers import layer_views

VIEW, SOURCE = "vw_0123456789ab", "tal_4c4d0b1e"
ADMIN = {"realm_access": {"roles": ["talus-admin"]}}
PUBLISHER = {"realm_access": {"roles": ["talus-publisher"]}}
USER = User(id="00000000-0000-0000-0000-0000000000a5", username="view-gate-test",
            email="view-gate-test@talusgis.invalid", display_name="View Gate Test")


def _owns(monkeypatch, *owned):
    async def _is_owner(user, layer_id, **kw):
        return layer_id in owned

    monkeypatch.setattr(layer_views, "is_owner", _is_owner)


def _run(gate, claims):
    return asyncio.run(gate(claims, USER, VIEW, SOURCE))


def test_view_owner_who_does_not_own_the_source_cannot_change_the_definition(monkeypatch):
    _owns(monkeypatch, VIEW)
    with pytest.raises(HTTPException) as exc:
        _run(layer_views._assert_can_edit_view_definition, PUBLISHER)
    assert exc.value.status_code == 403


def test_that_same_owner_can_still_delete_the_view(monkeypatch):
    """The control: the refusal above is about the definition, not about the view's owner as such."""
    _owns(monkeypatch, VIEW)
    _run(layer_views._assert_can_modify_view, PUBLISHER)


def test_source_owner_can_change_the_definition_and_delete(monkeypatch):
    _owns(monkeypatch, SOURCE)
    _run(layer_views._assert_can_edit_view_definition, PUBLISHER)
    _run(layer_views._assert_can_modify_view, PUBLISHER)


def test_administrator_can_change_the_definition(monkeypatch):
    _owns(monkeypatch)
    _run(layer_views._assert_can_edit_view_definition, ADMIN)


def test_a_stranger_can_do_neither(monkeypatch):
    _owns(monkeypatch)
    for gate in (layer_views._assert_can_edit_view_definition, layer_views._assert_can_modify_view):
        with pytest.raises(HTTPException) as exc:
            _run(gate, PUBLISHER)
        assert exc.value.status_code == 403


def test_the_edit_door_uses_the_definition_gate_and_the_delete_door_does_not():
    """Wiring: a gate nobody calls protects nothing."""
    edit_src = inspect.getsource(layer_views.edit_view)
    assert "_assert_can_edit_view_definition(" in edit_src
    assert "_assert_can_modify_view(" not in edit_src
    assert "_assert_can_modify_view(" in inspect.getsource(layer_views.delete_view)
