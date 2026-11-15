"""Raster store ops (V045+, raster store & serve) — over the storage seam.

Thin delegates to the raster store (app.storage, decision-object-store-build.md B1): the
filesystem backend under `<storage_root>/talus-rasters`, replacing the bundled MinIO. The
api remains the ONLY write path — internal-only, never browser-exposed (decision-16 Q2):
raster ingest streams the uploaded COG THROUGH the api to here, exactly as the media
proxy moves photo bytes.

`local_path()` is the GDAL seam (B4): rasterio/rio-tiler open the COG straight off disk
by real path, replacing `/vsis3/` — no HTTP round-trip per tile read, no AWS_* process
env, no GDAL_DISABLE_READDIR_ON_OPEN workaround. The path is fenced (app.storage
_key_to_path) like every other key.

Blocking file I/O — every function here must be called via run_in_threadpool.
"""
from __future__ import annotations

from .storage import get_raster_storage


def local_path(object_key: str) -> str:
    """Fenced absolute filesystem path for an object in the raster store — what GDAL/
    rasterio/rio-tiler open. Replaces vsis3_path() (B4)."""
    return str(get_raster_storage().path_for(object_key))


def read_object(object_key: str) -> bytes:
    """Read an object's full bytes from the raster store (used for the small thumbnail
    proxy). BLOCKING. Raises MediaObjectMissing / MediaStoreUnavailable like
    media.read_object."""
    return get_raster_storage().read(object_key)


def put_file(object_key: str, file_path: str, content_type: str) -> None:
    """Upload a local file to the raster store (streamed copy; never holds the COG in
    memory). BLOCKING."""
    get_raster_storage().write_file(object_key, file_path, content_type)


def put_bytes(object_key: str, data: bytes, content_type: str) -> None:
    """Upload in-memory bytes (the small ingest-generated thumbnail) to the raster store.
    BLOCKING."""
    get_raster_storage().write_bytes(object_key, data, content_type)


def remove_prefix(prefix: str) -> None:
    """Best-effort delete of every object under a raster's prefix (`rasters/<id>/` = COG +
    thumb). BLOCKING. Idempotent (an absent key does not error). Used to roll back a
    failed ingest and (c7) to purge a deleted raster's bytes."""
    get_raster_storage().delete_prefix(prefix)
