"""The layer's public on/off door writes the governance record (once-over 2026-11-15 R7).

The full Share door already wrote `layer_shares_set`; the separate public toggle wrote only a server
log line, so the security overview's "sharing changes on layers … are written to a separate activity
log" was true of one door and not the other. Pure logic: every dependency of the handler is stubbed.
"""
import asyncio

import pytest

from app.models.user import User
from app.routers import layer_shares
from app.routers.layer_shares import LayerPublicShareUpdate, set_layer_public_share

USER = User(
    id="00000000-0000-0000-0000-0000000000a7",
    username="public-share-audit-test",
    email="public-share-audit-test@talusgis.invalid",
    display_name="Public Share Audit Test",
)
LAYER = "tal_4c4d0b1e"


@pytest.fixture
def stubs(monkeypatch):
    rec: dict = {"audit": [], "changed": True}

    async def _registered(conn, layer_id):
        return None

    async def _manage(claims, user, layer_id):
        return True

    async def _may_share(conn, claims):
        return None

    async def _set_public(obj, public):
        rec["set"] = (obj, public)
        return rec["changed"]

    async def _record(conn, **kw):
        rec["audit"].append(kw)

    monkeypatch.setattr(layer_shares, "_require_registered", _registered)
    monkeypatch.setattr(layer_shares, "manage_allowed", _manage)
    monkeypatch.setattr(layer_shares, "assert_may_share_public", _may_share)
    monkeypatch.setattr(layer_shares, "set_object_public", _set_public)
    monkeypatch.setattr(layer_shares.admin_audit, "record", _record)
    return rec


def _call(public):
    return asyncio.run(set_layer_public_share(
        LAYER, LayerPublicShareUpdate(publicAccess=public), conn=None, claims={}, user=USER))


@pytest.mark.parametrize("public", [True, False])
def test_a_change_writes_the_governance_record(stubs, public):
    out = _call(public)
    assert out.publicAccess is public
    assert stubs["audit"] == [{
        "actor_id": USER.id, "action": "layer_public_share_set", "target_type": "layer",
        "target_id": LAYER, "detail": {"public": public},
    }]


def test_no_change_writes_no_record(stubs):
    stubs["changed"] = False
    _call(True)
    assert stubs["audit"] == []
