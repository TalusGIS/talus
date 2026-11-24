"""A layer's visible zoom range — the two places it is stored (decision-layer-zoom-range.md §3, §4.1):
the layer's DEFAULT in its renderer, and a map's OWN pair on an operationalLayers entry. Pure units,
mutation-check style: remove a predicate and a named test goes red.

  * drop the ge/le bounds on either key                -> test_*_out_of_bounds_rejected
  * drop the lowest-below-highest check                 -> test_*_min_not_below_max_rejected
  * drop the keys from Renderer._style()                -> test_renderer_range_round_trips_on_both_shapes
  * store absent keys as null in the renderer           -> test_renderer_absent_range_stays_absent
  * make the map pair required / reject a pre-range map -> test_pre_range_map_entry_still_validates

There is no serving path to test: the range is cartography the browser applies (§11), so the live
checks are the browser legs of §13 step 5, not an invariant.
"""
import pytest
from pydantic import ValidationError

from app.routers.symbology import Renderer
from app.routers.web_maps import OperationalLayer

SIMPLE = {"type": "simple", "symbol": {"color": "#336699"}}
UNIQUE = {
    "type": "uniqueValue",
    "field": "zoning",
    "stops": [{"value": "R1", "color": "#ffcc00"}],
}
LAYER = {"kind": "layer", "layerId": "parcels"}


# --- the layer default (renderer) -----------------------------------------------------------


@pytest.mark.parametrize("base", [SIMPLE, UNIQUE], ids=["simple", "uniqueValue"])
def test_renderer_range_round_trips_on_both_shapes(base):
    stored = Renderer(**base, visibleMinZoom=14, visibleMaxZoom=20).to_storage()
    assert stored["visibleMinZoom"] == 14
    assert stored["visibleMaxZoom"] == 20
    # What is stored validates again unchanged (a later PUT of the same renderer).
    assert Renderer(**stored).to_storage() == stored


@pytest.mark.parametrize("base", [SIMPLE, UNIQUE], ids=["simple", "uniqueValue"])
def test_renderer_absent_range_stays_absent(base):
    stored = Renderer(**base).to_storage()
    assert "visibleMinZoom" not in stored
    assert "visibleMaxZoom" not in stored


def test_renderer_one_end_only():
    assert Renderer(**SIMPLE, visibleMinZoom=14).to_storage()["visibleMinZoom"] == 14
    stored = Renderer(**SIMPLE, visibleMaxZoom=12).to_storage()
    assert stored["visibleMaxZoom"] == 12
    assert "visibleMinZoom" not in stored


@pytest.mark.parametrize("key,value", [
    ("visibleMinZoom", -1), ("visibleMinZoom", 24.5),
    ("visibleMaxZoom", -0.1), ("visibleMaxZoom", 25),
])
def test_renderer_out_of_bounds_rejected(key, value):
    with pytest.raises(ValidationError):
        Renderer(**SIMPLE, **{key: value})


@pytest.mark.parametrize("lo,hi", [(14, 14), (16, 12)])
def test_renderer_min_not_below_max_rejected(lo, hi):
    with pytest.raises(ValidationError, match="visibleMinZoom must be below visibleMaxZoom"):
        Renderer(**SIMPLE, visibleMinZoom=lo, visibleMaxZoom=hi)


def test_renderer_icon_zoom_is_independent_of_the_range():
    # The icon handover zoom (renderer.minZoom) is not a visibility bound: it may sit outside the
    # range, and the range never rewrites it (§4.1 — the map combines them, the store keeps both).
    stored = Renderer(**SIMPLE, minZoom=10, visibleMinZoom=14).to_storage()
    assert stored["minZoom"] == 10
    assert stored["visibleMinZoom"] == 14


# --- a map's own pair (operationalLayers entry) ---------------------------------------------


def test_pre_range_map_entry_still_validates():
    op = OperationalLayer(**LAYER)
    assert op.visibleMinZoom is None
    assert op.visibleMaxZoom is None


@pytest.mark.parametrize("entry", [
    LAYER,
    {"kind": "raster", "rasterId": "dem"},
    {
        "kind": "external", "externalId": "ext_0123abcd", "externalKind": "geojson",
        "url": "https://example.org/parcels.geojson",
    },
], ids=["layer", "raster", "external"])
def test_every_kind_may_carry_a_map_range(entry):
    op = OperationalLayer(**entry, visibleMinZoom=12, visibleMaxZoom=18)
    dumped = op.model_dump()
    assert (dumped["visibleMinZoom"], dumped["visibleMaxZoom"]) == (12, 18)


@pytest.mark.parametrize("key,value", [
    ("visibleMinZoom", -1), ("visibleMinZoom", 24.01),
    ("visibleMaxZoom", -5), ("visibleMaxZoom", 30),
])
def test_map_range_out_of_bounds_rejected(key, value):
    with pytest.raises(ValidationError):
        OperationalLayer(**LAYER, **{key: value})


@pytest.mark.parametrize("lo,hi", [(15, 15), (18, 10)])
def test_map_range_min_not_below_max_rejected(lo, hi):
    with pytest.raises(ValidationError, match="visibleMinZoom must be below visibleMaxZoom"):
        OperationalLayer(**LAYER, visibleMinZoom=lo, visibleMaxZoom=hi)
