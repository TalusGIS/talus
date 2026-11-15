"""Thumbnails are made only from images under a pixel cap, read from the header (once-over 2026-11-15 K3).

`_thumbnail_bytes` decoded whatever image an attachment held, on a door that serves public layers to
anyone. Within the 25 MB attachment limit, an image can decode to hundreds of megabytes, and the
library's own guard refuses only near 179 megapixels. The size is now read from the header first.
Tested with small ordinary images and the cap set low.
"""
import io

import pytest
from PIL import Image, ImageFile

from app.routers import attachments
from app.routers.attachments import _thumbnail_bytes


def _image(fmt: str, size=(300, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (40, 120, 200)).save(buf, format=fmt)
    return buf.getvalue()


@pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
def test_an_ordinary_photo_still_gets_a_thumbnail(fmt):
    out = Image.open(io.BytesIO(_thumbnail_bytes(_image(fmt))))
    assert out.format == "JPEG" and max(out.size) <= 256


@pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
def test_an_image_over_the_cap_is_refused_before_it_is_decoded(fmt, monkeypatch):
    monkeypatch.setattr(attachments, "_THUMB_MAX_SOURCE_PIXELS", 300 * 200 - 1)

    def _no_decode(self, *a, **kw):
        raise AssertionError("the image was decoded before its size was checked")

    monkeypatch.setattr(ImageFile.ImageFile, "load", _no_decode)
    with pytest.raises(ValueError, match="over the thumbnail source limit"):
        _thumbnail_bytes(_image(fmt))


def test_the_cap_is_well_above_a_phone_photo():
    assert attachments._THUMB_MAX_SOURCE_PIXELS >= 8000 * 4000
