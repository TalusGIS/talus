"""Storage seam — pluggable object storage behind one interface (the MinIO replacement).

decision-object-store-build.md B1–B5, on decision-object-store.md F1–F6: MinIO's upstream
was archived 2026-04-25; the ruled fix is filesystem storage by default, with S3 kept only
where it earns its place — the off-host backup target now, an external-S3 media/raster
backend later (R1). Every byte the product stores goes through a StorageBackend here.

Two backends:

  - FilesystemStorage — v1 backend for media + rasters, and the default backup target
    (R2: local-dir backups under the storage root — same-host honesty unchanged from the
    old internal-MinIO default, labeled not-DR in the UI). Keys map 1:1 to relative paths
    under the backend root (B3): `talus-media/attachments/<uuid>` on S3 is
    `<root>/attachments/<uuid>` on disk — the deliberate migration boundary.
  - S3Storage — a thin wrapper over the minio SDK (minio-py is Apache-2.0 and a separate,
    still-maintained repo from the archived server; boto3 is the recorded fallback). In v1
    it is constructed ONLY as the off-host backup target (BACKUP_S3_* fully set).

Write discipline (B5): every filesystem write lands in `<root>/.tmp/<uuid>` and is then
os.replace()d into place, so a reader can never observe a partial object (S3 puts were
atomic; plain file writes are not). `.tmp` lives inside the backend root — same volume,
so replace never crosses devices — and is invisible to list()/size walks.

The fence (B5 — the 9d zip-slip class): `_key_to_path` is the single chokepoint every
filesystem op goes through. Keys are server-generated (build-stage §1.7 verified no user
string reaches one), so a violation can only mean a bug or an attack: it raises
InvalidObjectKey and never "corrects" the path.

All methods are BLOCKING (file I/O or urllib3) — callers run them via run_in_threadpool,
exactly as they did with the SDK. Exceptions keep their established meaning (media.py
re-exports them, so router imports are untouched): MediaObjectMissing = the key is absent;
MediaStoreUnavailable = the store itself misbehaved (permissions, disk error, unreachable
or misbehaving S3).
"""
from __future__ import annotations

import io
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Protocol
from urllib.parse import urlparse

from .config import settings


class MediaObjectMissing(Exception):
    """The object key is absent from the store — a DB row references a gone object."""


class MediaStoreUnavailable(Exception):
    """The store could not be reached / misbehaved (IO error, permissions, bad S3)."""


class InvalidObjectKey(MediaStoreUnavailable):
    """A key failed the path fence. Keys are server-generated, so this is a bug or an
    attack — fail loudly, never 'correct' the path. Subclasses MediaStoreUnavailable so
    every existing caller handles it fail-closed with no new except arms."""


@dataclass(frozen=True)
class StoredObject:
    """One listed object — the fields the cleanup/backup/report jobs need."""

    key: str
    size: int
    last_modified: datetime | None  # tz-aware UTC; None only if the backend omits it


@dataclass(frozen=True)
class ObjectStat:
    """stat() result. On the filesystem backend `etag` is `{mtime_ns:x}-{size:x}` —
    it changes iff the file changes, which is all the basemap ETag header needs."""

    size: int
    etag: str
    last_modified: datetime | None


_TMP_DIR = ".tmp"       # per-backend-root staging dir; excluded from list()/walks
_KEY_MAX_LEN = 1024     # sane cap well above any real key (longest today < 120 chars)


def _validate_key(key: str, *, allow_trailing_slash: bool = False) -> None:
    """The character/segment half of the fence (shared by keys and list prefixes)."""
    if not key or len(key) > _KEY_MAX_LEN:
        raise InvalidObjectKey(f"invalid object key (empty or too long): {key[:80]!r}")
    if "\\" in key or key.startswith("/"):
        raise InvalidObjectKey(f"invalid object key (separator): {key!r}")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in key):
        raise InvalidObjectKey("invalid object key (control character)")
    check = key.rstrip("/") if allow_trailing_slash else key
    segments = check.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise InvalidObjectKey(f"invalid object key (dot/empty segment): {key!r}")
    if segments[0] == _TMP_DIR:
        raise InvalidObjectKey(f"invalid object key (reserved staging dir): {key!r}")


def _key_to_path(root: Path, key: str) -> Path:
    """The fence chokepoint: validated key -> path, with a resolve-and-containment check
    (belt and braces over the segment rules; also refuses a symlink escaping the root)."""
    _validate_key(key)
    path = root / key
    if not path.resolve().is_relative_to(root.resolve()):
        raise InvalidObjectKey(f"object key escapes storage root: {key!r}")
    return path


class StorageBackend(Protocol):
    """The ten-operation surface measured at kickoff, plus ranged read / stat / replace
    (the basemap consumer the kickoff map missed — build-stage §1.1)."""

    def read(self, key: str) -> bytes: ...
    def read_range(self, key: str, offset: int, length: int) -> bytes: ...
    def iter_chunks(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]: ...
    def stat(self, key: str) -> ObjectStat: ...
    def write_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream",
                    *, fsync: bool = False) -> None: ...
    def write_file(self, key: str, src_path: str, content_type: str = "application/octet-stream",
                   *, fsync: bool = False) -> None: ...
    def list(self, prefix: str = "") -> list[StoredObject]: ...
    def delete(self, key: str) -> None: ...
    def delete_prefix(self, prefix: str) -> None: ...
    def replace(self, src_key: str, dst_key: str) -> None: ...
    def ensure_root(self) -> None: ...
    def healthcheck(self) -> None: ...


# ---------------------------------------------------------------------------
# Filesystem backend
# ---------------------------------------------------------------------------

class FilesystemStorage:
    """Objects as plain files under `root`; key = relative path. A sysadmin can `ls` it
    and understand the layout — that legibility is part of the point (F5 §4.2)."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    # -- internals ---------------------------------------------------------

    def _tmp_path(self) -> Path:
        tmp_dir = self.root / _TMP_DIR
        tmp_dir.mkdir(parents=True, exist_ok=True)
        return tmp_dir / uuid.uuid4().hex

    def _atomic_write(self, dest: Path, write_to, *, fsync: bool) -> None:
        """Write via .tmp then os.replace — the only mutation path (B5). `write_to` is a
        callable(file) so bytes- and file-sources share the one discipline."""
        tmp = self._tmp_path()
        try:
            with open(tmp, "wb") as f:
                write_to(f)
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dest)
        except InvalidObjectKey:
            raise
        except OSError as exc:
            raise MediaStoreUnavailable(str(exc)) from exc
        finally:
            tmp.unlink(missing_ok=True)  # no-op after a successful replace

    def _prune_empty_parents(self, path: Path) -> None:
        """Best-effort: S3 has no directories, so a delete should not leave empty husks
        behind (keeps the on-disk layout `ls`-legible). Stops at the root or the first
        non-empty dir; races/permissions are ignored — pruning is cosmetic."""
        current = path.parent
        root = self.root.resolve()
        while current.resolve() != root and current.name != _TMP_DIR:
            try:
                current.rmdir()  # fails (OSError) unless empty — that is the stop signal
            except OSError:
                return
            current = current.parent

    # -- ops ---------------------------------------------------------------

    def path_for(self, key: str) -> Path:
        """Fenced absolute path for a key — the GDAL seam (B4): rasterio/rio-tiler open
        COGs by real filesystem path, replacing `/vsis3/`. Filesystem backend only; an S3
        media/raster backend later reopens the /vsis3/ question (the R1 fast-follow)."""
        return _key_to_path(self.root, key)

    def read(self, key: str) -> bytes:
        path = _key_to_path(self.root, key)
        try:
            return path.read_bytes()
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as exc:
            raise MediaObjectMissing(key) from exc
        except OSError as exc:
            raise MediaStoreUnavailable(str(exc)) from exc

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        path = _key_to_path(self.root, key)
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                return f.read(length)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as exc:
            raise MediaObjectMissing(key) from exc
        except OSError as exc:
            raise MediaStoreUnavailable(str(exc)) from exc

    def iter_chunks(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        """Open errors raise HERE (missing key before the first chunk — the established
        iter_object contract); the returned generator only ever raises mid-stream on IO."""
        path = _key_to_path(self.root, key)
        try:
            f = open(path, "rb")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as exc:
            raise MediaObjectMissing(key) from exc
        except OSError as exc:
            raise MediaStoreUnavailable(str(exc)) from exc

        def _gen() -> Iterator[bytes]:
            with f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk

        return _gen()

    def stat(self, key: str) -> ObjectStat:
        path = _key_to_path(self.root, key)
        try:
            st = path.stat()
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise MediaObjectMissing(key) from exc
        except OSError as exc:
            raise MediaStoreUnavailable(str(exc)) from exc
        if not path.is_file():  # a directory is not an object (S3 has no directories)
            raise MediaObjectMissing(key)
        return ObjectStat(
            size=st.st_size,
            etag=f"{st.st_mtime_ns:x}-{st.st_size:x}",
            last_modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
        )

    def write_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream",
                    *, fsync: bool = False) -> None:
        # content_type intentionally unused: serving paths read MIME from DB rows
        # (build-stage §1.6), never from store metadata.
        dest = _key_to_path(self.root, key)
        self._atomic_write(dest, lambda f: f.write(data), fsync=fsync)

    def write_file(self, key: str, src_path: str, content_type: str = "application/octet-stream",
                   *, fsync: bool = False) -> None:
        dest = _key_to_path(self.root, key)

        def _copy(f) -> None:
            with open(src_path, "rb") as src:
                shutil.copyfileobj(src, f, length=1024 * 1024)

        self._atomic_write(dest, _copy, fsync=fsync)

    def list(self, prefix: str = "") -> list[StoredObject]:
        """S3 semantics: `prefix` is a STRING prefix on keys, not a directory ("db/"
        matches db/x but not db-old/x). The walk starts at the deepest directory the
        prefix implies, so a deep prefix never scans the whole tree."""
        if prefix:
            _validate_key(prefix, allow_trailing_slash=True)
        start = self.root
        if "/" in prefix:
            start = _key_to_path(self.root, prefix.rsplit("/", 1)[0])
        out: list[StoredObject] = []
        if not start.is_dir():
            return out
        try:
            for dirpath, dirnames, filenames in os.walk(start):
                if Path(dirpath) == self.root and _TMP_DIR in dirnames:
                    dirnames.remove(_TMP_DIR)
                for name in filenames:
                    p = Path(dirpath) / name
                    key = p.relative_to(self.root).as_posix()
                    if not key.startswith(prefix):
                        continue
                    st = p.stat()
                    out.append(StoredObject(
                        key=key,
                        size=st.st_size,
                        last_modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                    ))
        except OSError as exc:
            raise MediaStoreUnavailable(str(exc)) from exc
        return out

    def delete(self, key: str) -> None:
        """Idempotent — an absent key is fine (the established remove_object contract)."""
        path = _key_to_path(self.root, key)
        try:
            path.unlink(missing_ok=True)
        except (IsADirectoryError, PermissionError) as exc:
            # a key naming a directory is not an object -> nothing to delete
            if path.is_dir():
                return
            raise MediaStoreUnavailable(str(exc)) from exc
        except OSError as exc:
            raise MediaStoreUnavailable(str(exc)) from exc
        self._prune_empty_parents(path)

    def delete_prefix(self, prefix: str) -> None:
        for obj in self.list(prefix):
            self.delete(obj.key)

    def replace(self, src_key: str, dst_key: str) -> None:
        """Atomic promote (basemap staging -> current) — strictly better than the S3
        copy-then-delete it replaces: a reader sees old bytes or new bytes, never neither."""
        src = _key_to_path(self.root, src_key)
        dst = _key_to_path(self.root, dst_key)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
        except FileNotFoundError as exc:
            raise MediaObjectMissing(src_key) from exc
        except OSError as exc:
            raise MediaStoreUnavailable(str(exc)) from exc
        self._prune_empty_parents(src)

    def ensure_root(self) -> None:
        """mkdir -p — the make_bucket equivalent (B3: directories are app-created on
        first use; no compose bootstrap replaces minio-setup)."""
        try:
            (self.root / _TMP_DIR).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MediaStoreUnavailable(str(exc)) from exc

    def healthcheck(self) -> None:
        """The bucket_exists-probe equivalent: cheapest call that proves the store is
        present and writable. Same contract — returns on green, raises on red."""
        if not self.root.is_dir():
            raise MediaStoreUnavailable(f"storage root missing: {self.root}")
        if not os.access(self.root, os.W_OK | os.X_OK):
            raise MediaStoreUnavailable(f"storage root not writable: {self.root}")


# ---------------------------------------------------------------------------
# S3 backend (the surviving S3 client — off-host backup target in v1)
# ---------------------------------------------------------------------------

def _endpoint_parts(url: str) -> tuple[str, bool]:
    """Split a store URL ('https://s3.example.com') into (host:port, secure) for the SDK,
    which wants a bare netloc plus a TLS flag rather than a full URL."""
    parsed = urlparse(url)
    return parsed.netloc or parsed.path, parsed.scheme == "https"


class S3Storage:
    """The minio-SDK code that already existed, behind the seam's surface. Error mapping
    preserves every established contract: NoSuchKey/NoSuchBucket -> MediaObjectMissing,
    list on a missing bucket -> empty, delete idempotent, anything else -> Unavailable."""

    def __init__(self, endpoint_url: str, access_key: str, secret_key: str, bucket: str,
                 *, timeout_seconds: float | None = None):
        from minio import Minio  # imported here so the fs-only path never loads urllib3 machinery

        netloc, secure = _endpoint_parts(endpoint_url)
        self.bucket = bucket
        # timeout_seconds: the admin connection test (decision-backup-destination.md F3) bounds its
        # probe rather than hanging a request worker on an unreachable endpoint; backup jobs keep
        # the SDK defaults (a slow WAN upload must not be cut short).
        http_client = None
        if timeout_seconds is not None:
            import urllib3

            http_client = urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=timeout_seconds, read=timeout_seconds),
                retries=urllib3.Retry(total=1),
            )
        self._client = Minio(netloc, access_key=access_key, secret_key=secret_key, secure=secure,
                             http_client=http_client)

    # -- internals ---------------------------------------------------------

    def _missing_codes(self):
        return ("NoSuchKey", "NoSuchBucket")

    def _get(self, key: str, **kwargs):
        from minio.error import S3Error

        try:
            return self._client.get_object(self.bucket, key, **kwargs)
        except S3Error as exc:
            if exc.code in self._missing_codes():
                raise MediaObjectMissing(key) from exc
            raise MediaStoreUnavailable(str(exc)) from exc
        except Exception as exc:  # urllib3 / connection errors -> store unreachable
            raise MediaStoreUnavailable(str(exc)) from exc

    # -- ops ---------------------------------------------------------------

    def read(self, key: str) -> bytes:
        resp = self._get(key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        resp = self._get(key, offset=offset, length=length)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def iter_chunks(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        resp = self._get(key)  # open errors raise here, before the first chunk

        def _gen() -> Iterator[bytes]:
            try:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                resp.close()
                resp.release_conn()

        return _gen()

    def stat(self, key: str) -> ObjectStat:
        from minio.error import S3Error

        try:
            st = self._client.stat_object(self.bucket, key)
        except S3Error as exc:
            if exc.code in self._missing_codes():
                raise MediaObjectMissing(key) from exc
            raise MediaStoreUnavailable(str(exc)) from exc
        except Exception as exc:
            raise MediaStoreUnavailable(str(exc)) from exc
        return ObjectStat(size=st.size or 0, etag=(st.etag or "").strip('"'),
                          last_modified=st.last_modified)

    def write_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream",
                    *, fsync: bool = False) -> None:
        try:
            self._client.put_object(self.bucket, key, io.BytesIO(data), length=len(data),
                                    content_type=content_type)
        except Exception as exc:
            raise MediaStoreUnavailable(str(exc)) from exc

    def write_file(self, key: str, src_path: str, content_type: str = "application/octet-stream",
                   *, fsync: bool = False) -> None:
        try:
            self._client.fput_object(self.bucket, key, src_path, content_type=content_type)
        except Exception as exc:
            raise MediaStoreUnavailable(str(exc)) from exc

    def list(self, prefix: str = "") -> list[StoredObject]:
        from minio.error import S3Error

        try:
            return [
                StoredObject(key=o.object_name, size=o.size or 0, last_modified=o.last_modified)
                for o in self._client.list_objects(self.bucket, prefix=prefix or None, recursive=True)
            ]
        except S3Error as exc:
            if exc.code == "NoSuchBucket":
                return []
            raise MediaStoreUnavailable(str(exc)) from exc
        except Exception as exc:
            raise MediaStoreUnavailable(str(exc)) from exc

    def delete(self, key: str) -> None:
        try:
            self._client.remove_object(self.bucket, key)  # idempotent server-side
        except Exception as exc:
            raise MediaStoreUnavailable(str(exc)) from exc

    def delete_prefix(self, prefix: str) -> None:
        for obj in self.list(prefix):
            self.delete(obj.key)

    def replace(self, src_key: str, dst_key: str) -> None:
        """copy-then-delete (server-side; the SDK's 5 GiB copy limit sits above the
        basemap's 4 GiB cap, as today)."""
        from minio.commonconfig import CopySource
        from minio.error import S3Error

        try:
            self._client.copy_object(self.bucket, dst_key, CopySource(self.bucket, src_key))
            self._client.remove_object(self.bucket, src_key)
        except S3Error as exc:
            if exc.code in self._missing_codes():
                raise MediaObjectMissing(src_key) from exc
            raise MediaStoreUnavailable(str(exc)) from exc
        except Exception as exc:
            raise MediaStoreUnavailable(str(exc)) from exc

    def ensure_root(self) -> None:
        try:
            if not self._client.bucket_exists(self.bucket):
                self._client.make_bucket(self.bucket)
        except Exception as exc:
            raise MediaStoreUnavailable(str(exc)) from exc

    def healthcheck(self) -> None:
        try:
            self._client.bucket_exists(self.bucket)
        except Exception as exc:
            raise MediaStoreUnavailable(str(exc)) from exc


# ---------------------------------------------------------------------------
# Role accessors — the get_minio()/get_backup_minio() replacements
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_media_storage() -> FilesystemStorage:
    return FilesystemStorage(Path(settings.storage_root) / settings.media_bucket)


@lru_cache(maxsize=1)
def get_raster_storage() -> FilesystemStorage:
    return FilesystemStorage(Path(settings.storage_root) / settings.raster_bucket)


def require_backup_endpoint_scheme(endpoint: str) -> None:
    """The F4 fence (decision-backup-destination.md): an explicit scheme, always. urlparse on a
    scheme-less endpoint used to degrade to secure=False — plaintext dumps across a network by
    accident. Raises the seam's own error so the backup job surfaces it as a visible error row.
    Applies to BOTH the env trio and the V090 DB override (the API also 422s at PUT time)."""
    if not (endpoint.startswith("https://") or endpoint.startswith("http://")):
        raise MediaStoreUnavailable(
            f"backup S3 endpoint '{endpoint}' must include an explicit http:// or https:// "
            "scheme — a scheme-less endpoint silently disables TLS"
        )


def build_backup_storage(*, endpoint: str, access_key: str, secret_key: str,
                         bucket: str) -> StorageBackend:
    """Construct the backup target from RESOLVED values (the honest-DR seam, B6): all three
    S3 values set -> off-host S3; none set -> the local subtree under `bucket` (R2). A PARTIAL
    set raises rather than silently falling back — an operator who set an endpoint believes
    backups are going off-host, and quietly writing them to the local disk instead would be the
    worst kind of fail-open. The scheduled job surfaces the raise as an error row — a visible
    alarm, not a silent wrong default.

    Deliberately NOT cached (the old lru_cache accessor is gone): the destination is now
    runtime-editable (V090), so every backup run / download / connection-test resolves the
    effective destination and constructs its own backend — that is the no-restart mechanism.
    Construction is cheap at backup cadence."""
    parts = (endpoint, access_key, secret_key)
    if all(parts):
        require_backup_endpoint_scheme(endpoint)
        return S3Storage(endpoint, access_key, secret_key, bucket)
    if any(parts):
        raise MediaStoreUnavailable(
            "the backup S3 destination is partially configured (endpoint/access/secret must "
            "all be set for an off-host target, or all empty for the local default)")
    return FilesystemStorage(Path(settings.storage_root) / bucket)
