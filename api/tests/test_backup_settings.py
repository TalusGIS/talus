"""Talus 43 — editable backup settings: validation + COALESCE-over-env precedence.

Pure units (no DB):
  - BackupSettingsUpdate validation — the keep-at-least floor can't go below 1 (mirrors the DB CHECK), the
    cadence must be > 0, retention days >= 0, and recipients must look like `age1…` PUBLIC keys (so a typo /
    a pasted private key can't silently disable encryption). null/[] recipients are allowed (= use env).

Opt-in integration (live DB; skips without one):
  - get_effective_backup_settings honors COALESCE(db_override, env): an all-NULL singleton yields the env
    defaults (no overrides flagged); a set column wins and is flagged; a partial override mixes correctly.
    Each integration case restores the singleton to all-NULL (env-driven) so it leaves no state behind.

Mirrors api/tests/test_backup.py's asyncio.run(case()) idiom (no pytest-asyncio).
"""
import asyncio
import os

import psycopg
import pytest
from pydantic import ValidationError

from app.backup_settings import get_effective_backup_settings
from app.config import settings
from app.routers.backups import BackupSettingsUpdate

# A valid-FORMAT age public key: "age1" + 58 chars in [0-9a-z]. The validator does a cheap FORMAT check
# (not a bech32 checksum verify), so this exercises exactly what ships — no live keypair / `age` binary needed.
GOOD = "age1" + "q" * 58

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://talus:changeme@localhost:5432/talusgis")


# ---- BackupSettingsUpdate validation (pure) -----------------------------------------------------

def test_floor_cannot_go_below_one():
    with pytest.raises(ValidationError):
        BackupSettingsUpdate(retentionMinKeep=0)          # the floor can never be 0 from the UI either
    assert BackupSettingsUpdate(retentionMinKeep=1).retentionMinKeep == 1


def test_interval_must_be_positive():
    with pytest.raises(ValidationError):
        BackupSettingsUpdate(intervalSeconds=0)
    assert BackupSettingsUpdate(intervalSeconds=21600).intervalSeconds == 21600


def test_retention_days_non_negative():
    with pytest.raises(ValidationError):
        BackupSettingsUpdate(retentionDays=-1)
    assert BackupSettingsUpdate(retentionDays=0).retentionDays == 0   # 0 days is valid (the floor protects)


def test_bad_recipient_rejected():
    with pytest.raises(ValidationError):
        BackupSettingsUpdate(ageRecipients=["not-an-age-key"])
    with pytest.raises(ValidationError):
        BackupSettingsUpdate(ageRecipients=["AGE-SECRET-KEY-1QQQQ"])   # a private key pasted by mistake
    with pytest.raises(ValidationError):
        BackupSettingsUpdate(ageRecipients=["age1tooshort"])           # right prefix, wrong length


def test_good_recipients_pass_and_blanks_dropped():
    u = BackupSettingsUpdate(ageRecipients=[GOOD, "   ", GOOD])
    assert u.ageRecipients == [GOOD, GOOD]                            # blank line tolerated/dropped


def test_empty_and_null_recipients_allowed():
    assert BackupSettingsUpdate(ageRecipients=[]).ageRecipients == []        # explicit empty (=> use env)
    assert BackupSettingsUpdate(ageRecipients=None).ageRecipients is None    # null (=> use env)
    assert BackupSettingsUpdate().ageRecipients is None                      # omitted defaults to null


def test_too_many_recipients_rejected():
    with pytest.raises(ValidationError):
        BackupSettingsUpdate(ageRecipients=[GOOD] * 21)                      # > the 20 cap


# ---- get_effective_backup_settings COALESCE precedence (opt-in live DB) -------------------------

async def _open_or_skip() -> psycopg.AsyncConnection:
    try:
        return await psycopg.AsyncConnection.connect(_DSN, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 — any connect failure -> skip
        pytest.skip(f"no DB at {_DSN}: {exc.__class__.__name__}")


async def _set_singleton(conn, iv, rd, rmk, ar, se) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.backup_settings SET interval_seconds = %s, retention_days = %s, "
            "retention_min_keep = %s, age_recipients = %s, scheduled_enabled = %s WHERE id = true",
            (iv, rd, rmk, ar, se),
        )
    await conn.commit()


def test_effective_all_null_uses_env():
    async def case() -> None:
        conn = await _open_or_skip()
        try:
            await _set_singleton(conn, None, None, None, None, None)
            eff = await get_effective_backup_settings(conn)
            assert eff.interval_seconds == settings.backup_interval_seconds
            assert eff.retention_days == settings.backup_retention_days
            assert eff.retention_min_keep == settings.backup_retention_min_keep
            assert eff.scheduled_enabled == settings.backup_enabled
            assert eff.age_recipients == tuple(settings.backup_age_recipients_list)
            assert eff.overrides == frozenset()      # nothing overridden => env drives everything
        finally:
            await conn.close()

    asyncio.run(case())


def test_effective_overrides_win_and_are_flagged():
    async def case() -> None:
        conn = await _open_or_skip()
        try:
            await _set_singleton(conn, 111, 222, 5, GOOD, False)
            eff = await get_effective_backup_settings(conn)
            assert eff.interval_seconds == 111
            assert eff.retention_days == 222
            assert eff.retention_min_keep == 5
            assert eff.scheduled_enabled is False    # a False override wins (NOT confused with "unset")
            assert eff.age_recipients == (GOOD,)
            assert eff.encryption_enabled is True
            assert eff.overrides == frozenset(
                {"interval_seconds", "retention_days", "retention_min_keep", "age_recipients", "scheduled_enabled"}
            )
        finally:
            await _set_singleton(conn, None, None, None, None, None)   # leave it env-driven
            await conn.close()

    asyncio.run(case())


def test_effective_partial_override_mixes():
    async def case() -> None:
        conn = await _open_or_skip()
        try:
            await _set_singleton(conn, None, 99, None, None, None)     # ONLY retention_days overridden
            eff = await get_effective_backup_settings(conn)
            assert eff.retention_days == 99                            # the override wins
            assert eff.interval_seconds == settings.backup_interval_seconds  # the rest fall back to env
            assert eff.scheduled_enabled == settings.backup_enabled
            assert eff.overrides == frozenset({"retention_days"})      # only that knob flagged custom
        finally:
            await _set_singleton(conn, None, None, None, None, None)
            await conn.close()

    asyncio.run(case())
