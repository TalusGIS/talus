"""Talus 40 — DR backup engine correctness.

Pure-logic units (no live DB / object store):
  - _incremental_targets: the APPEND-ONLY media diff — only blobs not already in the backup are copied, and
    dest keys are namespaced by source bucket so two buckets can't collide.
  - _select_prunable: the retention rule — old beyond the floor is pruned, but the M-most-recent floor is
    ALWAYS kept (even with retention_days=0, so a misconfig can never prune to zero).

Opt-in integration (live DB; skips without one):
  - prune_old_backups honors the floor + age against real platform.backups rows (inserted with NULL artifact
    keys so no object-store call is made), then cleans up its test rows.

Mirrors api/tests/test_maintenance_integration.py's asyncio.run(case()) idiom (no pytest-asyncio).
"""
import asyncio
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

import app.backup as backup
from app.backup import (
    _age_encrypt_bytes,
    _age_encrypt_file,
    _backup_env_file,
    _incremental_targets,
    _select_prunable,
    env_file_readable,
    prune_old_backups,
)
from app.backup_settings import EffectiveBackupSettings
from app.media import StoredObject

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://talus:changeme@localhost:5432/talusgis")


async def _open_or_skip() -> psycopg.AsyncConnection:
    try:
        return await psycopg.AsyncConnection.connect(_DSN, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 — any connect failure -> skip
        pytest.skip(f"no DB at {_DSN}: {exc.__class__.__name__}")


# ---- _incremental_targets (append-only media diff) ----------------------------------------------

def test_incremental_targets_skips_already_backed_up():
    objs = {
        "talus-media": [
            StoredObject(key="a.jpg", size=10, last_modified=None),
            StoredObject(key="b.jpg", size=20, last_modified=None),
        ]
    }
    existing = {"media/talus-media/a.jpg"}  # a.jpg already in the backup
    targets = _incremental_targets(objs, existing)
    assert [t[1] for t in targets] == ["b.jpg"]                 # only the NEW blob
    assert targets[0][2] == "media/talus-media/b.jpg"           # dest key namespaced by source bucket
    assert targets[0][3] == 20                                  # size carried through


def test_incremental_targets_namespaces_by_source_bucket():
    objs = {
        "talus-media": [StoredObject(key="x", size=1, last_modified=None)],
        "talus-raster": [StoredObject(key="x", size=2, last_modified=None)],  # SAME key, different bucket
    }
    targets = _incremental_targets(objs, set())
    dests = {t[2] for t in targets}
    assert dests == {"media/talus-media/x", "media/talus-raster/x"}  # no collision


def test_incremental_targets_empty_when_all_present():
    objs = {"talus-media": [StoredObject(key="a", size=1, last_modified=None)]}
    assert _incremental_targets(objs, {"media/talus-media/a"}) == []


def test_incremental_targets_suffix_matches_stored_keys():
    # When encrypting, dest keys carry the .age suffix; the existing-key check must compare against THAT.
    objs = {"talus-media": [StoredObject(key="a", size=1, last_modified=None)]}
    targets = _incremental_targets(objs, set(), dest_suffix=".age")
    assert targets[0][2] == "media/talus-media/a.age"
    # already-stored encrypted blob is skipped
    assert _incremental_targets(objs, {"media/talus-media/a.age"}, dest_suffix=".age") == []


# ---- Talus 47 / B1: suffix-AGNOSTIC skip — toggling encryption must not duplicate the archive --------

def test_incremental_targets_no_dup_when_encryption_toggled_on():
    # A plaintext blob is already archived; turning encryption ON must NOT re-copy it as <key>.age.
    objs = {"talus-media": [StoredObject(key="a", size=1, last_modified=None)]}
    assert _incremental_targets(objs, {"media/talus-media/a"}, dest_suffix=".age") == []


def test_incremental_targets_no_dup_when_encryption_toggled_off():
    # An encrypted blob is already archived; turning encryption OFF must NOT re-copy it as plaintext <key>.
    objs = {"talus-media": [StoredObject(key="a", size=1, last_modified=None)]}
    assert _incremental_targets(objs, {"media/talus-media/a.age"}, dest_suffix="") == []


def test_incremental_targets_new_blob_still_copied_in_current_form():
    # A genuinely-new blob (in NEITHER form) is still copied, in the current (encrypted) form.
    objs = {"talus-media": [StoredObject(key="n", size=3, last_modified=None)]}
    targets = _incremental_targets(objs, {"media/talus-media/other"}, dest_suffix=".age")
    assert [t[1] for t in targets] == ["n"]
    assert targets[0][2] == "media/talus-media/n.age"


def test_incremental_targets_superset_invariant_no_blob_dropped():
    # THE no-harm property: after the diff, EVERY source blob is represented — either already archived in some
    # form, or queued to copy. Nothing is silently dropped (the cleanup must not lose a blob restore needs).
    objs = {"talus-media": [
        StoredObject(key="p", size=1, last_modified=None),   # already archived plaintext
        StoredObject(key="e", size=2, last_modified=None),   # already archived encrypted
        StoredObject(key="n", size=3, last_modified=None),   # brand new
    ]}
    existing = {"media/talus-media/p", "media/talus-media/e.age"}
    queued = {t[1] for t in _incremental_targets(objs, existing, dest_suffix=".age")}
    assert queued == {"n"}                                   # only the new blob copied; p/e NOT re-copied
    for o in objs["talus-media"]:                            # and every blob is represented (none dropped)
        base = f"media/talus-media/{o.key}"
        assert base in existing or f"{base}.age" in existing or o.key in queued


# ---- age client-side encryption round-trip (Talus 40.1; opt-in on the `age` binary) -------------

def _age_available() -> bool:
    return bool(shutil.which("age") and shutil.which("age-keygen"))


def _gen_age_keypair(tmp_path) -> str:
    """Write a throwaway age identity to tmp_path/key.txt; return the public key (recipient)."""
    key_file = tmp_path / "key.txt"
    proc = subprocess.run(
        ["age-keygen", "-o", str(key_file)], capture_output=True, text=True, check=True
    )
    # age-keygen prints "Public key: age1..." to stderr.
    for line in proc.stderr.splitlines():
        if "public key" in line.lower():
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"could not parse public key from age-keygen: {proc.stderr!r}")


def test_age_encrypt_bytes_roundtrip(tmp_path):
    if not _age_available():
        pytest.skip("age binary not installed")
    recipient = _gen_age_keypair(tmp_path)

    plaintext = b"talus backup secret payload " * 500
    # Talus 43: recipients are passed explicitly (no settings monkeypatch) — the helpers are settings-free.
    cipher = asyncio.run(_age_encrypt_bytes(plaintext, [recipient]))
    assert cipher != plaintext
    assert cipher.startswith(b"age-encryption.org")  # the age format header
    # decrypt with the private key -> original bytes
    dec = subprocess.run(
        ["age", "-d", "-i", str(tmp_path / "key.txt")],
        input=cipher, capture_output=True, check=True,
    )
    assert dec.stdout == plaintext


def test_age_encrypt_file_roundtrip(tmp_path):
    if not _age_available():
        pytest.skip("age binary not installed")
    recipient = _gen_age_keypair(tmp_path)

    src = tmp_path / "plain.bin"
    payload = os.urandom(200_000)  # binary, like a pg_dump artifact
    src.write_bytes(payload)
    enc_path = asyncio.run(_age_encrypt_file(str(src), [recipient]))
    try:
        assert enc_path.endswith(".age") and os.path.exists(enc_path)
        with open(enc_path, "rb") as f:
            assert f.read(20).startswith(b"age-encryption.org")  # ciphertext, not the plaintext
        dec = subprocess.run(
            ["age", "-d", "-i", str(tmp_path / "key.txt"), enc_path],
            capture_output=True, check=True,
        )
        assert dec.stdout == payload  # decrypts byte-for-byte
    finally:
        if os.path.exists(enc_path):
            os.unlink(enc_path)


def test_age_multi_recipient_either_key_decrypts(tmp_path):
    if not _age_available():
        pytest.skip("age binary not installed")
    primary = _gen_age_keypair(tmp_path)
    # second custodian's key in its own file
    bg_dir = tmp_path / "bg"
    bg_dir.mkdir()
    breakglass = _gen_age_keypair(bg_dir)

    cipher = asyncio.run(_age_encrypt_bytes(b"both can open this", [primary, breakglass]))
    for key in (tmp_path / "key.txt", bg_dir / "key.txt"):
        dec = subprocess.run(
            ["age", "-d", "-i", str(key)], input=cipher, capture_output=True, check=True
        )
        assert dec.stdout == b"both can open this"  # the break-glass key works too


# ---- _select_prunable (retention: age AND a non-zero floor) -------------------------------------

def _row(i: int, age_days: float):
    now = datetime.now(timezone.utc)
    return (i, f"db/b{i}.dump", now - timedelta(days=age_days))


def test_select_prunable_prunes_old_beyond_floor():
    now = datetime.now(timezone.utc)
    rows = [_row(1, 1), _row(2, 10), _row(3, 40), _row(4, 50)]  # ids 3,4 are >30d old
    prunable = _select_prunable(rows, now, retention_days=30, min_keep=2)
    ids = {p[0] for p in prunable}
    assert ids == {3, 4}            # old AND beyond the 2-most-recent floor


def test_select_prunable_floor_always_kept():
    now = datetime.now(timezone.utc)
    rows = [_row(i, 100) for i in range(1, 6)]  # ALL ancient
    prunable = _select_prunable(rows, now, retention_days=30, min_keep=3)
    assert len(prunable) == 2       # 5 ancient - keep 3 most-recent = prune 2
    # the 3 newest (smallest ages here are equal; floor is by recency order) survive


def test_select_prunable_zero_days_never_prunes_to_zero():
    now = datetime.now(timezone.utc)
    rows = [_row(i, i) for i in range(1, 5)]  # 4 backups
    prunable = _select_prunable(rows, now, retention_days=0, min_keep=2)
    # days=0 => the age test passes for everything, but the floor still protects the 2 most-recent.
    assert len(prunable) == 2       # never the whole set
    survivors = {r[0] for r in rows} - {p[0] for p in prunable}
    assert len(survivors) == 2


def test_select_prunable_keeps_fresh_beyond_floor():
    now = datetime.now(timezone.utc)
    rows = [_row(1, 1), _row(2, 2), _row(3, 3), _row(4, 4)]  # all young
    prunable = _select_prunable(rows, now, retention_days=30, min_keep=1)
    assert prunable == []           # young enough to keep even beyond the floor


# ---- prune_old_backups against a live DB (opt-in) ----------------------------------------------

def test_prune_old_backups_live():
    # Talus 43: retention is threaded in via `eff` (explicit, not settings/singleton state) — days=30,
    # floor=1, so the single most-recent 'ok' backup survives and both 45d-old rows prune.
    eff = EffectiveBackupSettings(
        interval_seconds=86400, retention_days=30, retention_min_keep=1,
        age_recipients=(), scheduled_enabled=True, overrides=frozenset(),
    )

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            # Two OLD (45d) + one FRESH 'ok' backup, all with NULL artifact keys so no object-store call is
            # made. With min_keep=1 the floor keeps the single most-recent (the fresh one); both old prune.
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO platform.backups (created_at, kind, status, target, detail) VALUES "
                    "(now() - interval '45 days', 'scheduled', 'ok', 't40-test', 't40-test-old1'), "
                    "(now() - interval '46 days', 'scheduled', 'ok', 't40-test', 't40-test-old2'), "
                    "(now(),                       'scheduled', 'ok', 't40-test', 't40-test-fresh')"
                )
            await conn.commit()

            result = await prune_old_backups(conn, eff)
            assert result.items_affected >= 2  # at least our two old test rows

            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT detail FROM platform.backups WHERE target = 't40-test'"
                )
                remaining = {r[0] for r in await cur.fetchall()}
            assert "t40-test-fresh" in remaining   # floor kept the most-recent
            assert "t40-test-old1" not in remaining
            assert "t40-test-old2" not in remaining
        finally:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM platform.backups WHERE target = 't40-test'")
            await conn.commit()
            await conn.close()

    asyncio.run(case())


# ---- Talus 45: the `.env` lifeboat (encrypt-only; the fail-closed AND) ---------------------------

def test_env_file_readable_true_and_false(tmp_path, monkeypatch):
    # The PUT precondition + the includeEnvEffective signal: True iff the configured .env is present+readable.
    p = tmp_path / ".env"
    p.write_text("SECRET=x\n")
    monkeypatch.setattr(backup.settings, "backup_env_path", str(p))
    assert env_file_readable() is True
    monkeypatch.setattr(backup.settings, "backup_env_path", str(tmp_path / "absent.env"))
    assert env_file_readable() is False


def test_backup_env_file_returns_none_when_unreadable(tmp_path, monkeypatch):
    # When the mounted .env is unreadable, _backup_env_file returns None BEFORE any age call (the copy fails
    # first) — the caller turns that None into a loud error, never a silent partial. No `age` binary needed.
    monkeypatch.setattr(backup.settings, "backup_env_path", str(tmp_path / "absent.env"))

    async def case() -> object:
        # A syntactically-plausible recipient; never reached because the file can't be read. The store
        # is likewise never touched (helpers take the run's resolved store first since V090).
        store = backup.FilesystemStorage(tmp_path / "bk")
        return await _backup_env_file(store,
                                      ["age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"],
                                      "20260101-000000")

    assert asyncio.run(case()) is None


def test_prune_removes_all_artifacts_including_env_live(monkeypatch):
    # Talus 45 / F-retention: prune removes the WHOLE artifact set of a pruned backup — db + keycloak +
    # openfga + env — not just the talusgis dump (fixing the pre-existing kc/of orphan). Object-store helpers
    # are monkeypatched to RECORD keys (no live store needed); the row math runs against the live DB.
    removed: list[str] = []
    monkeypatch.setattr(backup, "_remove_backup_object", lambda store, key: removed.append(key))
    monkeypatch.setattr(backup, "_stat_backup_size", lambda store, key: 100)
    eff = EffectiveBackupSettings(
        interval_seconds=86400, retention_days=30, retention_min_keep=1,
        age_recipients=(), scheduled_enabled=True, overrides=frozenset(),
    )

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            # One 45d-old 'ok' backup carrying all four artifact keys + one FRESH (floor-protected). With
            # min_keep=1 the floor keeps the fresh row; the old one prunes and ALL its keys are removed.
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO platform.backups (created_at, kind, status, target, detail, "
                    " db_object_key, keycloak_object_key, openfga_object_key, env_object_key) VALUES "
                    "(now() - interval '45 days', 'scheduled', 'ok', 't45-test', 't45-old', "
                    " 'db/talusgis-old.dump.age', 'db/keycloak-old.dump.age', 'db/openfga-old.dump.age', "
                    " 'env/.env-old.age'), "
                    "(now(), 'scheduled', 'ok', 't45-test', 't45-fresh', 'db/talusgis-fresh.dump.age', "
                    " NULL, NULL, NULL)"
                )
            await conn.commit()

            await prune_old_backups(conn, eff)

            assert "env/.env-old.age" in removed            # the lifeboat artifact is pruned with its set
            assert "db/keycloak-old.dump.age" in removed    # kc/of orphan fixed (was left behind pre-T45)
            assert "db/openfga-old.dump.age" in removed
            assert "db/talusgis-old.dump.age" in removed
            assert "db/talusgis-fresh.dump.age" not in removed  # floor protected the most-recent
        finally:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM platform.backups WHERE target = 't45-test'")
            await conn.commit()
            await conn.close()

    asyncio.run(case())


# ---- V070 regression guard: the primary dump must run on the OWNER role -------------------------

def test_primary_dump_dsn_prefers_owner_role(monkeypatch):
    # The talusgis dump MUST use the owner DSN. When it silently inherited the least-privilege
    # talus_app pool at V070, every scheduled backup failed on `LOCK TABLE
    # public.flyway_schema_history` — 119 consecutive times, unnoticed for 14 days.
    monkeypatch.setattr(backup.settings, "database_url", "postgresql://talus_app:pw@h/db")
    monkeypatch.setattr(backup.settings, "owner_database_url", "postgresql://talus:pw@h/db")
    assert backup._primary_dump_dsn() == "postgresql://talus:pw@h/db"


def test_primary_dump_dsn_falls_back_before_cutover(monkeypatch):
    # Pre-cutover deploys leave owner_database_url unset; there both DSNs ARE the owner role, so
    # falling back to database_url is correct rather than a privilege downgrade.
    monkeypatch.setattr(backup.settings, "database_url", "postgresql://talus:pw@h/db")
    monkeypatch.setattr(backup.settings, "owner_database_url", None)
    assert backup._primary_dump_dsn() == "postgresql://talus:pw@h/db"
