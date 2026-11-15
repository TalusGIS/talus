"""The backup's media copy streams; it never holds an object in memory (once-over 2026-11-15 K4).

`run_backup` read each new live blob whole (`FilesystemStorage.read`) and, with encryption on, made a
second whole copy before uploading. A stored raster may be up to the 5 GB upload limit, so one large
imagery upload could make every later backup fail on the 8 GB server the install runbook recommends.
Each object is now uploaded from its own path, or encrypted file to file into a temporary file first.
"""
import asyncio
import inspect
import os
import shutil
import subprocess
import tempfile

import pytest

from app import backup
from app.config import settings
from app.storage import FilesystemStorage, MediaObjectMissing

KEY = "attachments/tal_x/1/0000-aaaa"
PAYLOAD = b"talus media blob " * 4096


@pytest.fixture
def live(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    path = root / "talus-media" / KEY
    path.parent.mkdir(parents=True)
    path.write_bytes(PAYLOAD)
    monkeypatch.setattr(settings, "storage_root", str(root))
    return path


@pytest.fixture
def dest(tmp_path):
    store = FilesystemStorage(tmp_path / "dest")
    store.ensure_root()
    return store


@pytest.fixture
def no_whole_read(monkeypatch):
    def _refuse(self, key):
        raise AssertionError(f"FilesystemStorage.read({key!r}): an object was read whole into memory")

    monkeypatch.setattr(FilesystemStorage, "read", _refuse)


def test_a_plain_copy_streams_from_the_objects_path(live, dest, no_whole_read):
    asyncio.run(backup._copy_media_object(dest, "talus-media", KEY, "media/talus-media/" + KEY, []))
    assert (dest.root / "media" / "talus-media" / KEY).read_bytes() == PAYLOAD


def test_a_missing_object_is_reported_as_missing(live, dest):
    with pytest.raises(MediaObjectMissing):
        asyncio.run(backup._copy_media_object(dest, "talus-media", KEY + "-gone", "media/x", []))


def test_run_backup_copies_media_through_the_streaming_helper():
    src = inspect.getsource(backup.run_backup)
    assert "_copy_media_object(" in src
    assert "_read_source_object(" not in src and "_age_encrypt_bytes(" not in src


def _age_available() -> bool:
    return bool(shutil.which("age") and shutil.which("age-keygen"))


def test_an_encrypted_copy_streams_and_leaves_nothing_behind(live, dest, no_whole_read, tmp_path, monkeypatch):
    if not _age_available():
        pytest.skip("age binary not installed")
    key_file = tmp_path / "key.txt"
    proc = subprocess.run(["age-keygen", "-o", str(key_file)], capture_output=True, text=True, check=True)
    recipient = next(l.split(":", 1)[1].strip() for l in proc.stderr.splitlines() if "public key" in l.lower())
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    asyncio.run(backup._copy_media_object(dest, "talus-media", KEY, "media/talus-media/" + KEY + ".age", [recipient]))

    stored = (dest.root / "media" / "talus-media" / (KEY + ".age")).read_bytes()
    assert stored.startswith(b"age-encryption.org")
    plain = subprocess.run(["age", "-d", "-i", str(key_file)], input=stored, capture_output=True, check=True)
    assert plain.stdout == PAYLOAD
    assert not os.path.exists(str(live) + ".age"), "a .age was written beside the live object"
    assert list(scratch.iterdir()) == [], "the temporary ciphertext was left behind"
