"""A STORED raster is opened with one named GDAL driver too — never by bare path.

The negative tests for the stored-raster addendum to decision-once-over-security-fixes.md. The
upload door refuses a document that points GDAL at another file or address (test_raster_upload_
drivers.py). But an object can reach the raster store without passing that door: one uploaded
before the rule existed, or an older server's raster folder put back by a restore. The tile, point,
metadata and export paths therefore open through `raster_cog.open_stored`, and every case here is a
stored object those paths must refuse WITHOUT reading what it points at, plus the ordinary COG that
must keep working.

The fixtures are built so the tests can fail: the "secret" raster holds a pixel value (99) that no
stored raster here holds, the network cases count connections on a real listening socket, and each
hostile object is first shown to do its harm when opened the old way.
"""
import asyncio
import inspect
import os
import re
import shutil
import socket
import threading

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app import raster_cog, raster_export, raster_serve, raster_store
from app.raster_cog import StoredRasterRefused, open_stored, to_cog

SECRET_VALUE = 99
KEY = "rasters/00000000-0000-0000-0000-000000000001/data.tif"
STORED_SENTENCE = "this raster's stored file cannot be opened. Upload the raster again."
# A box inside the test raster (lon 0..0.64, lat 0.36..1), in its native EPSG:4326.
INSIDE = (0.1, 0.5, 0.5, 0.9)


def _write(path, driver, value, **kw):
    with rasterio.open(
        path, "w", driver=driver, width=64, height=64, count=1, dtype="uint8",
        crs="EPSG:4326", transform=from_origin(0, 1, 0.01, 0.01), **kw,
    ) as dst:
        dst.write(np.full((1, 64, 64), value, dtype="uint8"))
    return str(path)


def _vrt(source: str) -> str:
    return (
        '<VRTDataset rasterXSize="64" rasterYSize="64"><SRS>EPSG:4326</SRS>'
        "<GeoTransform>0,0.01,0,1,0,-0.01</GeoTransform>"
        '<VRTRasterBand dataType="Byte" band="1"><SimpleSource>'
        f'<SourceFilename relativeToVRT="0">{source}</SourceFilename><SourceBand>1</SourceBand>'
        '<SourceProperties RasterXSize="64" RasterYSize="64" DataType="Byte" BlockXSize="64" BlockYSize="64"/>'
        "</SimpleSource></VRTRasterBand></VRTDataset>"
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The raster store as a folder under tmp_path: `local_path(key)` is that folder plus the key."""
    root = tmp_path / "store"
    (root / os.path.dirname(KEY)).mkdir(parents=True)
    monkeypatch.setattr(raster_store, "local_path", lambda key: str(root / key))
    return root


@pytest.fixture
def secret(tmp_path):
    """Another raster on the same volume — the thing a stored object must never make the api read."""
    return _write(tmp_path / "secret.tif", "GTiff", SECRET_VALUE)


def _put(store, data: bytes) -> str:
    p = store / KEY
    p.write_bytes(data)
    return str(p)


def _put_cog(store, tmp_path, value=7) -> str:
    """An object as the api itself stores one: a COG written by `to_cog`."""
    src = _write(tmp_path / "plain.tif", "GTiff", value)
    dst = str(store / KEY)
    to_cog(src, dst)
    return dst


def _every_path():
    """Each function of the serve and export paths that opens a stored raster, as a call on KEY."""
    spec = next(s for s in raster_export._CANDIDATES if s.key == "geotiff")
    return {
        "render_tile": lambda: raster_serve.render_tile(KEY, 0, 0, 0, [1], None, None),
        "read_point": lambda: raster_serve.read_point(KEY, 0.3, 0.7),
        "native_meta": lambda: raster_serve.native_meta(KEY),
        "tilejson_meta": lambda: raster_serve.tilejson_meta(KEY),
        "read_thumbnail_or_tile_dims": lambda: raster_serve.read_thumbnail_or_tile_dims(KEY),
        "coverage_to_geotiff": lambda: raster_export.coverage_to_geotiff(KEY, INSIDE, 16, 16, 50),
        "clip_to_file": lambda: raster_export.clip_to_file(KEY, None, spec, 50, None),
    }


def _assert_refused_by_every_path(stored_path: str) -> None:
    for name, call in _every_path().items():
        with pytest.raises(StoredRasterRefused) as exc:
            call()
        text = str(exc.value)
        assert text == STORED_SENTENCE, f"{name}: not the fixed sentence: {text!r}"
        assert "secret" not in text and "/vsi" not in text and stored_path not in text


class _Listener:
    def __enter__(self):
        self.hits: list[bytes] = []
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self._srv.settimeout(0.2)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._accept, daemon=True)
        self._t.start()
        return self

    def _accept(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                continue
            self.hits.append(b"connected")
            conn.close()

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(timeout=2)
        self._srv.close()
        return False


# --- the refusals -------------------------------------------------------------------------------


def test_a_stored_vrt_never_reads_the_file_it_points_at(store, secret):
    stored = _put(store, _vrt(secret).encode())
    # The fixture can fail: opened by bare path, as these paths did before, GDAL follows the pointer
    # and hands back the OTHER raster's pixels.
    with rasterio.open(stored) as plain:
        assert plain.driver == "VRT"
        assert int(plain.read(1)[0, 0]) == SECRET_VALUE
    _assert_refused_by_every_path(stored)


def test_a_stored_vrt_with_a_network_source_makes_no_connection(store):
    with _Listener() as listener:
        stored = _put(store, _vrt(f"/vsicurl/http://127.0.0.1:{listener.port}/x.tif").encode())
        _assert_refused_by_every_path(stored)
    assert listener.hits == [], "a serve or export path made a network connection for a stored object"


def test_the_listener_fixture_can_fail(store):
    """Proves the test above is not vacuous: opened by bare path, the same stored object DOES connect."""
    with _Listener() as listener:
        stored = _put(store, _vrt(f"/vsicurl/http://127.0.0.1:{listener.port}/x.tif").encode())
        with rasterio.Env(GDAL_HTTP_TIMEOUT="2", GDAL_HTTP_CONNECTTIMEOUT="2", GDAL_HTTP_MAX_RETRY="0"):
            try:
                with rasterio.open(stored) as src:
                    src.read(1)
            except Exception:  # noqa: BLE001 — the read fails; the connection is what is counted
                pass
    assert listener.hits, "a bare-path open no longer connects — the negative test above proves nothing"


def test_a_tiff_signature_followed_by_vrt_text_is_refused(store, secret):
    stored = _put(store, b"II*\x00" + _vrt(secret).encode())
    _assert_refused_by_every_path(stored)


@pytest.mark.parametrize(
    "body",
    [
        b"<GDAL_WMS><Service name=\"TMS\"><ServerUrl>http://127.0.0.1:9/${z}/${x}/${y}.png</ServerUrl></Service></GDAL_WMS>",
        b"<GDALTileIndexDataset><IndexDataset>/tmp/x.gpkg</IndexDataset></GDALTileIndexDataset>",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
        b"not a raster at all",
        b"",
    ],
)
def test_other_stored_content_is_refused_before_gdal_sees_it(store, body, monkeypatch):
    stored = _put(store, body)
    opened: list[tuple] = []

    def _record(*a, **kw):  # the signature check must come first
        opened.append(a)
        raise RuntimeError("opened")

    monkeypatch.setattr(rasterio, "open", _record)
    _assert_refused_by_every_path(stored)
    assert opened == [], "GDAL was asked to open a stored object whose signature is not a TIFF's"


@pytest.mark.parametrize("driver", ["HFA", "JP2OpenJPEG"])
def test_a_stored_object_in_another_upload_format_is_refused(store, tmp_path, driver):
    """The upload door accepts IMG and JPEG 2000 and CONVERTS them; the store never holds one. A
    stored object in either format did not come from the api, so it is not opened."""
    try:
        src = _write(tmp_path / "other", driver, 7)
    except Exception:  # noqa: BLE001 — this build cannot write the driver; nothing to prove
        pytest.skip(f"{driver} is not writable in this build")
    shutil.copy(src, store / KEY)
    _assert_refused_by_every_path(str(store / KEY))


def test_a_missing_stored_object_is_refused_with_the_fixed_sentence(store):
    _assert_refused_by_every_path(str(store / KEY))


def test_a_stored_geotiff_naming_an_overview_file_is_refused_and_never_read(store, secret):
    # A plain GeoTIFF: GDAL refuses to update a COG's tags in place, and the reference is the point.
    stored = _write(store / KEY, "GTiff", 7)
    with rasterio.open(stored, "r+", driver="GTiff") as ds:
        ds.update_tags(ns="OVERVIEWS", OVERVIEW_FILE=secret)
    aux = stored + ".aux.xml"
    if os.path.exists(aux):
        os.remove(aux)
    assert b"OVERVIEW_FILE" in open(stored, "rb").read(), "the reference did not land inside the file"
    # The fixture can fail: opened as before, GDAL attaches the other file and a downsampled read
    # returns ITS pixels.
    with rasterio.open(stored, driver="GTiff") as plain:
        assert len(plain.files) == 2
        assert int(plain.read(1, out_shape=(1, 8, 8))[0, 0]) == SECRET_VALUE
    _assert_refused_by_every_path(stored)


@pytest.mark.parametrize("ns", ["OVERVIEWS", "overviews", "Overviews", "oVERVIEWs"])
def test_a_stored_geotiff_whose_overview_reference_is_not_valid_text_makes_no_connection(store, ns):
    """The reference written as bytes that are not UTF-8: reading the tag shows nothing, GDAL still
    holds it. The opener asks whether the metadata domain EXISTS, so this is refused unread too —
    whatever case the domain's name is written in, since GDAL ignores it (once-over 2026-11-15 F1)."""
    with _Listener() as listener:
        target = b"http://127.0.0.1:%d/\xff\xfe.tif" % listener.port
        marker = "Z" * len(target)
        stored = _write(store / KEY, "GTiff", 7)
        with rasterio.open(stored, "r+", driver="GTiff") as ds:
            ds.update_tags(ns=ns, OVERVIEW_FILE=marker)
        aux = stored + ".aux.xml"
        if os.path.exists(aux):
            os.remove(aux)
        data = open(stored, "rb").read()
        assert data.count(marker.encode()) == 1
        with open(stored, "wb") as f:
            f.write(data.replace(marker.encode(), target))
        with rasterio.open(stored, driver="GTiff") as plain:
            assert plain.tags(ns="OVERVIEWS") == {}, "the tag decodes after all — this fixture proves nothing"
            assert ns in plain.tag_namespaces(), "the domain's case did not survive — this proves nothing"
        _assert_refused_by_every_path(stored)
    assert listener.hits == [], f"a serve or export path made a network connection for a stored object ({ns!r})"


def test_a_file_beside_a_stored_raster_is_never_attached(store, tmp_path, secret):
    """A sibling `.ovr` in the store's folder is not picked up: the object is read alone. Opened by
    bare path, GDAL attaches it — that is what makes this fixture able to fail."""
    stored = _write(store / KEY, "GTiff", 7)
    shutil.copy(secret, stored + ".ovr")
    with rasterio.open(stored) as plain:
        assert len(plain.files) == 2
    with open_stored(KEY) as src:
        assert [str(f) for f in src.files] == [stored]
        assert int(src.read(1, out_shape=(1, 8, 8))[0, 0]) == 7
    assert raster_serve.read_point(KEY, 0.3, 0.7) == [7]


# --- what must keep working ---------------------------------------------------------------------


def test_an_ordinary_stored_cog_is_served_and_exported(store, tmp_path):
    stored = _put_cog(store, tmp_path, value=7)
    png = raster_serve.render_tile(KEY, 0, 0, 0, [1], None, None)
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"
    assert raster_serve.read_point(KEY, 0.3, 0.7) == [7]
    assert raster_serve.read_point(KEY, 50.0, 50.0) is None  # outside the footprint, as before
    meta = raster_serve.native_meta(KEY)
    assert (meta.epsg, meta.width, meta.height, meta.band_count, meta.dtype) == (4326, 64, 64, 1, "uint8")
    tj = raster_serve.tilejson_meta(KEY)
    assert tj.bounds == pytest.approx([0.0, 0.36, 0.64, 1.0])
    assert raster_serve.read_thumbnail_or_tile_dims(KEY)["bounds"] == pytest.approx([0.0, 0.36, 0.64, 1.0])

    out, w, h = raster_export.coverage_to_geotiff(KEY, INSIDE, 16, 16, 50)
    try:
        with rasterio.open(out, driver="GTiff") as got:
            assert (got.width, got.height) == (w, h) and int(got.read(1)[0, 0]) == 7
    finally:
        os.unlink(out)

    spec = next(s for s in raster_export._CANDIDATES if s.key == "geotiff")
    out, native_w, native_h, downsampled = raster_export.clip_to_file(KEY, None, spec, 50, None)
    try:
        assert (native_w, native_h, downsampled) == (64, 64, False)
        with rasterio.open(out, driver="GTiff") as got:
            assert int(got.read(1)[0, 0]) == 7
    finally:
        os.unlink(out)
    # The opener left nothing open on the stored object.
    os.replace(stored, stored + ".moved")


def test_a_stored_cog_with_real_internal_overviews_is_still_served(store, tmp_path):
    """The overview-reference check must not catch a COG's own overviews, which every stored
    raster of any size has. A zoomed-out tile is drawn from them."""
    big = str(tmp_path / "big.tif")
    with rasterio.open(
        big, "w", driver="GTiff", width=2048, height=2048, count=1, dtype="uint8",
        crs="EPSG:4326", transform=from_origin(0, 1, 0.0005, 0.0005),
    ) as dst:
        dst.write(np.full((1, 2048, 2048), 7, dtype="uint8"))
    to_cog(big, str(store / KEY))
    with open_stored(KEY) as src:
        assert src.overviews(1), "the fixture has no internal overviews — this test proves nothing"
    png = raster_serve.render_tile(KEY, 0, 0, 0, [1], None, None)
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"
    assert raster_serve.read_point(KEY, 0.5, 0.5) == [7]


def test_a_caller_error_inside_the_open_is_not_turned_into_a_refusal(store, tmp_path):
    """Only the open is judged. An export that asks for too much still raises its own error."""
    _put_cog(store, tmp_path)
    with pytest.raises(raster_export.ExportTooLarge):
        raster_export.coverage_to_geotiff(KEY, INSIDE, 100_000, 100_000, 1)


# --- the rule itself ----------------------------------------------------------------------------


def test_no_serve_or_export_path_opens_a_stored_raster_by_bare_path():
    """Both modules open a stored raster only through `open_stored`. A new function that reaches for
    `local_path` or a bare `rasterio.open`/`Reader` on a path would walk around every test above."""
    for module in (raster_serve, raster_export):
        source = inspect.getsource(module)
        assert "local_path" not in source, f"{module.__name__} resolves a store path itself"
        assert "rasterio.Env(" not in source, f"{module.__name__} sets up its own GDAL environment"
        for m in re.finditer(r"rasterio\.open\(", source):
            call = source[m.end():m.end() + 120]
            assert re.match(r'\s*\w+,\s*"w"', call), (
                f"{module.__name__} opens a raster for reading without open_stored: {call[:60]!r}"
            )
        for m in re.finditer(r"\bReader\(", source):
            call = source[m.end():m.end() + 40]
            assert re.match(r"src\.name,\s*dataset=src\)", call), (
                f"{module.__name__} hands rio-tiler a path, not the checked dataset: {call!r}"
            )


def test_the_api_answers_a_refused_stored_raster_with_409_and_the_fixed_sentence():
    from app import main

    resp = asyncio.run(main._stored_raster_refused(None, StoredRasterRefused(raster_cog._MSG_STORED)))
    assert resp.status_code == 409
    assert resp.body == b'{"detail":"' + STORED_SENTENCE.encode() + b'"}'
