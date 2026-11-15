"""Public-sharing master switch — the resolver's full F2 truth table + live COALESCE integration
(decision-public-sharing-reachable.md).

Pure units (no DB): every row of the F2 resolution table against fake connections — INCLUDING the
one row a live box cannot prove without recreating a container: column NULL + env FALSE resolves
CLOSED ("row D", the fresh install). inv29 proves whichever NULL row the live box embodies by real
anonymous request; these units prove all four rows deterministically, on any box, in CI, forever.
Plus fail-closed: a connection that cannot answer resolves False (deny) even when the env fallback
says open, and source() reports 'default'.

Opt-in integration (live DB; skips without one): the same table against the REAL schema — a fake
cannot prove the SELECT matches V092's actual column name. Captures the singleton's original value
first and restores it in `finally` (the running api serves off this row live, the same brief-flip
contract inv29 uses).

Mirrors api/tests/test_backup_settings.py — the same COALESCE-over-env pattern (Talus 43, V042) —
and its asyncio.run(case()) idiom (no pytest-asyncio).
"""
import asyncio
import os

import psycopg
import pytest

from app.config import settings
from app.public_sharing import public_sharing_enabled, public_sharing_source

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://talus:changeme@localhost:5432/talusgis")


# ---- fakes: just enough psycopg surface for the resolver's one SELECT ---------------------------

class _AsyncCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeCursor(_AsyncCM):
    def __init__(self, row):
        self._row = row

    async def execute(self, sql):
        pass

    async def fetchone(self):
        return self._row


class _FakeConn:
    """Returns one fixed row. row=(True,)/(False,)/(None,) are the column's three states;
    row=None is the singleton row missing entirely (the defensive branch — V028 seeds one)."""

    def __init__(self, row):
        self._row = row

    def transaction(self):
        return _AsyncCM()

    def cursor(self):
        return _FakeCursor(self._row)


class _ExplodingConn:
    """Fails at first touch — the pre-V092 / DB-down shape."""

    def transaction(self):
        raise RuntimeError("boom")


def _resolve(conn) -> bool:
    return asyncio.run(public_sharing_enabled(conn))


def _source(conn) -> str:
    return asyncio.run(public_sharing_source(conn))


# ---- the F2 truth table (pure) ------------------------------------------------------------------

def test_column_true_wins_over_env_false(monkeypatch):
    monkeypatch.setattr(settings, "public_anon_access_enabled", False)
    assert _resolve(_FakeConn((True,))) is True
    assert _source(_FakeConn((True,))) == "override"


def test_column_false_wins_over_env_true(monkeypatch):
    # The admin turned it OFF while .env still says on — the admin wins.
    monkeypatch.setattr(settings, "public_anon_access_enabled", True)
    assert _resolve(_FakeConn((False,))) is False
    assert _source(_FakeConn((False,))) == "override"


def test_null_follows_env_true(monkeypatch):
    # The UPGRADE row: an existing public deployment keeps serving across V092.
    monkeypatch.setattr(settings, "public_anon_access_enabled", True)
    assert _resolve(_FakeConn((None,))) is True
    assert _source(_FakeConn((None,))) == "default"


def test_null_follows_env_false(monkeypatch):
    # ROW D — the fresh install: nothing decided anywhere resolves CLOSED. The row a live box
    # with env=true cannot prove without a container recreate; proven here instead.
    monkeypatch.setattr(settings, "public_anon_access_enabled", False)
    assert _resolve(_FakeConn((None,))) is False
    assert _source(_FakeConn((None,))) == "default"


def test_missing_row_follows_env(monkeypatch):
    # Defensive branch: the singleton row absent behaves exactly like NULL.
    monkeypatch.setattr(settings, "public_anon_access_enabled", False)
    assert _resolve(_FakeConn(None)) is False
    monkeypatch.setattr(settings, "public_anon_access_enabled", True)
    assert _resolve(_FakeConn(None)) is True
    assert _source(_FakeConn(None)) == "default"


def test_read_failure_fails_closed_even_when_env_says_open(monkeypatch):
    # Fail-closed beats the fallback: a switch we cannot READ is OFF, even though a healthy NULL
    # read would have fallen back to env=true. A DB that cannot answer is not a DB that should be
    # publishing to the internet. source() honestly reports 'default' — nothing is overriding.
    monkeypatch.setattr(settings, "public_anon_access_enabled", True)
    assert _resolve(_ExplodingConn()) is False
    assert _source(_ExplodingConn()) == "default"


# ---- opt-in integration: the real schema --------------------------------------------------------

async def _open_or_skip() -> psycopg.AsyncConnection:
    try:
        return await psycopg.AsyncConnection.connect(_DSN)
    except Exception as exc:  # noqa: BLE001 — any connect failure -> skip
        pytest.skip(f"no DB at {_DSN}: {exc.__class__.__name__}")


async def _set_column(conn: psycopg.AsyncConnection, value: bool | None) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.org_settings SET public_sharing_enabled = %s WHERE id = true",
            (value,),
        )
    await conn.commit()


def test_live_coalesce_all_rows(monkeypatch):
    """The full table against the real column — proves the SELECT matches V092's schema, which the
    fakes above cannot. Row D included: the env side is monkeypatched in-process, so no container
    recreate is needed to hold the fresh-install shape against a real database."""

    async def case():
        conn = await _open_or_skip()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT public_sharing_enabled FROM platform.org_settings WHERE id = true"
                )
                row = await cur.fetchone()
            original = None if row is None else row[0]
            try:
                for column, env, want in [
                    (True, False, True),   # admin ON wins over env off
                    (False, True, False),  # admin OFF wins over env on
                    (None, True, True),    # the upgrade row
                    (None, False, False),  # ROW D — the fresh install, closed
                ]:
                    await _set_column(conn, column)
                    monkeypatch.setattr(settings, "public_anon_access_enabled", env)
                    got = await public_sharing_enabled(conn)
                    assert got is want, f"column={column} env={env}: got {got}, want {want}"
                    src = await public_sharing_source(conn)
                    want_src = "default" if column is None else "override"
                    assert src == want_src, f"column={column}: source {src}, want {want_src}"
            finally:
                await _set_column(conn, original)  # the api serves off this row live — put it back
        finally:
            await conn.close()

    asyncio.run(case())
