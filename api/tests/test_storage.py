"""Unit tests for the storage seam (decision-object-store-build.md B1-B5, B11).

The fence negatives land WITH the seam, before any consumer switches over (B5) — path
traversal is the new attack surface this slice creates, per the slice's Security section.
All filesystem cases run against pytest tmp_path roots; no network, no Docker. The S3
backend is exercised only for its selection logic here — its behavior IS the pre-existing
minio code, re-proven live by the invariant suite and the restore drill (B11).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.storage import (
    FilesystemStorage,
    InvalidObjectKey,
    _endpoint_parts,
    MediaObjectMissing,
    MediaStoreUnavailable,
    S3Storage,
    _TMP_DIR,
    build_backup_storage,
)


@pytest.fixture()
def store(tmp_path: Path) -> FilesystemStorage:
    s = FilesystemStorage(tmp_path / "talus-media")
    s.ensure_root()
    return s


# ---------------------------------------------------------------------------
# The fence (B5) — negative cases first, the per-slice security gate's order
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_key", [
    "",                                   # empty
    "../escape",                          # classic traversal
    "a/../../etc/passwd",                 # traversal buried mid-key
    "a/./b",                              # dot segment
    "a//b",                               # empty segment
    "/etc/passwd",                        # absolute
    "a\\b",                               # backslash (case-insensitive-volume games)
    "a/\x00b",                            # control character
    "a/\x7fb",                            # DEL
    ".tmp/sneak",                         # reserved staging dir
    "x" * 2000,                           # over the length cap
])
def test_fence_rejects(store: FilesystemStorage, bad_key: str):
    for op in (lambda: store.read(bad_key),
               lambda: store.write_bytes(bad_key, b"x"),
               lambda: store.stat(bad_key),
               lambda: store.delete(bad_key)):
        with pytest.raises(InvalidObjectKey):
            op()


def test_fence_rejection_is_store_unavailable(store: FilesystemStorage):
    """InvalidObjectKey must subclass MediaStoreUnavailable so every existing caller
    handles a fence trip fail-closed with no new except arms."""
    with pytest.raises(MediaStoreUnavailable):
        store.read("../escape")


def test_fence_never_touches_outside_root(store: FilesystemStorage, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    with pytest.raises(InvalidObjectKey):
        store.write_bytes("../outside.txt", b"overwritten")
    assert outside.read_bytes() == b"secret"


@pytest.mark.parametrize("good_key", [
    "attachments/3f2a.jpg",
    "basemap/current.pmtiles",
    "rasters/uuid/source.tif",
    "db/talus-20260822-010203.dump.age",
    "with space/unicode-ü.bin",           # F5 §4.7 spot-check classes
])
def test_fence_accepts_real_key_shapes(store: FilesystemStorage, good_key: str):
    store.write_bytes(good_key, b"payload")
    assert store.read(good_key) == b"payload"


# ---------------------------------------------------------------------------
# Symlink escapes — the resolve()-containment half of the fence. The segment
# rules above can't see these: every key here is well-formed, and only the
# containment check refuses them. (Named as the one unasserted gap by the
# slice's /security-review, 2026-08-23.)
# ---------------------------------------------------------------------------

def test_symlinked_dir_cannot_escape_root(store: FilesystemStorage, tmp_path: Path):
    """A link planted INSIDE the store pointing OUT of it must not become a door —
    on any op, in either direction (no host read, no write landing outside)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"host secret")
    (store.root / "escape").symlink_to(outside)

    for op in (lambda: store.read("escape/secret.txt"),
               lambda: store.stat("escape/secret.txt"),
               lambda: store.path_for("escape/secret.txt"),   # the GDAL seam (B4)
               lambda: store.iter_chunks("escape/secret.txt"),
               lambda: store.write_bytes("escape/planted.bin", b"x"),
               lambda: store.delete("escape/secret.txt")):
        with pytest.raises(InvalidObjectKey):
            op()
    assert (outside / "secret.txt").read_bytes() == b"host secret"  # not read away, not deleted
    assert not (outside / "planted.bin").exists()                   # no write escaped


def test_symlinked_file_cannot_escape_root(store: FilesystemStorage, tmp_path: Path):
    """The single-file variant: an innocent-looking key whose target is a host file."""
    outside_file = tmp_path / "outside.txt"
    outside_file.write_bytes(b"host secret")
    (store.root / "innocent.jpg").symlink_to(outside_file)
    with pytest.raises(InvalidObjectKey):
        store.read("innocent.jpg")


def test_symlink_staying_inside_root_still_resolves(store: FilesystemStorage):
    """Containment, NOT a blanket symlink ban — a link whose target stays under the
    root is served normally, so the fence can't be called over-broad."""
    store.write_bytes("real/data.bin", b"payload")
    (store.root / "alias").symlink_to(store.root / "real")
    assert store.read("alias/data.bin") == b"payload"


def test_list_does_not_walk_symlinked_dirs(store: FilesystemStorage, tmp_path: Path):
    """Enumeration must not leak the host either: os.walk does not follow symlinked
    directories, so an escape link contributes no keys to a listing (and therefore
    none to the backup copy or the storage report, which both walk this)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"host secret")
    (store.root / "escape").symlink_to(outside)
    store.write_bytes("real.bin", b"x")
    assert [o.key for o in store.list()] == ["real.bin"]


# ---------------------------------------------------------------------------
# Round-trip semantics — the ten-operation contracts the consumers rely on
# ---------------------------------------------------------------------------

def test_write_read_stat_roundtrip(store: FilesystemStorage):
    store.write_bytes("a/b.bin", b"0123456789")
    st = store.stat("a/b.bin")
    assert st.size == 10
    assert st.etag  # present and stable until the file changes
    assert st.last_modified is not None and st.last_modified.tzinfo is not None
    assert store.stat("a/b.bin").etag == st.etag


def test_etag_changes_on_rewrite(store: FilesystemStorage):
    store.write_bytes("k", b"one")
    first = store.stat("k").etag
    store.write_bytes("k", b"two-longer")
    assert store.stat("k").etag != first


def test_read_range(store: FilesystemStorage):
    store.write_bytes("r", b"0123456789")
    assert store.read_range("r", 0, 4) == b"0123"
    assert store.read_range("r", 7, 100) == b"789"   # clamped at EOF, like the SDK
    with pytest.raises(MediaObjectMissing):
        store.read_range("absent", 0, 4)


def test_iter_chunks_streams_and_raises_at_open(store: FilesystemStorage):
    store.write_bytes("big", b"ab" * 1500)
    chunks = list(store.iter_chunks("big", chunk_size=1024))
    assert b"".join(chunks) == b"ab" * 1500
    assert len(chunks) == 3
    with pytest.raises(MediaObjectMissing):
        store.iter_chunks("absent")  # the open-time contract: raises BEFORE iteration


def test_missing_key_is_missing_not_unavailable(store: FilesystemStorage):
    for op in (lambda: store.read("nope"),
               lambda: store.stat("nope"),
               lambda: store.read_range("nope", 0, 1)):
        with pytest.raises(MediaObjectMissing):
            op()


def test_directory_is_not_an_object(store: FilesystemStorage):
    store.write_bytes("dir/file", b"x")
    with pytest.raises(MediaObjectMissing):
        store.read("dir")
    with pytest.raises(MediaObjectMissing):
        store.stat("dir")


def test_delete_idempotent_and_prunes_empty_dirs(store: FilesystemStorage):
    store.write_bytes("deep/nest/leaf.bin", b"x")
    store.delete("deep/nest/leaf.bin")
    store.delete("deep/nest/leaf.bin")  # second delete: no error (contract)
    assert not (store.root / "deep").exists()  # empty husks pruned (ls-legibility)
    assert store.root.is_dir()                 # never the root itself


def test_delete_prune_stops_at_nonempty(store: FilesystemStorage):
    store.write_bytes("shared/a.bin", b"x")
    store.write_bytes("shared/b.bin", b"x")
    store.delete("shared/a.bin")
    assert (store.root / "shared" / "b.bin").exists()


def test_write_file_from_external_source(store: FilesystemStorage, tmp_path: Path):
    src = tmp_path / "upload.tif"
    src.write_bytes(b"COG" * 1000)
    store.write_file("rasters/x/source.tif", str(src), fsync=True)
    assert store.read("rasters/x/source.tif") == b"COG" * 1000
    assert src.exists()  # source untouched (copy, not move)


def test_replace_promotes_atomically(store: FilesystemStorage):
    store.write_bytes("basemap/current.pmtiles", b"old")
    store.write_bytes("basemap/staging.pmtiles", b"new")
    store.replace("basemap/staging.pmtiles", "basemap/current.pmtiles")
    assert store.read("basemap/current.pmtiles") == b"new"
    with pytest.raises(MediaObjectMissing):
        store.stat("basemap/staging.pmtiles")


def test_replace_missing_src(store: FilesystemStorage):
    with pytest.raises(MediaObjectMissing):
        store.replace("absent", "dest")


# ---------------------------------------------------------------------------
# list() — S3 string-prefix semantics + .tmp invisibility
# ---------------------------------------------------------------------------

def test_list_string_prefix_semantics(store: FilesystemStorage):
    store.write_bytes("db/a.dump", b"1")
    store.write_bytes("db/deep/b.dump", b"22")
    store.write_bytes("db-old/c.dump", b"3")
    keys = sorted(o.key for o in store.list("db/"))
    assert keys == ["db/a.dump", "db/deep/b.dump"]  # "db-old/" NOT matched — S3 semantics
    assert {o.key for o in store.list()} == {"db/a.dump", "db/deep/b.dump", "db-old/c.dump"}
    sizes = {o.key: o.size for o in store.list("db/")}
    assert sizes["db/deep/b.dump"] == 2


def test_dotfile_keys_are_listed_and_readable(store: FilesystemStorage):
    """The `.env` lifeboat artifact is `env/.env-<stamp>.age` — a DOTFILE. Retention prunes by
    listing, so a listing that hid dotfiles would leak every env artifact forever (and an `ls`-based
    port of the old mc count did exactly that in verify-t45, 2026-08-23). os.walk sees them; this
    pins it against a future switch to iterdir/glob."""
    store.write_bytes("env/.env-20260823-045900.age", b"ciphertext")
    assert [o.key for o in store.list("env/")] == ["env/.env-20260823-045900.age"]
    assert store.read("env/.env-20260823-045900.age") == b"ciphertext"


def test_list_missing_root_is_empty(tmp_path: Path):
    s = FilesystemStorage(tmp_path / "never-created")
    assert s.list() == []  # missing bucket => nothing stored yet (established contract)


def test_tmp_dir_never_listed(store: FilesystemStorage):
    (store.root / _TMP_DIR / "leftover").write_bytes(b"partial write debris")
    store.write_bytes("real.bin", b"x")
    assert [o.key for o in store.list()] == ["real.bin"]


def test_delete_prefix(store: FilesystemStorage):
    store.write_bytes("rasters/id1/source.tif", b"x")
    store.write_bytes("rasters/id1/thumb.png", b"y")
    store.write_bytes("rasters/id2/source.tif", b"z")
    store.delete_prefix("rasters/id1/")
    assert [o.key for o in store.list()] == ["rasters/id2/source.tif"]


# ---------------------------------------------------------------------------
# Atomicity + health
# ---------------------------------------------------------------------------

def test_failed_write_leaves_no_partial_object(store: FilesystemStorage):
    class Boom(Exception):
        pass

    def exploding_write(f):
        f.write(b"half")
        raise Boom()

    with pytest.raises(Boom):
        store._atomic_write(store.root / "victim.bin", exploding_write, fsync=False)
    assert not (store.root / "victim.bin").exists()   # dest never appeared
    assert list((store.root / _TMP_DIR).iterdir()) == []  # tmp cleaned up


def test_overwrite_is_atomic_replace(store: FilesystemStorage):
    store.write_bytes("k", b"aaaa")
    store.write_bytes("k", b"bb")
    assert store.read("k") == b"bb"  # never a torn aaaa/bb hybrid; size fully replaced


def test_healthcheck_green_and_red(store: FilesystemStorage, tmp_path: Path):
    store.healthcheck()  # green: returns
    with pytest.raises(MediaStoreUnavailable):
        FilesystemStorage(tmp_path / "absent").healthcheck()  # red: raises (same contract)


def test_ensure_root_idempotent(tmp_path: Path):
    s = FilesystemStorage(tmp_path / "new-store")
    s.ensure_root()
    s.ensure_root()
    s.healthcheck()


# ---------------------------------------------------------------------------
# Backup-target selection (B6) — all / none / partial / scheme fence
# (build_backup_storage replaced the cached get_backup_storage accessor — the destination is
#  runtime-editable now (V090), so construction takes RESOLVED values and caches nothing.)
# ---------------------------------------------------------------------------

def _select_backup(endpoint: str, access: str, secret: str):
    from app.config import settings

    return build_backup_storage(endpoint=endpoint, access_key=access, secret_key=secret,
                                bucket=settings.backup_bucket)


def test_backup_target_none_set_is_local(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    backend = _select_backup("", "", "")
    assert isinstance(backend, FilesystemStorage)
    assert backend.root == tmp_path / settings.backup_bucket


def test_backup_target_all_set_is_s3():
    backend = _select_backup("https://s3.example.com", "ak", "sk")
    assert isinstance(backend, S3Storage)
    from app.config import settings

    assert backend.bucket == settings.backup_bucket


def test_backup_target_partial_config_raises():
    """An operator who set an endpoint believes backups go off-host; silently writing
    them to the local disk instead would be fail-open. Partial config = loud error."""
    with pytest.raises(MediaStoreUnavailable):
        _select_backup("https://s3.example.com", "", "")


def test_backup_target_schemeless_endpoint_raises():
    """The F4 fence (decision-backup-destination.md): a scheme-less endpoint used to slip
    through urlparse as secure=False — plaintext dumps across a network by accident. Now it
    raises loudly, on the env path and the DB-override path alike."""
    with pytest.raises(MediaStoreUnavailable):
        _select_backup("s3.example.com", "ak", "sk")


def test_backup_target_http_stays_allowed():
    """http:// is a legitimate choice (an on-LAN NAS without TLS) — the fence requires the
    scheme to be EXPLICIT, it does not force TLS (the UI labels the plaintext transport)."""
    backend = _select_backup("http://nas.local:9000", "ak", "sk")
    assert isinstance(backend, S3Storage)


def test_endpoint_parts_splits_url_and_tls():
    """Rescued from tests/test_media_integration.py, deleted with the pilot built-ins
    (decision-retire-builtins.md phase 2). It lived there only by history: `_endpoint_parts`
    is storage-seam code and SURVIVES, and its one remaining consumer is the OFF-HOST backup
    target (BACKUP_S3_ENDPOINT) — so the fixtures are shaped like real off-host endpoints
    rather than an internal service name. Deleting the file would have silently dropped
    coverage of live code; §5.1 of the decision doc exists to catch exactly this."""
    assert _endpoint_parts("http://nas.internal:9000") == ("nas.internal:9000", False)
    assert _endpoint_parts("https://s3.us-west-002.backblazeb2.com") == ("s3.us-west-002.backblazeb2.com", True)
