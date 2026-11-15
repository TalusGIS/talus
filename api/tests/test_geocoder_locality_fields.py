"""`localityFields` on the geocoder write model (decision-batch-geocoding-county-scale.md F3a, V107).

Pure pydantic, no DB. Pins the shape rules the admin form and the API agree on: locality fields are
optional on an internal locator and normalize to []; a field cannot sit in both lists (it would be
scored AND used as a tiebreak, and the index/label composition would double it); an external locator
carries none. Existence on the layer is `geocode_internal.validate_mapping`'s job (live catalog), not
this model's.
"""
import pytest
from pydantic import ValidationError

from app.geocoders import GeocoderTestRequest, GeocoderWrite

_INTERNAL = {"name": "AddressPt", "type": "internal", "layerId": "tal_de1f8c74", "addressFields": ["address"]}


def test_internal_locality_defaults_to_empty_list() -> None:
    assert GeocoderWrite(**_INTERNAL).localityFields == []
    assert GeocoderWrite(**_INTERNAL, localityFields=None).localityFields == []


def test_internal_locality_kept_in_order_and_stripped() -> None:
    w = GeocoderWrite(**_INTERNAL, localityFields=[" postalcomm", "zip "])
    assert w.localityFields == ["postalcomm", "zip"]


def test_field_cannot_be_both_address_and_locality() -> None:
    with pytest.raises(ValidationError, match="both an address field and a locality field"):
        GeocoderWrite(**_INTERNAL, localityFields=["address"])


def test_locality_must_not_repeat() -> None:
    with pytest.raises(ValidationError, match="must not repeat"):
        GeocoderWrite(**_INTERNAL, localityFields=["zip", "zip"])


def test_external_locator_rejects_locality_fields() -> None:
    with pytest.raises(ValidationError, match="only apply to internal locators"):
        GeocoderWrite(name="OSM", type="nominatim", url="https://nominatim.example", localityFields=["zip"])


def test_external_locator_normalizes_to_empty() -> None:
    assert GeocoderWrite(name="OSM", type="nominatim", url="https://nominatim.example").localityFields == []


def test_test_request_accepts_locality_fields() -> None:
    r = GeocoderTestRequest(type="internal", layerId="tal_de1f8c74", addressFields=["address"],
                            localityFields=["postalcomm", "zip"])
    assert r.localityFields == ["postalcomm", "zip"]
