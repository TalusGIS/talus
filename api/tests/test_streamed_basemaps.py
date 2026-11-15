"""Streamed basemaps (V115 / decision-streamed-basemap.md §14) — the closed-list rule, as pure
unit tests. The security content of the slice is that a streamed basemap is a NAME: the api never
stores, accepts or returns a tile address. These tests pin that at the three places a caller's
value enters: the offered-list request, the default-basemap request, and a saved map's definition.
The gates themselves (anon 401 / non-admin 403) are proven by request in
scripts/verify-invariants/inv51-streamed-basemap-gate.sh."""
import pytest
from pydantic import ValidationError

from app import basemap
from app.routers.basemap import DefaultBasemapRequest, StreamedBasemapsRequest
from app.routers.web_maps import WebMapDefinition

ADDRESS = "https://tiles.example.org/{z}/{x}/{y}.png"


def test_normalize_orders_and_dedupes():
    assert basemap.normalize_streamed(["imagery", "osm", "imagery"]) == ["osm", "imagery"]
    assert basemap.normalize_streamed([]) == []  # the zero-external setting is a valid value
    assert basemap.normalize_streamed(list(basemap.STREAMED_BASEMAP_VALUES)) == ["osm", "usgstopo", "imagery"]


@pytest.mark.parametrize("bad", [[ADDRESS], ["satellite"], ["light"], [None], [1], "osm", None])
def test_normalize_refuses_anything_but_known_names(bad):
    with pytest.raises(ValueError):
        basemap.normalize_streamed(bad)


def test_streamed_request_is_canonical():
    assert StreamedBasemapsRequest(value=["imagery", "usgstopo"]).value == ["usgstopo", "imagery"]
    assert StreamedBasemapsRequest(value=[]).value == []


@pytest.mark.parametrize("bad", [["imagery", ADDRESS], ["satellite"], ["light"], ["osm"] * 7])
def test_streamed_request_refuses_addresses_and_unknown_names(bad):
    with pytest.raises(ValidationError):
        StreamedBasemapsRequest(value=bad)


def test_default_request_takes_the_eight_names_only():
    for name in ("light", "dark", "grayscale", "imagery", "osm", "usgstopo", "osm-gray", "osm-dark"):
        assert DefaultBasemapRequest(value=name).value == name
    for bad in (ADDRESS, "white", "satellite", "", "osm-sepia", "osm_dark"):
        with pytest.raises(ValidationError):
            DefaultBasemapRequest(value=bad)


def test_the_three_lists_agree():
    # One vocabulary, three consumers: the org default, the offered list, and a saved map.
    assert set(basemap.DEFAULT_BASEMAP_VALUES) == set(basemap.DOWNLOADED_BASEMAP_VALUES) | set(
        basemap.STREAMED_LOOK_VALUES
    )
    # V116: every streamed look belongs to a publisher on the offered list's vocabulary, and each
    # publisher is itself a look (its own standard one) — the offered list stays three entries.
    assert set(basemap.STREAMED_LOOK_PUBLISHER.values()) == set(basemap.STREAMED_BASEMAP_VALUES)
    assert set(basemap.STREAMED_BASEMAP_VALUES) <= set(basemap.STREAMED_LOOK_VALUES)
    # The router's two Literals are the same vocabularies (drift here is the bug class V093 warns of).
    assert set(DefaultBasemapRequest.model_fields["value"].annotation.__args__) == set(
        basemap.DEFAULT_BASEMAP_VALUES
    )
    assert basemap.STREAMED_BASEMAPS_FALLBACK == ("imagery",)  # an upgrade offers what it offered before


def _definition(name: str) -> dict:
    return {
        "basemap": name,
        "operationalLayers": [],
        "initialView": {"center": [-82.5, 27.3], "zoom": 10},
    }


def test_saved_map_accepts_streamed_names_and_refuses_an_address():
    for name in ("osm", "usgstopo", "imagery", "light", "osm-gray", "osm-dark"):
        assert WebMapDefinition(**_definition(name)).basemap == name
    with pytest.raises(ValidationError):
        WebMapDefinition(**_definition(ADDRESS))


# --- V116 / decision-streamed-basemap.md §16: what a portal LISTS ---------------------------


def test_new_looks_are_not_offerable_publishers():
    # A look is not a publisher: the offered list refuses the two new names.
    for look in ("osm-gray", "osm-dark"):
        with pytest.raises(ValidationError):
            StreamedBasemapsRequest(value=[look])
        with pytest.raises(ValueError):
            basemap.normalize_streamed([look])


def test_publisher_of():
    assert basemap.publisher_of("osm") == "osm"
    assert basemap.publisher_of("osm-gray") == "osm"
    assert basemap.publisher_of("osm-dark") == "osm"
    assert basemap.publisher_of("usgstopo") == "usgstopo"
    assert basemap.publisher_of("imagery") == "imagery"
    for local in ("light", "dark", "grayscale", ADDRESS, "white", ""):
        assert basemap.publisher_of(local) is None


@pytest.mark.parametrize(
    "name, listed, streamed, expected",
    [
        ("light", True, [], True),
        ("dark", False, ["osm"], False),  # Streamed: the downloaded looks are not listed
        ("osm", True, [], False),  # Downloaded: nothing streamed is listed
        ("osm-dark", False, ["osm"], True),  # a look is offered when its publisher is
        ("osm-gray", True, ["usgstopo", "imagery"], False),  # ...and not when it is not
        ("imagery", True, ["imagery"], True),
        ("usgstopo", False, ["osm"], False),
        (ADDRESS, True, ["osm", "usgstopo", "imagery"], False),  # never a listed basemap
        ("osm", True, None, False),
    ],
)
def test_is_listed(name, listed, streamed, expected):
    assert basemap.is_listed(name, downloaded_listed=listed, streamed=streamed) is expected


def test_first_listed():
    # Listed downloaded looks: Light, the pre-V116 fall-back, whatever is streamed.
    assert basemap.first_listed(downloaded_listed=True, streamed=["osm"]) == "light"
    assert basemap.first_listed(downloaded_listed=True, streamed=[]) == "light"
    # Streamed only: the first offered publisher in canonical order, not the order sent.
    assert basemap.first_listed(downloaded_listed=False, streamed=["imagery", "usgstopo"]) == "usgstopo"
    assert basemap.first_listed(downloaded_listed=False, streamed=["osm", "imagery"]) == "osm"
    with pytest.raises(basemap.NothingListed):
        basemap.first_listed(downloaded_listed=False, streamed=[])


def _plan(default, listed, values, downloaded):
    return basemap.plan_listing(
        current_default=default, current_listed=listed, values=values, downloaded=downloaded
    )


def test_plan_listing_keeps_a_default_that_is_still_listed():
    assert _plan("dark", True, ["imagery"], None) == (True, "dark")
    assert _plan("osm-dark", True, ["osm"], False) == (False, "osm-dark")
    assert _plan("imagery", True, ["imagery"], None) == (True, "imagery")


def test_plan_listing_moves_a_default_that_stops_being_listed():
    # The V115 behaviour, unchanged: withdraw the streamed default on a portal that lists its
    # downloaded looks and new maps open on Light.
    assert _plan("osm", True, ["imagery"], None) == (True, "light")
    assert _plan("imagery", True, [], None) == (True, "light")
    # Choosing Streamed while new maps open on Light: the first offered publisher.
    assert _plan("light", True, ["usgstopo", "imagery"], False) == (False, "usgstopo")
    # Choosing Downloaded while new maps open on OpenStreetMap Dark: Light.
    assert _plan("osm-dark", False, [], True) == (True, "light")
    # Unticking OpenStreetMap on Streamed while a look of it is the default.
    assert _plan("osm-gray", False, ["imagery"], None) == (False, "imagery")


def test_plan_listing_refuses_to_list_nothing():
    with pytest.raises(basemap.NothingListed):
        _plan("osm", False, [], None)  # already Streamed, now offering nothing
    with pytest.raises(basemap.NothingListed):
        _plan("light", True, [], False)  # Downloaded, asked to stop listing them too


def test_plan_listing_never_returns_an_unlisted_default():
    # Exhaustive over the whole small space: whatever the write, the resulting default is one the
    # resulting settings list — the cross-column CHECK can never be the thing that catches it.
    import itertools

    publishers = basemap.STREAMED_BASEMAP_VALUES
    subsets = [list(c) for n in range(4) for c in itertools.combinations(publishers, n)]
    for default, current, values, downloaded in itertools.product(
        basemap.DEFAULT_BASEMAP_VALUES, (True, False), subsets, (True, False, None)
    ):
        try:
            listed, new_default = _plan(default, current, values, downloaded)
        except basemap.NothingListed:
            assert not values and (downloaded is False or (downloaded is None and not current))
            continue
        assert basemap.is_listed(new_default, downloaded_listed=listed, streamed=values)


@pytest.mark.parametrize("bad", ["no", "false", "true", 0, 1, "yes", []])
def test_downloaded_must_be_a_real_boolean(bad):
    # A lax boolean would read "no" as false. This field decides whether a portal's maps may reach
    # an outside publisher at all, so only a JSON true or false is taken.
    with pytest.raises(ValidationError):
        StreamedBasemapsRequest(value=["imagery"], downloaded=bad)


def test_downloaded_is_optional_and_strict():
    assert StreamedBasemapsRequest(value=["imagery"]).downloaded is None  # the V115 caller
    assert StreamedBasemapsRequest(value=[], downloaded=True).downloaded is True
    assert StreamedBasemapsRequest(value=["osm"], downloaded=False).downloaded is False
