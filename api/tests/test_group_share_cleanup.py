"""Deleting a group removes its shares on EVERY type that can be shared with a group.

The authorization model lets seven types be shared with a group; `delete_group` cleaned three
(layer, web_map, raster). The shares on field projects, story maps, dashboards and reports stayed in
OpenFGA, pointing at a group id that no longer existed — and a group's id is derived from its NAME.
Create a new group with the old name and its members inherited those shares. (A field project's
archive embeds an access token.)

Two locks: the list of shareable types is checked against the model file itself, so a type added
later cannot be forgotten again; and creating a group first clears any share left pointing at its
id, which closes the inheritance on servers that deleted a group before this fix.
"""
import asyncio
import re
from pathlib import Path

import pytest

from app import authz
from app.routers import groups

MODEL = Path(__file__).resolve().parents[2] / "db" / "openfga" / "model.fga"


def _types_with_shared_group() -> set[str]:
    types: set[str] = set()
    current = None
    for line in MODEL.read_text().splitlines():
        m = re.match(r"^type\s+([a-z_]+)\s*$", line)
        if m:
            current = m.group(1)
        elif current and re.match(r"^\s+define\s+shared_group\s*:", line):
            types.add(current)
    return types


def test_the_model_file_is_where_this_test_thinks_it_is():
    assert MODEL.is_file(), f"{MODEL} is missing — the parity test below would prove nothing"
    assert {"layer", "web_map", "raster"} <= _types_with_shared_group()


def test_every_type_the_model_lets_a_group_be_shared_with_is_cleaned_up():
    assert set(authz.GROUP_SHAREABLE_TYPES) == _types_with_shared_group()
    assert len(authz.GROUP_SHAREABLE_TYPES) == len(set(authz.GROUP_SHAREABLE_TYPES))


@pytest.fixture
def fga(monkeypatch):
    """An in-memory stand-in for the three OpenFGA calls the group doors make."""
    state = {
        "shares": {  # object type -> ids shared with group:field-crew
            "layer": ["tal_00000001"], "field_project": ["fp-1", "fp-2"],
            "dashboard": ["d-1"], "report": ["r-1"], "story_map": ["s-1"],
        },
        "deleted": [], "written": [],
    }

    async def _shared(group_slug, object_type):
        return list(state["shares"].get(object_type, [])) if group_slug == "field-crew" else []

    async def _delete(tuples):
        state["deleted"].extend(tuples)

    async def _write(tuples):
        state["written"].extend(tuples)

    monkeypatch.setattr(authz, "_objects_shared_with_group", _shared)
    monkeypatch.setattr(groups, "_openfga_delete", _delete)
    monkeypatch.setattr(groups, "_openfga_write", _write)
    return state


def test_group_share_tuples_covers_every_type(fga):
    tuples = asyncio.run(authz.group_share_tuples("field-crew"))
    assert sorted(tuples) == sorted([
        ("group:field-crew", "shared_group", "layer:tal_00000001"),
        ("group:field-crew", "shared_group", "field_project:fp-1"),
        ("group:field-crew", "shared_group", "field_project:fp-2"),
        ("group:field-crew", "shared_group", "dashboard:d-1"),
        ("group:field-crew", "shared_group", "report:r-1"),
        ("group:field-crew", "shared_group", "story_map:s-1"),
    ])


def test_clearing_leftover_shares_removes_all_of_them_and_reports_the_count(fga):
    cleared = asyncio.run(groups._clear_leftover_group_shares("field-crew", actor="someone"))
    assert cleared == 6
    assert ("group:field-crew", "shared_group", "field_project:fp-1") in fga["deleted"]
    assert len(fga["deleted"]) == 6


def test_clearing_does_nothing_when_nothing_is_left(fga):
    assert asyncio.run(groups._clear_leftover_group_shares("brand-new", actor="someone")) == 0
    assert fga["deleted"] == []


@pytest.mark.parametrize("system_id", ["home", "open_data"])
def test_clearing_never_touches_a_system_group(fga, monkeypatch, system_id):
    """Home and Open Data hold real shares by design; nothing may clear them by 'creating' one."""
    async def _shared(group_slug, object_type):
        return ["tal_00000009"]

    monkeypatch.setattr(authz, "_objects_shared_with_group", _shared)
    assert asyncio.run(groups._clear_leftover_group_shares(system_id, actor="someone")) == 0
    assert fga["deleted"] == []


def test_both_doors_are_wired():
    import inspect

    assert "group_share_tuples(" in inspect.getsource(groups.delete_group)
    create_src = inspect.getsource(groups.create_group)
    assert "_clear_leftover_group_shares(" in create_src
    # cleared BEFORE the new group's own tuples are written — never a window where both exist
    assert create_src.index("_clear_leftover_group_shares(") < create_src.index("_openfga_write(")
