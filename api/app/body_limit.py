"""A ceiling on every request body.

Until this module nothing bounded a request body: not the edge, not the api. Any door that takes a
JSON document — and several that read a form before they know who is calling — would buffer whatever
arrived. One rule now, in the api, so it is tested with the api and travels with the image whatever
sits in front of it:

  * every request body is limited to `settings.request_body_max_mb` (32 MiB);
  * the doors whose OWN documented cap is larger are listed in `_LARGER_DOORS` with that cap — the
    uploads that stream to disk (raster, import, basemap, field project, the KML table) and the
    file-flow doors that carry a file as base64 or text INSIDE a JSON body (feature import, the
    schema imports, a batch-geocoding table). The door's own check stays the authority and gives the
    friendly message; the ceiling here is only the outer bound, with slack for framing;
  * a body over its ceiling is refused with 413 BEFORE it is read when the client declared its
    length, and cut off as it arrives when it did not.

A door missing from `_LARGER_DOORS` gets the default — its large uploads fail loudly with 413, which
is the safe direction to be wrong in. tests/test_request_body_ceiling.py scans the source for every
route that reads a streamed body and fails if one is neither listed here nor recorded as fitting
under the default.

Pure ASGI (not BaseHTTPMiddleware): it must see the body as it arrives, without buffering it.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable

import structlog
from fastapi import HTTPException, status

from . import basemap
from .config import settings
from .file_ingest import MAX_FILE_BYTES

log = structlog.get_logger(__name__)

_MIB = 1024 * 1024
_FRAMING_SLACK = _MIB          # multipart boundaries, part headers, the JSON around a payload
# layer_fields.py's own cap for an Esri workspace XML (kept in step by tests/test_request_body_ceiling.py;
# not imported: the router module pulls in the whole request stack).
_SCHEMA_XML_MAX_BYTES = 100 * _MIB


def _as_base64(n: int) -> int:
    """Bytes a payload of `n` bytes occupies once base64-encoded inside a JSON string."""
    return (n * 4) // 3 + 4

# The routers answer under /api/v1 and, transitionally, at the root (main.py) — both are matched.
_PREFIX = r"^(?:/api/v1)?"

# (method, path pattern, the door's own cap in bytes — read at request time so a changed setting applies)
_LARGER_DOORS: tuple[tuple[str, re.Pattern[str], Callable[[], int]], ...] = (
    ("POST", re.compile(_PREFIX + r"/rasters/?$"), lambda: settings.raster_max_upload_mb * _MIB),
    ("POST", re.compile(_PREFIX + r"/import-jobs/?$"), lambda: settings.import_max_upload_mb * _MIB),
    ("POST", re.compile(_PREFIX + r"/admin/basemap/upload/?$"), lambda: basemap.UPLOAD_CAP_BYTES),
    ("POST", re.compile(_PREFIX + r"/field-projects/?$"), lambda: settings.field_project_max_bytes),
    ("PUT", re.compile(_PREFIX + r"/field-projects/[^/]+/archive/?$"), lambda: settings.field_project_max_bytes),
    ("POST", re.compile(_PREFIX + r"/geocode/batch/kml/rows/?$"), lambda: MAX_FILE_BYTES),
    # A file carried INSIDE a JSON body, as base64 or as text — the door's cap is on the decoded file.
    ("POST", re.compile(_PREFIX + r"/geocode/batch/run/?$"), lambda: MAX_FILE_BYTES),
    ("POST", re.compile(_PREFIX + r"/layers/[^/]+/features/import/?$"), lambda: _as_base64(MAX_FILE_BYTES)),
    ("POST", re.compile(_PREFIX + r"/custom-fields/preview-import/?$"), lambda: _as_base64(MAX_FILE_BYTES)),
    ("POST", re.compile(_PREFIX + r"/layers/[^/]+/custom-fields/import-geopackage/?$"),
     lambda: _as_base64(MAX_FILE_BYTES)),
    ("POST", re.compile(_PREFIX + r"/layers/[^/]+/custom-fields/import-esri-xml/?$"),
     lambda: _as_base64(_SCHEMA_XML_MAX_BYTES)),
)


def default_limit() -> int:
    return settings.request_body_max_mb * _MIB


def limit_for(method: str, path: str) -> int:
    """The body ceiling, in bytes, for this request. Never below the default."""
    base = default_limit()
    for door_method, pattern, cap in _LARGER_DOORS:
        if method == door_method and pattern.match(path):
            return max(base, cap() + _FRAMING_SLACK)
    return base


class RequestBodyTooLarge(HTTPException):
    """Raised from the wrapped `receive` when a body with no declared length passes its ceiling.
    An HTTPException on purpose: FastAPI turns any OTHER exception raised while it reads a body into
    a 400 "error parsing the body"; an HTTPException it re-raises, so the caller gets the honest 413."""

    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="request body too large")


class RequestBodyLimitMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = limit_for(scope.get("method", ""), scope.get("path", ""))

        declared = None
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                declared = value
                break
        if declared is not None and declared.isdigit() and int(declared) > limit:
            log.info("request_body_refused", reason="declared_length_over_ceiling",
                     path=scope.get("path"), declared=int(declared), limit=limit)
            await _send_413(send)       # the body is never read
            return

        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    log.info("request_body_refused", reason="body_over_ceiling",
                             path=scope.get("path"), limit=limit)
                    raise RequestBodyTooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except RequestBodyTooLarge:
            # Reached only when nothing inside turned it into a response (a body read outside
            # FastAPI's own handling). If a response has begun there is nothing honest left to send.
            if started:
                raise
            await _send_413(send)


async def _send_413(send) -> None:
    body = json.dumps({"detail": "request body too large"}).encode()
    await send({
        "type": "http.response.start",
        "status": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close")],
    })
    await send({"type": "http.response.body", "body": body})
