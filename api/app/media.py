"""Media storage functions (Talus 16; generic attachments V068) — over the storage seam.

Thin delegates to the media store (app.storage, decision-object-store-build.md B1): the
filesystem backend under `<storage_root>/talus-media`, replacing the bundled MinIO. The
function surface, docstring contracts, and exception types are unchanged from the SDK era,
so the dozen router/job callers are untouched: everything here is BLOCKING (file I/O) and
must be called via run_in_threadpool; MediaObjectMissing / MediaStoreUnavailable /
StoredObject are re-exported from app.storage for the existing `from ..media import ...`
sites.

"""
from __future__ import annotations

from .storage import (  # noqa: F401 — re-exports are this module's compat surface
    MediaObjectMissing,
    MediaStoreUnavailable,
    StoredObject,
    _endpoint_parts,
    get_media_storage,
)


def read_object(object_key: str) -> bytes:
    """Read an object's full bytes from the media store. BLOCKING — call via a threadpool.
    Raises MediaObjectMissing (absent key) or MediaStoreUnavailable (anything else). At
    pilot scale a photo is a few MB, so a full read is simpler than streaming."""
    return get_media_storage().read(object_key)


def write_object(object_key: str, data: bytes, content_type: str) -> None:
    """Put an object's bytes into the media store (feature attachment upload, V068).
    BLOCKING — call via a threadpool. `data` is the fully-read request body (capped at
    settings.attachment_max_bytes). Raises MediaStoreUnavailable on any store error — the
    caller then rolls back the DB row so a committed attachment row always has its blob
    (routers/attachments.py store ordering)."""
    get_media_storage().write_bytes(object_key, data, content_type)


def write_object_from_file(object_key: str, file_path: str, content_type: str) -> None:
    """Put an object into the media store from a file on disk (field-project .qgz intake,
    V085; may run to the 100 MB cap — never holds the payload in memory). BLOCKING.
    Raises MediaStoreUnavailable on any store error."""
    get_media_storage().write_file(object_key, file_path, content_type)


def iter_object(object_key: str, chunk_size: int = 1024 * 1024):
    """Yield one media object's bytes in chunks — the streamed-download path
    (field-project archive serving, V085). StreamingResponse accepts a sync generator and
    iterates it off the event loop. Raises MediaObjectMissing / MediaStoreUnavailable at
    open time (before the first chunk)."""
    return get_media_storage().iter_chunks(object_key, chunk_size)


def list_object_keys() -> list[StoredObject]:
    """Enumerate every object in the media store (key, size, last-modified). BLOCKING —
    call via a threadpool. The orphaned-media cleanup job (Talus 39) diffs this against
    live media.attachments rows. A store with nothing written yet -> empty list."""
    return get_media_storage().list()


def remove_object(object_key: str) -> None:
    """Delete one object from the media store. BLOCKING — call via a threadpool.
    Idempotent: an absent key does not error. Two in-fence callers, both cruft-only (HARD
    FENCE): the orphaned-media cleanup job (unreferenced AND past grace) and the Talus+ reports
    retention prune (an artifact row beyond keep_last is deleted FIRST, then its bytes —
    decision-scheduled-reports.md F5; a missed blob is collected by the sweep's reports/ leg). The
    reports worker also removes its own TRANSIENT map-staging object (reports/<id>/_maps/<job>.png)
    after the one render that consumes it — same posture: cruft it created, never authoritative media."""
    get_media_storage().delete(object_key)
