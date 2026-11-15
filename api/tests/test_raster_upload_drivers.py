"""Raster upload opens an uploaded file with ONE named GDAL driver, chosen from the file's signature.

The negative tests for once-over finding A1 (decision-once-over-security-fixes.md). GDAL picks a
driver from a file's CONTENT, so before the fix a VRT document posted under a `.tif` name was opened
as a VRT: its `<SourceFilename>` could name another file on the volume, or a `/vsicurl/http://…`
address the server would then fetch. Every case here is a file the door must refuse WITHOUT reading
what it points at, plus the formats that must keep working.

The fixtures are built so the test can fail: the "secret" raster holds a pixel value (99) that no
accepted upload holds, and the network case counts connections on a real listening socket.
"""
import os
import shutil
import socket
import threading

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app import raster_cog
from app.raster_cog import NotACog, inspect_raster, is_cog, make_thumbnail_png, to_cog

SECRET_VALUE = 99


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
def secret(tmp_path):
    """Another raster on the same volume — the thing a crafted upload must never read."""
    return _write(tmp_path / "secret.tif", "GTiff", SECRET_VALUE)


def _spool(tmp_path, name: str, data: bytes) -> str:
    """A file as the upload door spools it: arbitrary bytes under a `.tif` name."""
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def _assert_refused_everywhere(path: str, tmp_path) -> None:
    """The four GDAL entry points of the upload door all refuse `path`, and none leaks GDAL's own
    error text (which is where a crafted file's contents would travel back)."""
    with pytest.raises(NotACog) as exc:
        inspect_raster(path)
    assert "secret" not in str(exc.value) and "/vsi" not in str(exc.value) and path not in str(exc.value)
    assert is_cog(path) is False
    out = str(tmp_path / "out.cog.tif")
    with pytest.raises(Exception):
        to_cog(path, out)
    assert not os.path.exists(out) or os.path.getsize(out) == 0
    assert make_thumbnail_png(path, [1]) is None


# --- the refusals -------------------------------------------------------------------------------


def test_vrt_under_a_tif_name_never_reads_the_file_it_points_at(tmp_path, secret):
    evil = _spool(tmp_path, "upload.tif", _vrt(secret).encode())
    # The fixture can fail: GDAL, left to choose, opens this as a VRT and returns the secret's pixels.
    with rasterio.open(evil) as unrestricted:
        assert unrestricted.driver == "VRT"
        assert int(unrestricted.read(1)[0, 0]) == SECRET_VALUE
    _assert_refused_everywhere(evil, tmp_path)


def test_vrt_with_a_network_source_makes_no_connection(tmp_path):
    hits: list[bytes] = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def _accept():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            hits.append(b"connected")
            conn.close()

    t = threading.Thread(target=_accept, daemon=True)
    t.start()
    try:
        evil = _spool(tmp_path, "upload.tif", _vrt(f"/vsicurl/http://127.0.0.1:{port}/x.tif").encode())
        _assert_refused_everywhere(evil, tmp_path)
    finally:
        stop.set()
        t.join(timeout=2)
        srv.close()
    assert hits == [], "the upload path made a network connection for an uploaded file"


def test_the_listener_fixture_can_fail(tmp_path):
    """Proves the test above is not vacuous: opened the old way, the same file DOES connect."""
    hits: list[bytes] = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def _accept():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            hits.append(b"connected")
            conn.close()

    t = threading.Thread(target=_accept, daemon=True)
    t.start()
    try:
        evil = _spool(tmp_path, "upload.tif", _vrt(f"/vsicurl/http://127.0.0.1:{port}/x.tif").encode())
        with rasterio.Env(GDAL_HTTP_TIMEOUT="2", GDAL_HTTP_CONNECTTIMEOUT="2", GDAL_HTTP_MAX_RETRY="0"):
            try:
                with rasterio.open(evil) as src:
                    src.read(1)
            except Exception:  # noqa: BLE001 — the read fails; the connection is what is counted
                pass
    finally:
        stop.set()
        t.join(timeout=2)
        srv.close()
    assert hits, "an unrestricted open no longer connects — the negative test above proves nothing"


def test_tiff_signature_followed_by_vrt_text_is_refused(tmp_path, secret):
    poly = _spool(tmp_path, "upload.tif", b"II*\x00" + _vrt(secret).encode())
    _assert_refused_everywhere(poly, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        b"<GDAL_WMS><Service name=\"TMS\"><ServerUrl>http://127.0.0.1:9/${z}/${x}/${y}.png</ServerUrl></Service></GDAL_WMS>",
        b"<GDALTileIndexDataset><IndexDataset>/tmp/x.gpkg</IndexDataset></GDALTileIndexDataset>",
        b'{"type":"FeatureCollection","features":[]}',
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
        b"not a raster at all",
        b"II",  # shorter than any signature
    ],
)
def test_other_content_is_refused_before_gdal_sees_it(tmp_path, body, monkeypatch):
    path = _spool(tmp_path, "upload.tif", body)

    opened: list[tuple] = []

    def _record(*a, **kw):  # the signature check must come first
        opened.append(a)
        raise RuntimeError("opened")

    monkeypatch.setattr(rasterio, "open", _record)
    with pytest.raises(NotACog):
        inspect_raster(path)
    assert is_cog(path) is False
    assert make_thumbnail_png(path, [1]) is None
    assert opened == [], "GDAL was asked to open a file whose signature is not accepted"


def test_a_sibling_file_is_never_attached(tmp_path, secret):
    """A file beside the upload (an overview, a PAM sidecar) is not picked up: the upload is read
    alone. Opened plainly, GDAL attaches the sibling — that is what makes this fixture able to fail."""
    p = _write(tmp_path / "upload.tif", "GTiff", 7)
    shutil.copy(secret, p + ".ovr")
    with rasterio.open(p, driver="GTiff") as plain:
        assert len(plain.files) == 2
    with raster_cog._open_upload(p) as src:
        assert [str(f) for f in src.files] == [p]
    assert inspect_raster(p).width == 64


def test_a_dataset_made_of_more_than_one_file_is_refused(tmp_path, monkeypatch):
    """The one-file rule on its own, whatever driver produced the second file."""
    p = _write(tmp_path / "upload.tif", "GTiff", 7)
    real_open = rasterio.open

    class _TwoFiles:
        driver = "GTiff"
        files = [p, str(tmp_path / "somewhere-else.tif")]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rasterio, "open", lambda *a, **kw: _TwoFiles())
    with pytest.raises(NotACog):
        inspect_raster(p)
    monkeypatch.setattr(rasterio, "open", real_open)
    assert inspect_raster(p).width == 64


# --- a genuine GeoTIFF that names an outside "overview file" in its own metadata ----------------


def _tif_naming_an_overview_file(tmp_path, target: str, ns: str = "OVERVIEWS") -> str:
    """A real GeoTIFF whose GDAL metadata carries OVERVIEW_FILE=<target> in the OVERVIEWS domain —
    written INTO the file (no sidecar travels with an upload). `ns` is the domain's name as written
    into the file: GDAL looks the domain up without regard to case, so every spelling is the same
    domain to it (once-over 2026-11-15 F1)."""
    p = _write(tmp_path / "upload.tif", "GTiff", 7)
    with rasterio.open(p, "r+", driver="GTiff") as ds:
        ds.update_tags(ns=ns, OVERVIEW_FILE=target)
    aux = p + ".aux.xml"
    if os.path.exists(aux):
        os.remove(aux)
    assert b"OVERVIEW_FILE" in open(p, "rb").read(), "the reference did not land inside the file"
    return p


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


def test_overview_reference_to_another_file_is_refused_and_never_read(tmp_path, secret):
    evil = _tif_naming_an_overview_file(tmp_path, secret)
    # The fixture can fail: opened as before this check, GDAL attaches the other file and a
    # downsampled read returns ITS pixels.
    with rasterio.open(evil, driver="GTiff") as plain:
        assert len(plain.files) == 2
        assert int(plain.read(1, out_shape=(1, 8, 8))[0, 0]) == SECRET_VALUE
    _assert_refused_everywhere(evil, tmp_path)


def _targets(port: int) -> dict[str, str]:
    suffix = raster_cog._UPLOAD_GDAL_OPTIONS["CPL_VSIL_CURL_ALLOWED_EXTENSIONS"]
    return {
        "the HTTP driver": f"http://127.0.0.1:{port}/overview.tif",
        "a remote-file path carrying the accepted suffix": f"/vsicurl/http://127.0.0.1:{port}/x{suffix}",
        "an inline WMS description": (
            f'<GDAL_WMS><Service name="TMS"><ServerUrl>http://127.0.0.1:{port}/wms/${{z}}/${{x}}/${{y}}.png'
            "</ServerUrl></Service><DataWindow><UpperLeftX>-180</UpperLeftX><UpperLeftY>90</UpperLeftY>"
            "<LowerRightX>180</LowerRightX><LowerRightY>-90</LowerRightY><TileLevel>1</TileLevel>"
            "<TileCountX>1</TileCountX><TileCountY>1</TileCountY><YOrigin>top</YOrigin></DataWindow>"
            "<Projection>EPSG:4326</Projection><BlockSizeX>256</BlockSizeX><BlockSizeY>256</BlockSizeY>"
            "<BandsCount>1</BandsCount></GDAL_WMS>"
        ),
    }


@pytest.mark.parametrize("kind", ["the HTTP driver", "a remote-file path carrying the accepted suffix",
                                  "an inline WMS description"])
def test_overview_reference_to_an_address_makes_no_connection(tmp_path, kind):
    with _Listener() as listener:
        evil = _tif_naming_an_overview_file(tmp_path, _targets(listener.port)[kind])
        _assert_refused_everywhere(evil, tmp_path)
    assert listener.hits == [], f"{kind}: the upload path made a network connection"


def _tif_naming_an_overview_file_in_bytes(tmp_path, target: bytes, ns: str = "OVERVIEWS") -> str:
    """As above, but the reference is written as raw bytes that are NOT valid UTF-8. rasterio's
    `tags()` drops an item it cannot decode, so a check that reads the tag sees nothing, while GDAL
    still holds the reference and follows it. Found by the security review of the stored-raster fix."""
    marker = "Z" * len(target)
    p = _tif_naming_an_overview_file(tmp_path, marker, ns)
    data = open(p, "rb").read()
    assert data.count(marker.encode()) == 1
    with open(p, "wb") as f:
        f.write(data.replace(marker.encode(), target))
    with rasterio.open(p, driver="GTiff") as plain:
        assert plain.tags(ns="OVERVIEWS") == {}, "the tag decodes after all — this fixture proves nothing"
    return p


def test_overview_reference_that_is_not_valid_text_makes_no_connection(tmp_path):
    with _Listener() as listener:
        evil = _tif_naming_an_overview_file_in_bytes(
            tmp_path, b"http://127.0.0.1:%d/\xff\xfe.tif" % listener.port)
        _assert_refused_everywhere(evil, tmp_path)
    assert listener.hits == [], "the upload path made a network connection for an undecodable reference"


# Once-over 2026-11-15 F1: the domain's name, as written into the file, in another case. The check
# compared it case-sensitively while GDAL does not, so a reference that is not valid text, in a
# lower- or mixed-case domain, passed both halves of the check and the server connected.
_OTHER_CASES = ("overviews", "Overviews", "oVERVIEWs")


@pytest.mark.parametrize("ns", _OTHER_CASES)
def test_overview_reference_in_a_domain_of_any_case_makes_no_connection(tmp_path, ns):
    with _Listener() as listener:
        evil = _tif_naming_an_overview_file_in_bytes(
            tmp_path, b"http://127.0.0.1:%d/\xff\xfe.tif" % listener.port, ns)
        with rasterio.open(evil, driver="GTiff") as plain:
            assert ns in plain.tag_namespaces(), "the domain's case did not survive — this proves nothing"
        _assert_refused_everywhere(evil, tmp_path)
    assert listener.hits == [], f"the upload path made a network connection for a reference in {ns!r}"


@pytest.mark.parametrize("ns", _OTHER_CASES)
def test_the_other_case_fixture_can_fail(tmp_path, ns):
    """GDAL follows the reference whatever the domain's case: opened without the check, asking for
    the file list connects. Without this, the test above could pass because GDAL ignored the domain."""
    with _Listener() as listener:
        evil = _tif_naming_an_overview_file_in_bytes(
            tmp_path, b"http://127.0.0.1:%d/\xff\xfe.tif" % listener.port, ns)
        with rasterio.Env(GDAL_HTTP_TIMEOUT="2", GDAL_HTTP_CONNECTTIMEOUT="2", GDAL_HTTP_MAX_RETRY="0"):
            try:
                with rasterio.open(evil, driver="GTiff") as src:
                    _ = src.files
                    src.read(1, out_shape=(1, 8, 8))
            except Exception:  # noqa: BLE001 — the read may fail; the connection is what is counted
                pass
    assert listener.hits, f"an unchecked open of a {ns!r} domain no longer connects — the test above proves nothing"


def test_the_undecodable_reference_fixture_can_fail(tmp_path):
    """Opened without the check, asking for the file list makes GDAL follow the reference."""
    with _Listener() as listener:
        evil = _tif_naming_an_overview_file_in_bytes(
            tmp_path, b"http://127.0.0.1:%d/\xff\xfe.tif" % listener.port)
        with rasterio.Env(GDAL_HTTP_TIMEOUT="2", GDAL_HTTP_CONNECTTIMEOUT="2", GDAL_HTTP_MAX_RETRY="0"):
            try:
                with rasterio.open(evil, driver="GTiff") as src:
                    _ = src.files
                    src.read(1, out_shape=(1, 8, 8))
            except Exception:  # noqa: BLE001 — the read may fail; the connection is what is counted
                pass
    assert listener.hits, "an unchecked open no longer connects — the negative test above proves nothing"


def test_the_overview_reference_fixture_can_fail(tmp_path):
    """Not vacuous: with the SAME options but without the metadata check, asking for the file list
    connects — this is the path the check closes."""
    with _Listener() as listener:
        evil = _tif_naming_an_overview_file(tmp_path, f"http://127.0.0.1:{listener.port}/overview.tif")
        with rasterio.Env(**raster_cog._UPLOAD_GDAL_OPTIONS):
            with rasterio.open(evil, driver="GTiff") as src:
                try:
                    src.files
                except Exception:  # noqa: BLE001 — the fetch fails; the connection is what is counted
                    pass
    assert listener.hits, "GDAL no longer follows OVERVIEW_FILE — the tests above prove nothing"


def test_the_accepted_remote_suffix_is_random_per_process():
    suffix = raster_cog._UPLOAD_GDAL_OPTIONS["CPL_VSIL_CURL_ALLOWED_EXTENSIONS"]
    assert suffix.startswith(".talus-none-") and len(suffix) >= 12 + 32


def test_an_ordinary_geotiff_with_real_internal_overviews_is_still_accepted(tmp_path):
    """The refusal is about an OUTSIDE overview source, not about having overviews."""
    plain = _write(tmp_path / "plain.tif", "GTiff", 7)
    cog = str(tmp_path / "upload.tif")
    rasterio.shutil.copy(plain, cog, driver="COG", overview_resampling="average", blocksize=16)
    with rasterio.open(cog, driver="GTiff") as src:
        assert src.tags(ns="OVERVIEWS") == {}
    assert inspect_raster(cog).width == 64 and is_cog(cog) is True
    assert make_thumbnail_png(cog, [1]) is not None


# --- what must keep working ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "driver,ext,kw,expected",
    [
        ("GTiff", "tif", {}, "GTiff"),
        ("GTiff", "tif", {"BIGTIFF": "YES"}, "GTiff"),
        ("GTiff", "tif", {"tiled": True, "compress": "DEFLATE"}, "GTiff"),
        ("HFA", "img", {}, "HFA"),
        ("JP2OpenJPEG", "jp2", {"QUALITY": "100", "REVERSIBLE": "YES"}, "JP2OpenJPEG"),  # lossless, so the pixel survives
    ],
)
def test_supported_formats_are_inspected_converted_and_thumbnailed(tmp_path, driver, ext, kw, expected):
    # Written under its natural name (GDAL's JPEG 2000 writer embeds the coordinate system only for a
    # .jp2 name), then spooled ALONE — no sidecar travels with an upload.
    made = tmp_path / "made"
    made.mkdir()
    real = _write(made / f"real.{ext}", driver, 7, **kw)
    spooled = str(tmp_path / "upload.tif")  # the door names every upload .tif, whatever it is
    shutil.copy(real, spooled)
    assert raster_cog.sniff_driver(spooled) == expected
    info = inspect_raster(spooled)
    assert (info.srid, info.width, info.height, info.band_count) == (4326, 64, 64, 1)
    out = str(tmp_path / "out.cog.tif")
    to_cog(spooled, out)
    assert is_cog(out) is True
    with rasterio.open(out, driver="GTiff") as cog:
        assert int(cog.read(1)[0, 0]) == 7
    thumb = make_thumbnail_png(out, [1])
    assert thumb is not None and thumb[:8] == b"\x89PNG\r\n\x1a\n"


def test_an_already_cog_upload_is_recognised(tmp_path):
    plain = _write(tmp_path / "plain.tif", "GTiff", 7)
    cog = str(tmp_path / "upload.tif")
    rasterio.shutil.copy(plain, cog, driver="COG")
    assert is_cog(cog) is True
    assert is_cog(plain) in (True, False)  # a tiny strip TIFF may or may not validate; must not raise


def test_refusal_sentences_are_fixed_text(tmp_path):
    """No GDAL error text in what the caller is told (it goes to the server log instead)."""
    for body in (b"not a raster at all", b"II*\x00" + b"\xff" * 200):
        path = _spool(tmp_path, "upload.tif", body)
        with pytest.raises(NotACog) as exc:
            inspect_raster(path)
        msg = str(exc.value)
        assert path not in msg and "TIFF" not in msg.replace("GeoTIFF", "") and "offset" not in msg
