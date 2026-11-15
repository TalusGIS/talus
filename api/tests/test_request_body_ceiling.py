"""Every request body has a ceiling.
(In the api, 32 MiB; upload doors keep their own caps.)

Three things are pinned:

1. the middleware itself — a declared length over the ceiling is refused before a byte is read; an
   undeclared one is cut off as it arrives; neither needs a credential;
2. through FastAPI — the refusal reaches the caller as 413, not as the 400 "error parsing the body"
   FastAPI makes of any other exception raised while it reads a body;
3. the inventory — the source is scanned for every write route that reads a streamed body or carries
   a file inside JSON, and each must either be listed in `body_limit._LARGER_DOORS` with its own cap
   or be recorded below as fitting under the default. A new upload door that is in neither fails
   here, which is the point: the list cannot go stale quietly.
"""
import ast
import asyncio
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from app import body_limit
from app.body_limit import RequestBodyLimitMiddleware, default_limit, limit_for
from app.config import settings
from app.file_ingest import MAX_FILE_BYTES

MIB = 1024 * 1024
API = Path(__file__).resolve().parents[1]
REPO = API.parent


# --- 1. the middleware, driven as raw ASGI -------------------------------------------------------


def _drive(app, *, method="POST", path="/api/v1/layers", headers=(), chunks=()):
    """Run one request through `app`. `chunks` is an iterable of body pieces (may be a generator, so
    a large body is never held in the test's memory). Returns (status, pieces_taken, body)."""
    it = iter(chunks)
    taken = 0
    out = {"status": None, "body": b""}

    async def receive():
        nonlocal taken
        try:
            piece = next(it)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        taken += 1
        return {"type": "http.request", "body": piece, "more_body": True}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    scope = {"type": "http", "method": method, "path": path, "headers": list(headers), "query_string": b""}
    asyncio.run(app(scope, receive, send))
    return out["status"], taken, out["body"]


class _Drain:
    """An inner app that reads the whole body, the way a door that buffers it would."""

    def __init__(self):
        self.called = False
        self.read = 0

    async def __call__(self, scope, receive, send):
        self.called = True
        while True:
            message = await receive()
            self.read += len(message.get("body") or b"")
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _pieces(total_mib):
    piece = b"x" * MIB
    for _ in range(total_mib):
        yield piece


def test_default_is_32_mib():
    assert settings.request_body_max_mb == 32 and default_limit() == 32 * MIB


def test_declared_length_over_the_ceiling_is_refused_before_anything_is_read():
    inner = _Drain()
    status, taken, body = _drive(
        RequestBodyLimitMiddleware(inner),
        headers=[(b"content-length", str(33 * MIB).encode())], chunks=_pieces(33),
    )
    assert status == 413 and b"too large" in body
    assert taken == 0 and inner.called is False, "the body was read, or the door was reached"


def test_undeclared_body_is_cut_off_as_it_arrives():
    inner = _Drain()
    status, taken, _ = _drive(RequestBodyLimitMiddleware(inner), chunks=_pieces(200))
    assert status == 413
    assert taken == 33, f"read {taken} MiB of a 200 MiB body before refusing (ceiling 32)"
    assert inner.read <= 32 * MIB


def test_a_body_at_the_ceiling_passes_untouched():
    inner = _Drain()
    status, taken, body = _drive(
        RequestBodyLimitMiddleware(inner),
        headers=[(b"content-length", str(32 * MIB).encode())], chunks=_pieces(32),
    )
    assert (status, body) == (200, b"ok") and inner.read == 32 * MIB


def test_no_credential_is_needed_to_be_refused():
    """The refusal is the middleware's: it happens with no Authorization header at all."""
    status, taken, _ = _drive(RequestBodyLimitMiddleware(_Drain()),
                              headers=[(b"content-length", b"999999999999")])
    assert status == 413 and taken == 0


def test_a_lying_content_length_is_still_cut_off():
    """Declares 10 bytes, sends 200 MiB: the count of what arrives is what binds."""
    status, taken, _ = _drive(RequestBodyLimitMiddleware(_Drain()),
                              headers=[(b"content-length", b"10")], chunks=_pieces(200))
    assert status == 413 and taken == 33


@pytest.mark.parametrize("path", ["/api/v1/rasters", "/rasters"])
def test_an_upload_door_keeps_its_own_larger_cap(path):
    inner = _Drain()
    status, _, _ = _drive(RequestBodyLimitMiddleware(inner), path=path, chunks=_pieces(40))
    assert status == 200 and inner.read == 40 * MIB          # past the default, under the door's cap
    over = settings.raster_max_upload_mb * MIB + 2 * MIB
    status, taken, _ = _drive(RequestBodyLimitMiddleware(_Drain()), path=path,
                              headers=[(b"content-length", str(over).encode())])
    assert status == 413 and taken == 0                      # and the door's cap is still an outer bound


def test_the_larger_cap_belongs_to_the_door_not_its_neighbours():
    for method, path in [("GET", "/api/v1/rasters"), ("POST", "/api/v1/rasters/abc/export"),
                         ("PUT", "/api/v1/rasters"), ("POST", "/api/v1/rasters/../layers")]:
        assert limit_for(method, path) == default_limit(), (method, path)


def test_non_http_scopes_pass_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(RequestBodyLimitMiddleware(inner)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


# --- 2. through FastAPI: 413, not "error parsing the body" ---------------------------------------


class _Doc(BaseModel):
    text: str


def _fastapi_app():
    app = FastAPI()

    @app.post("/api/v1/thing")
    async def thing(doc: _Doc):
        return {"n": len(doc.text)}

    app.add_middleware(RequestBodyLimitMiddleware)
    return app


def test_fastapi_json_door_answers_413_for_an_undeclared_oversize_body():
    def body():
        yield b'{"text": "'
        yield from _pieces(40)
        yield b'"}'

    status, taken, payload = _drive(_fastapi_app(), path="/api/v1/thing",
                                    headers=[(b"content-type", b"application/json")], chunks=body())
    assert status == 413, f"got {status}: {payload[:120]!r}"
    assert taken <= 34


def test_fastapi_json_door_still_works_under_the_ceiling():
    status, _, payload = _drive(_fastapi_app(), path="/api/v1/thing",
                                headers=[(b"content-type", b"application/json")],
                                chunks=[b'{"text": "hello"}'])
    assert status == 200 and payload == b'{"n":5}'


def test_the_real_app_has_the_middleware():
    from app.main import app

    assert any(m.cls is RequestBodyLimitMiddleware for m in app.user_middleware)


# --- 3. the inventory ----------------------------------------------------------------------------

_STREAMS = re.compile(r"\.stream\(\)|\.form\(\)|\bUploadFile\b")
_CARRIES_A_FILE = re.compile(r"MAX_FILE_BYTES|_MAX_XML_BYTES|geocode_batch_max_bytes|_decode_b64|base64")
_PREFIX_RE = re.compile(r"""APIRouter\(\s*prefix\s*=\s*["']([^"']*)["']""")


def _scan(root: Path):
    """(method, sample request path, where) for every POST/PUT/PATCH route under `root` whose handler —
    or a same-module helper it calls — reads a streamed/form/uploaded body or handles a file payload."""
    found = []
    for py in sorted(root.rglob("*.py")):
        if "tests" in py.parts:
            continue
        src = py.read_text()
        tree = ast.parse(src)
        m = _PREFIX_RE.search(src)
        prefix = m.group(1) if m else ""
        funcs = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.setdefault(node.name, node)

        def touches(fn, seen):
            if fn.name in seen:
                return False
            seen.add(fn.name)
            seg = ast.get_source_segment(src, fn) or ""
            if _STREAMS.search(seg) or _CARRIES_A_FILE.search(seg):
                return True
            return any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in funcs
                and touches(funcs[n.func.id], seen)
                for n in ast.walk(fn)
            )

        for fn in funcs.values():
            for dec in fn.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.args):
                    continue
                verb = dec.func.attr
                if verb in ("post", "put", "patch"):
                    methods = {verb.upper()}
                elif verb == "api_route":
                    methods = set()
                    for kw in dec.keywords:
                        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                            methods = {e.value.upper() for e in kw.value.elts
                                       if isinstance(e, ast.Constant)} & {"POST", "PUT", "PATCH"}
                else:
                    continue
                a = dec.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    path = a.value
                elif isinstance(a, ast.JoinedStr):
                    path = "".join(v.value if isinstance(v, ast.Constant) else "/mount" for v in a.values)
                else:
                    continue
                if methods and touches(fn, set()):
                    sample = "/api/v1" + prefix + re.sub(r"\{[^}]+\}", "x1", path)
                    for method in sorted(methods):
                        found.append((method, sample, f"{py.relative_to(REPO)}::{fn.name}"))
    return found


# Doors that read a streamed body or handle a file payload and FIT UNDER THE DEFAULT, each with the
# cap that makes that true. Anything the scan finds that is neither here nor in _LARGER_DOORS fails.
_FITS_UNDER_DEFAULT = {
    "api/app/routers/attachments.py::upload_attachment": "one attachment, settings.attachment_max_bytes (25 MB)",
    "api/app/routers/geoservices.py::*": "query doors 4 MiB (_QUERY_FORM_MAX_BYTES); edit doors the default; "
                                         "attachments settings.attachment_max_bytes (25 MB)",
}


def _discovered():
    # This file ships in the open-source tree, so it scans the open-source api only. Doors that live
    # in another tree are checked by that tree's own tests, which reuse `_scan` from here.
    return _scan(API / "app")


def test_the_scan_finds_the_doors_we_know_about():
    """The scanner can fail: if it found nothing, the inventory test below would pass on anything."""
    where = {w for _, _, w in _discovered()}
    for known in ("api/app/routers/rasters.py::create_raster", "api/app/routers/import_jobs.py::create_import_job",
                  "api/app/routers/layer_features.py::import_features",
                  "api/app/routers/layer_fields.py::import_esri_xml"):
        assert known in where, f"the scan no longer finds {known}"


def test_every_body_reading_door_is_listed_or_recorded_as_fitting():
    unaccounted = []
    for method, sample, where in _discovered():
        if limit_for(method, sample) > default_limit():
            continue
        module = where.split("::")[0]
        if where in _FITS_UNDER_DEFAULT or f"{module}::*" in _FITS_UNDER_DEFAULT:
            continue
        unaccounted.append(f"{method} {sample}  ({where})")
    assert not unaccounted, (
        "these doors read a streamed body or carry a file, and are neither in body_limit._LARGER_DOORS "
        "nor recorded in _FITS_UNDER_DEFAULT:\n  " + "\n  ".join(unaccounted)
    )


def test_no_entry_in_the_table_is_dead():
    samples = [(m, s) for m, s, _ in _discovered()]
    for method, pattern, _cap in body_limit._LARGER_DOORS:
        assert any(m == method and pattern.match(s) for m, s in samples), \
            f"{method} {pattern.pattern} matches no route in the source"


def test_the_caps_recorded_as_fitting_do_fit():
    assert settings.attachment_max_bytes + MIB <= default_limit()
    from app.routers.geoservices import _QUERY_FORM_MAX_BYTES
    assert _QUERY_FORM_MAX_BYTES <= default_limit()


def test_the_table_agrees_with_the_doors_own_caps():
    from app.routers import layer_fields

    assert body_limit._SCHEMA_XML_MAX_BYTES == layer_fields._MAX_XML_BYTES
    assert limit_for("POST", "/api/v1/layers/x1/features/import") >= (MAX_FILE_BYTES * 4) // 3
    assert limit_for("POST", "/api/v1/layers/x1/custom-fields/import-esri-xml") >= (layer_fields._MAX_XML_BYTES * 4) // 3
    assert limit_for("POST", "/api/v1/geocode/batch/run") >= MAX_FILE_BYTES
    assert limit_for("POST", "/api/v1/import-jobs") >= settings.import_max_upload_mb * MIB
    assert limit_for("POST", "/api/v1/admin/basemap/upload") >= 4 * 1024 * MIB
    assert limit_for("PUT", "/api/v1/field-projects/x1/archive") >= settings.field_project_max_bytes
