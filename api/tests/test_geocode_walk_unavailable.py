"""The geocode walks say so when EVERY attempted locator failed (a dead
locator was a silent 200 with no error code). Fail-soft per locator is unchanged:
one locator failing while another answers (even empty) is still a plain result."""
import asyncio

import pytest

from app import geocode
from app.geocode import GeocoderError, LocatorsUnavailable


class _Adapter:
    config = None
    transport = "http"

    def __init__(self, name, *, fail=False, results=None):
        self.name = name
        self._fail = fail
        self._results = results or []

    async def suggest(self, client, q, bbox=None):
        if self._fail:
            raise GeocoderError(f"{self.name} down")
        return list(self._results)

    async def search(self, client, q, magic_key, bbox=None):
        if self._fail:
            raise GeocoderError(f"{self.name} down")
        return list(self._results)


@pytest.fixture
def walk(monkeypatch):
    def use(adapters):
        monkeypatch.setattr(geocode, "_scoped_adapters", lambda configs, locator: list(adapters))
        monkeypatch.setattr(geocode, "_adapter_by_name",
                            lambda configs, name: next((a for a in adapters if a.name == name), None))
    return use


def test_suggest_raises_only_when_every_attempted_locator_failed(walk):
    walk([_Adapter("a", fail=True), _Adapter("b", fail=True)])
    with pytest.raises(LocatorsUnavailable) as exc:
        asyncio.run(geocode.suggest([], "100 Main"))
    assert (exc.value.attempted, exc.value.failed) == (2, 2)


def test_suggest_stays_fail_soft_when_one_locator_answers_empty(walk):
    walk([_Adapter("a", fail=True), _Adapter("b", results=[])])
    assert asyncio.run(geocode.suggest([], "100 Main")) == []


def test_suggest_first_non_empty_still_wins_past_a_failure(walk):
    walk([_Adapter("a", fail=True), _Adapter("b", results=["hit"])])
    assert asyncio.run(geocode.suggest([], "100 Main")) == ["hit"]


def test_no_attemptable_locator_is_a_plain_empty_result(walk):
    walk([])
    assert asyncio.run(geocode.suggest([], "100 Main")) == []
    assert asyncio.run(geocode.search([], "100 Main")) == []


def test_search_counts_both_paths(walk):
    walk([_Adapter("a", fail=True)])
    with pytest.raises(LocatorsUnavailable) as exc:
        asyncio.run(geocode.search([], "100 Main", source="a", magic_key="k"))  # picked path, then the walk
    assert (exc.value.attempted, exc.value.failed) == (2, 2)
    walk([_Adapter("a", fail=True), _Adapter("b", results=["c"])])
    assert asyncio.run(geocode.search([], "100 Main")) == ["c"]
