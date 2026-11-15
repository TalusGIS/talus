"""MySQL / MariaDB dialect, end to end against real servers (decision-sql-source-pull.md §14, s4).

Uses TEST_MYSQL_URL and TEST_MARIADB_URL (mysql://user:pw@host:port/db, a login that may CREATE a
throwaway database) as SOURCES: a throwaway database with the §14 types is created, read through the
dialect's catalog / columns / counts / stream, coerced, and dropped — the same legs on BOTH servers.
Skips when a URL is unset or the server is absent. This bypasses `vet_host` on purpose (what is under
test is the dialect: the pin, the read-only session, quoting, the unbuffered cursor, the type map and
VALUES). The read-only leg runs as the seeding login, which CAN write — the SESSION refuses.

Run:   cd api && TEST_MYSQL_URL=mysql://root:pw@127.0.0.1:13306/citydata \\
                 TEST_MARIADB_URL=mysql://root:pw@127.0.0.1:13307/citydata python -m pytest tests/test_sql_source_mysql_integration.py -q
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import zoneinfo
from urllib.parse import urlsplit

import pytest

from app.sqlsource import dialects as d

DB = "sqlpull_itest"
SERVERS = [s for s in ("TEST_MYSQL_URL", "TEST_MARIADB_URL") if os.environ.get(s)]


def _info(url: str) -> d.ConnInfo:
    u = urlsplit(url)
    return d.ConnInfo(host=u.hostname or "127.0.0.1", port=u.port or 3306, database=(u.path or "/").lstrip("/") or DB,
                      username=u.username or "root", password=u.password, tls_mode="disable", timezone="America/Chicago")


def _root(url: str):
    import pymysql
    i = _info(url)
    return pymysql.connect(host=i.host, port=i.port, user=i.username, password=i.password or "", autocommit=True, charset="utf8mb4")


def _seed(url: str) -> None:
    c = _root(url)
    cur = c.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {DB}")
    cur.execute(f"CREATE DATABASE {DB}")
    cur.execute(f"""CREATE TABLE {DB}.billing_accounts (
        account_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, pin VARCHAR(20) NULL UNIQUE, `Odd Name` TEXT NULL,
        balance DECIMAL(12,2) NULL, units INT NULL, big BIGINT NULL, cnt_u INT UNSIGNED NULL, small_u SMALLINT UNSIGNED NULL,
        active TINYINT(1) NULL, flag BIT(1) NULL, opened DATE NULL, read_at DATETIME NULL, yr YEAR NULL,
        kind ENUM('res','com') NULL, tags SET('x','y') NULL, meta JSON NULL, blob_col BLOB NULL, note TEXT NULL,
        two_col_a INT NULL, two_col_b INT NULL, UNIQUE KEY two_col (two_col_a, two_col_b))""")
    cur.execute(f"""INSERT INTO {DB}.billing_accounts
        (pin, `Odd Name`, balance, units, big, cnt_u, small_u, active, flag, opened, read_at, yr, kind, tags, meta, blob_col, note) VALUES
        ('12-34-100-001', 'a', 1250.75, 3, 9007199254740993, 4000000000, 65535, 1, b'1', '2026-09-28', '2026-09-28 14:30:00', 2026, 'res', 'x,y', '{{"k": 1}}', X'0001', 'héllo'),
        ('12-34-100-002', NULL, NULL, NULL, NULL, NULL, NULL, 0, b'0', NULL, NULL, NULL, NULL, NULL, NULL, NULL, ''),
        ('12-34-100-003', 'c', -0.01, 0, 0, 0, 0, NULL, NULL, '1999-12-31', '2026-01-01 00:00:00', 1999, 'com', '', '[]', NULL, NULL)""")
    cur.execute(f"CREATE VIEW {DB}.v_accounts AS SELECT pin, balance FROM {DB}.billing_accounts")
    cur.close(); c.close()


def _drop(url: str) -> None:
    c = _root(url)
    cur = c.cursor(); cur.execute(f"DROP DATABASE IF EXISTS {DB}"); cur.close(); c.close()


@pytest.fixture(scope="module", params=SERVERS or ["unset"])
def server(request):
    if request.param == "unset":
        pytest.skip("TEST_MYSQL_URL / TEST_MARIADB_URL unset")
    url = os.environ[request.param]
    try:
        _seed(url)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no server at {request.param}: {type(exc).__name__}")
    yield url
    _drop(url)


async def _open(url: str):
    info = _info(url)
    return await d.DIALECTS["mysql"].open(info, info.host if d.is_ip_literal_host(info.host) else "127.0.0.1")


def test_catalog_counts_stream_and_values(server):
    async def go():
        session = await _open(server)
        try:
            tables = {(t.schema, t.name): t for t in await session.catalog_tables()}
            tbl = tables[(DB, "billing_accounts")]
            # PRIMARY first, then the single-column UNIQUE; the two-column unique index is NOT a key candidate
            assert tbl.kind == "table" and tbl.key_columns[0] == "account_id" and set(tbl.key_columns) == {"account_id", "pin"}
            assert tables[(DB, "v_accounts")].kind == "view" and tables[(DB, "v_accounts")].key_columns == ()
            assert not any(sch in ("mysql", "sys", "information_schema", "performance_schema") for sch, _ in tables)
            cols = {c.name: c for c in await session.catalog_columns(DB, "billing_accounts")}
            assert cols["balance"].data_type == "number" and cols["balance"].numeric_subtype == "double"
            assert cols["units"].numeric_subtype == "long_integer" and cols["big"].numeric_subtype == "double"       # 64-bit → double
            assert cols["cnt_u"].numeric_subtype == "double" and cols["small_u"].numeric_subtype == "long_integer"    # unsigned widening
            assert cols["active"].data_type == "boolean" and cols["flag"].data_type == "boolean"                      # tinyint(1), bit(1)
            assert cols["read_at"].temporal and cols["opened"].data_type == "date" and cols["yr"].numeric_subtype == "short_integer"
            assert cols["blob_col"].data_type is None and cols["kind"].data_type == "text" and cols["meta"].data_type == "text"
            assert cols["account_id"].data_type == "number"
            names = ["account_id", "pin", "Odd Name", "balance", "units", "big", "cnt_u", "small_u", "active", "flag",
                     "opened", "read_at", "yr", "kind", "tags", "meta", "note"]
            counts = await session.counts(DB, "billing_accounts", ["balance", "note"])
            assert counts.total == 3 and counts.non_null["balance"] == 2 and counts.non_null["note"] == 2
            located = await session.counts(DB, "billing_accounts", [], located=("units", "big"))
            assert located.total == 2
            rows = [r async for r in session.stream(DB, "billing_accounts", names)]
            assert len(rows) == 3
            zone = zoneinfo.ZoneInfo("America/Chicago")

            def coerced(row):
                out = {}
                for n, v in zip(names, row):
                    c = cols[n]
                    out[n] = d.coerce_value(v, data_type=c.data_type or "text", temporal=c.temporal, utc=False, zone=zone)
                return out
            a, b, c3 = (coerced(r) for r in rows)
            assert a["Odd Name"] == "a" and a["balance"] == 1250.75 and a["units"] == 3 and a["big"] == 9007199254740993
            assert a["cnt_u"] == 4000000000 and a["small_u"] == 65535
            assert a["active"] is True and a["flag"] is True and a["opened"] == "2026-09-28" and a["yr"] == 2026
            assert a["read_at"] == "2026-09-28T14:30:00-05:00"          # naive → Central (the connection zone)
            assert a["kind"] == "res" and a["tags"] == "x,y" and a["note"] == "héllo"
            assert a["meta"] in ('{"k": 1}', '{"k":1}')
            assert b["balance"] is None and b["active"] is False and b["flag"] is False and b["note"] == "" and b["opened"] is None
            assert c3["balance"] == -0.01 and c3["units"] == 0 and c3["active"] is None and c3["tags"] == ""
        finally:
            await session.close()
    asyncio.run(go())


def test_session_is_read_only(server):
    async def go():
        session = await _open(server)
        try:
            import pymysql
            # The raw connection, deliberately, as a login that CAN write: the SESSION refuses (1792)
            # before any privilege is consulted — Talus never writes to a source, by construction.
            with pytest.raises(pymysql.MySQLError) as exc:
                cur = session._conn.cursor()
                try:
                    cur.execute(f"DELETE FROM {DB}.billing_accounts")
                finally:
                    cur.close()
            assert d._my_error(exc.value, _info(server)).reason == "read_only"
            assert (await session.counts(DB, "billing_accounts", [])).total == 3
        finally:
            await session.close()
    asyncio.run(go())


def test_missing_table_and_column_are_named_errors(server):
    async def go():
        session = await _open(server)
        try:
            with pytest.raises(d.SourceError) as exc:
                await session.catalog_columns(DB, "nope")
            assert exc.value.reason == "table_missing"
            with pytest.raises(d.SourceError) as exc2:
                await session.counts(DB, "billing_accounts", ["gone"])
            assert exc2.value.reason == "column_missing"
            with pytest.raises(d.SourceError) as exc3:
                await session.counts("no_such_db", "billing_accounts", [])
            assert exc3.value.reason in ("table_missing", "permission_denied", "database_missing")
        finally:
            await session.close()
    asyncio.run(go())


def test_bad_password_and_tls_modes(server):
    async def go():
        info = _info(server)
        bad = d.ConnInfo(info.host, info.port, info.database, info.username, "definitely-wrong", tls_mode="disable", timezone="UTC")
        with pytest.raises(d.SourceError) as exc:
            await d.DIALECTS["mysql"].open(bad, "127.0.0.1")
        assert exc.value.reason == "auth_failed" and "definitely-wrong" not in str(exc.value)
        # `require` against the server's own auto-generated certificate: encrypted, no name check → opens
        req = d.ConnInfo(info.host, info.port, info.database, info.username, info.password, tls_mode="require", timezone="UTC")
        s = await d.DIALECTS["mysql"].open(req, "127.0.0.1")
        try:
            cur = s._conn.cursor()
            cur.execute("SHOW STATUS LIKE 'Ssl_cipher'")
            cipher = cur.fetchone()
            cur.close()
            assert cipher and cipher[1], "require must actually encrypt"
        finally:
            await s.close()
        # `verify-full` against that self-signed certificate must FAIL as tls_failed (never as a crash)
        vf = d.ConnInfo(info.host, info.port, info.database, info.username, info.password, tls_mode="verify-full", timezone="UTC")
        with pytest.raises(d.SourceError) as exc2:
            await d.DIALECTS["mysql"].open(vf, "127.0.0.1")
        assert exc2.value.reason == "tls_failed"
    asyncio.run(go())


def test_full_auth_on_a_plain_connection_after_the_password_is_reset(server):
    """MySQL 8's caching_sha2_password: the FIRST login after a password change on a PLAIN connection runs
    the RSA full-auth path, which PyMySQL 1.2.3 ends without a packet (_Sha2Handler). Resetting the login's
    password empties the server's cache, so both opens below must succeed; MariaDB (native password) passes
    trivially."""
    async def go():
        info = _info(server)
        c = _root(server)
        cur = c.cursor()
        cur.execute("CREATE USER IF NOT EXISTS 'itest_sha2'@'%%' IDENTIFIED BY %s", ("Itest-pw-1",))
        cur.execute("ALTER USER 'itest_sha2'@'%%' IDENTIFIED BY %s", ("Itest-pw-2",))      # empties the sha2 cache
        cur.execute(f"GRANT SELECT ON {DB}.* TO 'itest_sha2'@'%'")
        cur.close(); c.close()
        plain = d.ConnInfo(info.host, info.port, DB, "itest_sha2", "Itest-pw-2", tls_mode="disable", timezone="UTC")
        for attempt in ("uncached (full auth)", "cached (fast auth)"):
            s = await d.DIALECTS["mysql"].open(plain, "127.0.0.1")
            try:
                await s.ping()
            finally:
                await s.close()
        c = _root(server); cur = c.cursor(); cur.execute("DROP USER IF EXISTS 'itest_sha2'@'%'"); cur.close(); c.close()
    asyncio.run(go())

