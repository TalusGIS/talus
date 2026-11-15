"""A ranged read of the basemap archive never holds more than one piece in memory.
(Stream, do not clamp.)

`GET /basemap/archive` is anonymous. `Range: bytes=0-` used to become ONE read of the whole object
(the archive may be 4 GiB) into the api's memory, per request. The reply is now streamed in pieces of
`basemap.RANGE_PIECE_BYTES`; what the client receives — status, Content-Range, Content-Length, the
bytes — is exactly what it received before.

The store here is a stub that REFUSES any single read larger than one piece, so the old code path
cannot pass these tests by accident.
"""
import asyncio

import pytest

from app import basemap
from app.routers import basemap as basemap_router

PIECE = basemap.RANGE_PIECE_BYTES
SIZE = PIECE * 3 + 12345
DATA = bytes((i * 31 + (i >> 8)) % 251 for i in range(SIZE))


class _Stat:
    size, etag = SIZE, "etag-1"


class _Store:
    def __init__(self):
        self.reads: list[tuple[int, int]] = []

    def stat(self, key):
        return _Stat()

    def read_range(self, key, offset, length):
        assert length <= PIECE, f"one read of {length} bytes — more than a piece ({PIECE})"
        self.reads.append((offset, length))
        return DATA[offset:offset + length]


class _Request:
    def __init__(self, range_header, method="GET"):
        self.headers = {} if range_header is None else {"range": range_header}
        self.method = method


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(basemap, "get_media_storage", lambda: s)
    return s


def _get(range_header):
    async def _run():
        resp = await basemap_router.basemap_archive(_Request(range_header))
        if hasattr(resp, "body_iterator"):
            body = b"".join([chunk async for chunk in resp.body_iterator])
        else:
            body = resp.body
        return resp, body

    return asyncio.run(_run())


@pytest.mark.parametrize(
    "header,start,end",
    [
        ("bytes=0-", 0, SIZE - 1),                       # the finding: open-ended from zero
        (f"bytes={PIECE - 7}-{PIECE * 2 + 99}", PIECE - 7, PIECE * 2 + 99),   # spans pieces
        (f"bytes=-{PIECE + 5}", SIZE - PIECE - 5, SIZE - 1),                  # suffix
        ("bytes=0-16383", 0, 16383),                     # what the map client really sends
        (f"bytes=100-{SIZE * 5}", 100, SIZE - 1),        # end past the object: clamped, as before
        (f"bytes={SIZE - 1}-", SIZE - 1, SIZE - 1),      # the last byte
    ],
)
def test_ranged_read_returns_exactly_the_range_one_piece_at_a_time(store, header, start, end):
    resp, body = _get(header)
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes {start}-{end}/{SIZE}"
    assert resp.headers["content-length"] == str(end - start + 1)
    assert resp.headers["accept-ranges"] == "bytes" and resp.headers["etag"] == '"etag-1"'
    assert body == DATA[start:end + 1]
    assert store.reads and max(length for _, length in store.reads) <= PIECE


def test_the_stub_store_would_have_caught_the_old_read(store):
    """The fixture can fail: one whole-range read, as the door used to make, trips the stub."""
    with pytest.raises(AssertionError):
        store.read_range("k", 0, SIZE)


def test_unsatisfiable_range_is_still_416(store):
    resp, _ = _get(f"bytes={SIZE}-")
    assert resp.status_code == 416 and resp.headers["content-range"] == f"bytes */{SIZE}"
    assert store.reads == []


def test_store_failure_before_the_first_byte_is_still_503(monkeypatch, store):
    from fastapi import HTTPException

    from app.storage import MediaObjectMissing

    def _gone(key, offset, length):
        raise MediaObjectMissing(key)

    monkeypatch.setattr(store, "read_range", _gone)
    with pytest.raises(HTTPException) as exc:
        _get("bytes=0-")
    assert exc.value.status_code == 503
