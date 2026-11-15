"""PostgreSQL dialect, end to end against a real database (decision-sql-source-pull.md s1).

Uses TEST_DATABASE_URL (default: the dev stack's own Postgres) as the SOURCE: a throwaway schema
with the §5 types is created, read through the dialect's catalog / counts / stream, coerced, and
dropped. Skips when the database is absent. This bypasses `vet_host` on purpose (the stack's own
database is on the never-list for a REAL pull — inv47 proves that live); what is under test here
is the dialect: quoting, the server-side cursor, the type map and VALUES, not counts.

Run:   cd api && python -m pytest tests/test_sql_source_dialect_integration.py -q
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import zoneinfo
from urllib.parse import urlsplit

import pytest

from app.sqlsource import dialects as d

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://talus:changeme@localhost:5432/talusgis")
SCHEMA = "sqlpull_itest"


def _info() -> d.ConnInfo:
    u = urlsplit(_DSN)
    return d.ConnInfo(host=u.hostname or "localhost", port=u.port or 5432, database=(u.path or "/talusgis").lstrip("/"),
                      username=u.username or "talus", password=u.password, tls_mode="disable", timezone="America/Chicago")


async def _seed():
    import psycopg
    async with await psycopg.AsyncConnection.connect(_DSN, autocommit=True) as c:
        await c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await c.execute(f"CREATE SCHEMA {SCHEMA}")
        await c.execute(f"""
            CREATE TABLE {SCHEMA}.billing_accounts (
                account_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                pin varchar(20) UNIQUE,
                "Odd Name" text,
                balance numeric(12,2),
                units int4,
                big int8,
                active bool,
                opened date,
                read_at timestamp,
                read_at_utc timestamptz,
                tags text[],
                meta jsonb,
                blob bytea,
                note text
            )""")
        await c.execute(f"""
            INSERT INTO {SCHEMA}.billing_accounts (pin, "Odd Name", balance, units, big, active, opened, read_at, read_at_utc, tags, meta, blob, note) VALUES
            ('12-34-100-001', 'a', 1250.75, 3, 9007199254740993, true,  '2026-09-28', '2026-09-28 14:30:00', '2026-09-28 19:30:00+00', ARRAY['x','y'], '{{"k": 1}}', '\\x0001', 'héllo'),
            ('12-34-100-002', NULL, NULL, NULL, NULL, false, NULL, NULL, NULL, NULL, NULL, NULL, ''),
            ('12-34-100-003', 'c', -0.01, 0, 0, NULL, '1999-12-31', '2026-01-01 00:00:00', '2026-01-01 06:00:00+00', ARRAY[]::text[], '[]', NULL, NULL)
        """)
        await c.execute(f"CREATE VIEW {SCHEMA}.v_accounts AS SELECT pin, balance FROM {SCHEMA}.billing_accounts")


async def _drop():
    import psycopg
    async with await psycopg.AsyncConnection.connect(_DSN, autocommit=True) as c:
        await c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


@pytest.fixture(scope="module")
def seeded():
    try:
        asyncio.run(_seed())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no database at TEST_DATABASE_URL: {type(exc).__name__}")
    yield
    asyncio.run(_drop())


async def _open():
    info = _info()
    return await d.DIALECTS["postgresql"].open(info, info.host if d.is_ip_literal_host(info.host) else await _first_ip(info.host))


async def _first_ip(host: str) -> str:
    """The first IPv4 answer: Docker publishes the dev port on 127.0.0.1 only, and `localhost`
    resolves to ::1 first on macOS (a harness detail; a real pull pins whatever the guard returns)."""
    from app.netguard import default_resolver
    addrs = await default_resolver(host, 5432)
    return next((a for a in addrs if ":" not in a), addrs[0])


def test_catalog_counts_stream_and_values(seeded):
    async def go():
        session = await _open()
        try:
            await session.ping()
            tables = {(t.schema, t.name): t for t in await session.catalog_tables()}
            tbl = tables[(SCHEMA, "billing_accounts")]
            assert tbl.kind == "table" and set(tbl.key_columns) == {"account_id", "pin"}
            assert tables[(SCHEMA, "v_accounts")].kind == "view" and tables[(SCHEMA, "v_accounts")].key_columns == ()

            cols = {c.name: c for c in await session.catalog_columns(SCHEMA, "billing_accounts")}
            assert cols["balance"].data_type == "number" and cols["balance"].numeric_subtype == "double"
            assert cols["units"].numeric_subtype == "long_integer" and cols["big"].numeric_subtype == "double"   # 64-bit → double
            assert cols["read_at"].temporal and cols["read_at_utc"].temporal
            assert cols["blob"].data_type is None and cols["tags"].data_type == "text"
            assert cols["account_id"].data_type == "text"

            names = ["pin", "Odd Name", "balance", "units", "big", "active", "opened", "read_at", "read_at_utc", "tags", "meta", "note"]
            counts = await session.counts(SCHEMA, "billing_accounts", names)
            assert counts.total == 3 and counts.non_null["balance"] == 2 and counts.non_null["note"] == 2

            rows = [r async for r in session.stream(SCHEMA, "billing_accounts", names)]
            assert len(rows) == 3
            zone = zoneinfo.ZoneInfo("America/Chicago")
            by_pin = {}
            for r in rows:
                vals = {}
                for n, v in zip(names, r):
                    c = cols[n]
                    vals[n] = d.coerce_value(v, data_type=c.data_type, temporal=c.temporal, utc=(n == "read_at_utc"), zone=zone)
                by_pin[vals["pin"]] = vals
            a = by_pin["12-34-100-001"]
            assert a["Odd Name"] == "a" and a["balance"] == 1250.75 and a["units"] == 3 and a["big"] == 9007199254740993
            assert a["active"] is True and a["opened"] == "2026-09-28"
            assert a["read_at"] == "2026-09-28T14:30:00-05:00"          # naive → Central (the connection zone)
            # aware → the SAME INSTANT with an offset (the server renders it in its session zone, UTC here)
            assert _dt.datetime.fromisoformat(a["read_at_utc"]) == _dt.datetime(2026, 9, 28, 19, 30, tzinfo=_dt.timezone.utc)
            assert _dt.datetime.fromisoformat(a["read_at_utc"]).utcoffset() is not None
            assert a["tags"] == '["x", "y"]' and a["meta"] == '{"k": 1}' and a["note"] == "héllo"
            b = by_pin["12-34-100-002"]
            assert b["balance"] is None and b["active"] is False and b["note"] == "" and b["opened"] is None
            c3 = by_pin["12-34-100-003"]
            assert c3["balance"] == -0.01 and c3["units"] == 0 and c3["active"] is None and c3["tags"] == "[]"
        finally:
            await session.close()
    asyncio.run(go())


def test_session_is_read_only(seeded):
    async def go():
        session = await _open()
        try:
            import psycopg
            # The raw connection, deliberately: the SESSION itself refuses a write (25006) before any
            # login privilege is consulted — Talus never writes to a source, by construction.
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                async with session._conn.cursor() as cur:
                    await cur.execute(f"DELETE FROM {SCHEMA}.billing_accounts")
        finally:
            await session.close()
    asyncio.run(go())


def test_missing_table_and_column_are_named_errors(seeded):
    async def go():
        session = await _open()
        try:
            with pytest.raises(d.SourceError) as exc:
                await session.catalog_columns(SCHEMA, "nope")
            assert exc.value.reason == "table_missing"
            with pytest.raises(d.SourceError) as exc2:
                await session.counts(SCHEMA, "billing_accounts", ["gone"])
            assert exc2.value.reason == "column_missing"
        finally:
            await session.close()
    asyncio.run(go())
