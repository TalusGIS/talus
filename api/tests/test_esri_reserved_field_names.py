"""The ESRI door's reserved field names, and the three layers of the fix that keep them from clashing
(decision-esri-field-name-clash.md).

★ The bug this file guards, in full. `_esri_fields` seeds the FeatureServer field list with hardcoded
`OBJECTID` + `GlobalID`, then appends every owner-defined field, with no case-folded dedupe. A layer
whose source ESRI export carried an `objectid` props key was therefore advertised with BOTH `OBJECTID`
and `objectid`. GPKG/SQLite names are case-insensitive, so OGR refuses the duplicate field and
`QgsOfflineEditing` aborts the feature copy — AND STILL RETURNS SUCCESS. The crew gets an empty
GeoPackage, the project stays pointed at the live service, and nobody learns why until a field day is
gone. Seven such layers came out of one bulk ingest, unprompted; eight were found across two stacks.

The failure mode is the reason these tests are cheap and blunt: every leg here is about making a
SILENT wrong answer into a loud one.

Pure logic only — no DB, no HTTP. The live-surface half of the acceptance (no two advertised names
equal under `.lower()`) is in test_geoservices_fields_integration.py, which needs a real layer.
"""
import pytest
from pydantic import ValidationError

from app.routers.geoservices import _alias_case_duplicates
from app.routers.layer_fields import (
    _RESERVED_KEYS,
    _SRC_RENAMED_KEYS,
    FieldCreate,
    _renamed_from,
    _sanitize_key,
)


# ---- Layer 1: prevention at ingest (§3.1) --------------------------------------------------------

@pytest.mark.parametrize("source, expected", [
    ("OBJECTID", "objectid_src"),
    ("objectid", "objectid_src"),
    ("ObjectId", "objectid_src"),
    ("GlobalID", "globalid_src"),
    ("globalid", "globalid_src"),
    ("GLOBALID", "globalid_src"),
])
def test_a_reserved_source_column_lands_as_src(source, expected):
    """The rename is on the KEY the door will advertise, so it must fire on any spelling — the whole
    defect is that gdb names are case-insensitive and ours were not."""
    assert _sanitize_key(source) == expected


@pytest.mark.parametrize("source, expected", [
    ("POLE_TYPE", "pole_type"),
    ("Pole Type", "pole_type"),
    ("shape_length", "shape_length"),   # NOT reserved: the door doesn't synthesize it (§3.3)
    ("shape_area", "shape_area"),
    ("fid", "fid"),
    ("objectid_src", "objectid_src"),   # already renamed — idempotent, no `_src_src`
    ("globalid_2", "globalid_2"),       # only the exact reserved name is renamed
])
def test_ordinary_columns_are_untouched(source, expected):
    """Reserving more than the door synthesizes would refuse legitimate ESRI columns for a
    hypothetical — and real exports carry shape_length/shape_area constantly (§3.3)."""
    assert _sanitize_key(source) == expected


def test_the_rename_is_idempotent():
    """Re-importing the same file must not walk the name outward one `_src` per pass."""
    assert _sanitize_key(_sanitize_key("OBJECTID")) == "objectid_src"


def test_renamed_from_marks_only_the_renamed_columns():
    """The marker is what turns a rename into a VISIBLE preview row instead of an anonymous green
    `ok` (§3.6 Q8) — if it over-reports, every import grows a bogus amber row."""
    assert _renamed_from("OBJECTID") == "OBJECTID"   # carries the SOURCE spelling, for the message
    assert _renamed_from("GlobalID") == "GlobalID"
    assert _renamed_from("Pole Type") is None
    assert _renamed_from("shape_area") is None


# ---- Layer 2: prevention by hand (§3.3) ----------------------------------------------------------

def test_objectid_and_globalid_are_reserved():
    """★ The §2.3 staleness this closes: V064 added a physical `objectid` column to every feature
    table and extended NEITHER this set nor the DB CHECK, so for 23 migrations a hand-authored field
    could shadow a real column — a second, independent way into the same clash that no ingest-time
    fix can reach."""
    assert "objectid" in _RESERVED_KEYS
    assert "globalid" in _RESERVED_KEYS


def test_the_reserved_set_is_exactly_what_the_door_synthesizes():
    """The governing rule (§3.3 Q4): *reserve what the door synthesizes, nothing more.* Speculative
    Esri reservations cost real churn — this asserts the list didn't quietly bloat."""
    for speculative in ("shape", "shape_length", "shape_area", "fid", "oid"):
        assert speculative not in _RESERVED_KEYS, f"{speculative} is not synthesized; reserving it refuses real data"


def test_hand_authoring_a_reserved_key_is_refused():
    """The friendly 422 half of defense-in-depth; V087's CHECK is the half that actually enforces."""
    for key in ("objectid", "globalid"):
        with pytest.raises(ValidationError, match="reserved"):
            FieldCreate(fieldKey=key, label="x", dataType="text")


def test_the_src_rename_set_is_a_subset_of_the_reserved_set():
    """A name renamed at ingest but NOT reserved would let the hand-authoring door reopen the clash;
    a mismatch here is exactly how §2.3 happened the first time."""
    assert _SRC_RENAMED_KEYS <= _RESERVED_KEYS


# ---- Layer 3 (read path): the backstop (§3.4) ----------------------------------------------------

def _f(name):
    return {"name": name, "type": "esriFieldTypeString", "alias": name}


def test_the_backstop_aliases_a_late_case_duplicate_and_never_drops_it():
    """Q5's requirement (1): alias, never drop. A dropped field is the silent failure again, one
    layer down — the client would simply never see the attribute."""
    fields = [_f("OBJECTID"), _f("notes"), _f("objectid")]
    lookup = {"OBJECTID": {"source": "objectid", "kind": "real"},
              "notes": {"source": "notes", "kind": "real"},
              "objectid": {"source": "objectid", "kind": "props"}}

    aliased = _alias_case_duplicates("tal_deadbeef", fields, lookup)

    assert aliased == [("objectid", "objectid_1")]
    assert [f["name"] for f in fields] == ["OBJECTID", "notes", "objectid_1"]
    assert len({f["name"].lower() for f in fields}) == 3, "no two names equal under .lower()"


def test_the_alias_keeps_pointing_at_the_true_storage():
    """★ The objection §3.4 raised against door-level aliasing was that a write would have to reverse
    it through `_split_attrs` forever. It does not: only the ADVERTISED name moves, and `source` still
    names the props key — so the reverse mapping already exists."""
    fields = [_f("OBJECTID"), _f("objectid")]
    lookup = {"OBJECTID": {"source": "objectid", "kind": "real"},
              "objectid": {"source": "objectid", "kind": "props"}}

    _alias_case_duplicates("tal_deadbeef", fields, lookup)

    assert "objectid" not in lookup, "the stale key would shadow the real column in _attributes_for"
    assert lookup["objectid_1"] == {"source": "objectid", "kind": "props"}
    assert lookup["OBJECTID"]["source"] == "objectid"


def test_the_backstop_walks_past_an_occupied_alias():
    """Three-way collision: `_1` is taken, so the next free name is `_2`. Reuses the chute idiom."""
    fields = [_f("OBJECTID"), _f("objectid_1"), _f("objectid")]
    aliased = _alias_case_duplicates("tal_deadbeef", fields, {})
    assert aliased == [("objectid", "objectid_2")]
    assert len({f["name"].lower() for f in fields}) == 3


def test_the_backstop_is_a_no_op_on_a_healthy_layer():
    """It should be unreachable after V087 — so on every real layer it must change nothing at all."""
    fields = [_f("OBJECTID"), _f("GlobalID"), _f("notes"), _f("pole_type")]
    before = [dict(f) for f in fields]
    assert _alias_case_duplicates("tal_deadbeef", fields, {}) == []
    assert fields == before
