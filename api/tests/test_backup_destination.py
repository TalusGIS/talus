"""Admin-choosable backup destination (decision-backup-destination.md) — resolver + fence correctness.

Pure units (no DB):
  - BackupDestinationUpdate validation — the F4 endpoint fence (explicit scheme, no embedded
    credentials, host[:port] only), bucket charset, access-key presence, and the loud "blank secret"
    rejection (omit = keep is the ONLY way to keep — an empty string must never silently do either).
  - _classify_probe_failure — the test's failure detail is an S3 error CODE or a generic reachability
    line, never the store's response body (no read-back oracle).
  - _store_for — an overridden destination resolved without its secret is a code bug and raises;
    a resolved override builds S3Storage from the override values verbatim.
  - EffectiveBackupDestination.label — the honest-DR label for both kinds.

Opt-in integration (live DB with V090; skips without one):
  - get_effective_backup_destination precedence: all-NULL singleton => env-driven; a set override
    group wins; with_secret=True round-trips the Fernet ciphertext (skips without TALUS_ENCRYPTION_KEY).
  - The V090 CHECKs: a partial override group and a scheme-less endpoint are unrepresentable at rest.
  Each integration case restores the singleton to all-NULL so it leaves no state behind.

Mirrors api/tests/test_backup.py's asyncio.run(case()) idiom (no pytest-asyncio).
"""
import asyncio
import os

import psycopg
import pytest
from pydantic import ValidationError

from app.backup import _store_for
from app.backup_settings import EffectiveBackupDestination, get_effective_backup_destination
from app.config import settings
from app.routers.backups import BackupDestinationUpdate, _classify_probe_failure
from app.secrets import encrypt_secret
from app.storage import S3Storage

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://talus:changeme@localhost:5432/talusgis")


async def _open_or_skip() -> psycopg.AsyncConnection:
    try:
        return await psycopg.AsyncConnection.connect(_DSN, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 — any connect failure -> skip
        pytest.skip(f"no DB at {_DSN}: {exc.__class__.__name__}")


def _upd(**kw):
    base = {"endpoint": "https://s3.example.com", "accessKey": "ak"}
    base.update(kw)
    return BackupDestinationUpdate(**base)


# ---- The F4 endpoint fence (pure) ----------------------------------------------------------------

def test_endpoint_requires_explicit_scheme():
    with pytest.raises(ValidationError):
        _upd(endpoint="s3.example.com")            # scheme-less used to degrade to plaintext
    with pytest.raises(ValidationError):
        _upd(endpoint="ftp://s3.example.com")      # only http(s) speak S3
    assert _upd(endpoint="https://s3.example.com").endpoint == "https://s3.example.com"
    assert _upd(endpoint="http://nas.local:9000").endpoint == "http://nas.local:9000"  # LAN NAS stays legal


def test_endpoint_rejects_embedded_credentials():
    with pytest.raises(ValidationError):
        _upd(endpoint="https://ak:sk@s3.example.com")


def test_endpoint_rejects_path_query_fragment():
    with pytest.raises(ValidationError):
        _upd(endpoint="https://s3.example.com/some/prefix")
    with pytest.raises(ValidationError):
        _upd(endpoint="https://s3.example.com?x=1")
    # A bare trailing slash is tolerated (normalized away), not an error:
    assert _upd(endpoint="https://s3.example.com/").endpoint == "https://s3.example.com"


def test_bucket_charset_and_blank():
    assert _upd(bucket=None).bucket is None
    assert _upd(bucket="  ").bucket is None        # blank = use the env default
    assert _upd(bucket="talus-backups").bucket == "talus-backups"
    with pytest.raises(ValidationError):
        _upd(bucket="bad bucket")                  # whitespace
    with pytest.raises(ValidationError):
        _upd(bucket="bad/bucket")                  # a path is not a bucket


def test_access_key_required():
    with pytest.raises(ValidationError):
        _upd(accessKey="")
    with pytest.raises(ValidationError):
        _upd(accessKey="has space")


def test_blank_secret_is_loudly_rejected():
    """Omitted = keep the stored secret; an explicit empty string must never silently clear OR keep."""
    with pytest.raises(ValidationError):
        _upd(secretKey="")
    with pytest.raises(ValidationError):
        _upd(secretKey="   ")
    assert _upd(secretKey=None).secretKey is None
    assert _upd(secretKey="sk").secretKey == "sk"


# ---- Probe-failure classification (pure) ---------------------------------------------------------

def test_probe_failure_surfaces_s3_code_only():
    msg = "S3 operation failed; code: SignatureDoesNotMatch, message: The request signature we calculated..."
    out = _classify_probe_failure(msg)
    assert "SignatureDoesNotMatch" in out
    assert "request signature we calculated" not in out    # never the store's response body


def test_probe_failure_generic_for_connection_errors():
    out = _classify_probe_failure("HTTPSConnectionPool(host='10.0.0.1', port=443): Max retries exceeded")
    assert "could not be reached" in out
    assert "10.0.0.1" not in out                           # terse; no echo of internals


# ---- _store_for (pure) ---------------------------------------------------------------------------

def test_store_for_overridden_without_secret_is_a_code_bug():
    dest = EffectiveBackupDestination(kind="s3", overridden=True, endpoint="https://s3.example.com",
                                      bucket="b", access_key="ak", secret_key="", secret_key_set=True)
    with pytest.raises(RuntimeError):
        _store_for(dest)


def test_store_for_overridden_builds_s3_from_override_values():
    dest = EffectiveBackupDestination(kind="s3", overridden=True, endpoint="https://s3.example.com",
                                      bucket="custom-bucket", access_key="ak", secret_key="sk",
                                      secret_key_set=True)
    backend = _store_for(dest)
    assert isinstance(backend, S3Storage)
    assert backend.bucket == "custom-bucket"


def test_labels_are_honest_dr_signals():
    s3 = EffectiveBackupDestination(kind="s3", overridden=True, endpoint="https://x", bucket="b",
                                    access_key="a", secret_key="s", secret_key_set=True)
    local = EffectiveBackupDestination(kind="local", overridden=False, bucket="talus-backups")
    assert s3.label == "s3://b (off-host)"
    assert "same-host — not DR" in local.label


# ---- Resolver precedence + V090 CHECKs (opt-in live DB) ------------------------------------------

async def _clear_override(conn: psycopg.AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.backup_settings SET s3_endpoint = NULL, s3_bucket = NULL, "
            "s3_access_key = NULL, s3_secret_key = NULL WHERE id = true"
        )
    await conn.commit()


def test_destination_precedence_live():
    if not settings.talus_encryption_key:
        pytest.skip("TALUS_ENCRYPTION_KEY not set")

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            await _clear_override(conn)
            base = await get_effective_backup_destination(conn)
            assert base.overridden is False           # all-NULL singleton = env-driven

            ct = encrypt_secret("test-secret")
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE platform.backup_settings SET s3_endpoint = %s, s3_bucket = %s, "
                    "s3_access_key = %s, s3_secret_key = %s WHERE id = true",
                    ("https://s3.test.example", "dest-test-bucket", "AKTEST", ct),
                )
            await conn.commit()

            no_secret = await get_effective_backup_destination(conn)
            assert no_secret.overridden and no_secret.kind == "s3"
            assert no_secret.endpoint == "https://s3.test.example"
            assert no_secret.bucket == "dest-test-bucket"
            assert no_secret.access_key == "AKTEST"
            assert no_secret.secret_key == ""         # with_secret=False never decrypts
            assert no_secret.secret_key_set is True

            with_secret = await get_effective_backup_destination(conn, with_secret=True)
            assert with_secret.secret_key == "test-secret"   # Fernet round-trip, app memory only
        finally:
            await _clear_override(conn)
            await conn.close()

    asyncio.run(case())


def test_v090_checks_forbid_partial_and_schemeless_live():
    async def case() -> None:
        conn = await _open_or_skip()
        try:
            await _clear_override(conn)
            # Partial group (endpoint without creds) — unrepresentable at rest:
            with pytest.raises(psycopg.errors.CheckViolation):
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE platform.backup_settings SET s3_endpoint = 'https://x.example' "
                        "WHERE id = true"
                    )
            await conn.rollback()
            # Scheme-less endpoint — the F4 fence pinned at rest:
            with pytest.raises(psycopg.errors.CheckViolation):
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE platform.backup_settings SET s3_endpoint = 'x.example', "
                        "s3_access_key = 'a', s3_secret_key = %s WHERE id = true",
                        (b"not-real-ciphertext",),
                    )
            await conn.rollback()
        finally:
            await _clear_override(conn)
            await conn.close()

    asyncio.run(case())
