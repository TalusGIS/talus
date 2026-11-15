"""The dialect seam (decision-sql-source-pull.md §3, s1).

ONE composed-SELECT design; everything a database differs on lives behind `Dialect`:
the driver and how it connects (pinned address, TLS, read-only session), the catalog reader
(tables, views, columns, single-column keys), identifier quoting, the geometry expression
(a later slice) and the type map onto Talus field types.

Rules every dialect keeps (the Security section):
  - identifiers come from the catalog and are rendered ONLY through `quote_ident`; no caller
    text ever reaches the SQL string — the SELECT is composed from a closed grammar;
  - the session is read-only where the database can express it, and Talus itself only ever
    issues the composed SELECT and the catalog/count queries;
  - every error that leaves this module is a `SourceError` with a stable `reason` and an
    admin-safe `message` — never the driver's own text (a server banner, a resolved address,
    a path or a credential must never reach a client; the log gets host-only lines).

Why pure-Python drivers: the project's licence check is an allowlist of dependency licences.
psycopg is the accepted exception; pytds (MIT), PyMySQL (MIT) and python-oracledb thin
(Apache-2.0) need none.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, AsyncIterator, Protocol

import structlog

log = structlog.get_logger(__name__)

CONNECT_TIMEOUT_S = 5
STATEMENT_TIMEOUT_MS = 10 * 60 * 1000   # one SELECT over a 100k-row table is seconds; a minute is already wrong
CATALOG_MAX_TABLES = 2000
STREAM_BATCH = 2500

DIALECT_NAMES = ("postgresql", "sqlserver", "mysql", "oracle")
TLS_MODES = ("verify-full", "require", "disable")
TEMPORAL_TYPES = ("timestamp", "timestamptz", "time", "timetz")


class SourceError(Exception):
    """A failure talking to the source. `reason` is a stable code; `message` is safe to show an
    admin — fixed sentences, never the driver's text. Callers map it to 422/502 as they see fit."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ConnInfo:
    host: str
    port: int
    database: str
    username: str
    password: str | None
    tls_mode: str = "verify-full"
    ca_pem: str | None = None
    timezone: str = "UTC"        # IANA name: naive timestamps are read in this zone (F9)


@dataclass(frozen=True)
class TableInfo:
    schema: str
    name: str
    kind: str                     # 'table' | 'view'
    key_columns: tuple[str, ...]  # single-column PK / UNIQUE candidates (F10)


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    source_type: str              # the database's own type name, for the admin
    data_type: str | None         # Talus field type, or None = unsupported in this slice
    numeric_subtype: str | None = None
    temporal: bool = False        # timestamp-ish → text ISO 8601 (F9); offers the per-column UTC switch
    unsupported_reason: str | None = None


@dataclass
class SourceCounts:
    total: int
    non_null: dict[str, int] = field(default_factory=dict)


class SourceSession(Protocol):
    """One open, read-only connection to a source. Always `close()`d by the caller."""

    async def ping(self) -> None: ...
    async def catalog_tables(self) -> list[TableInfo]: ...
    async def catalog_columns(self, schema: str, table: str) -> list[ColumnInfo]: ...
    async def counts(self, schema: str, table: str, columns: list[str],
                     located: tuple[str, str] | None = None) -> SourceCounts: ...
    def stream(self, schema: str, table: str, columns: list[str]) -> AsyncIterator[tuple]: ...
    async def close(self) -> None: ...


class Dialect(Protocol):
    name: str
    default_port: int
    available: bool
    unavailable_reason: str | None

    async def open(self, info: ConnInfo, pinned_ip: str) -> SourceSession: ...
    def map_type(self, source_type: str) -> ColumnInfo: ...


# ---------------------------------------------------------------------------------------------
# Value coercion — shared by every dialect (the driver gives Python values; Talus props are JSON)
# ---------------------------------------------------------------------------------------------
def coerce_value(v: Any, *, data_type: str, temporal: bool, utc: bool, zone: _dt.tzinfo) -> Any:
    """A source value → the JSON value the Talus field validator accepts. Never raises for a
    value: anything that cannot be represented honestly becomes None (and the per-column
    non-null count check in the run report catches a column that loses everything)."""
    if v is None:
        return None
    if isinstance(v, memoryview | bytes | bytearray):
        if data_type == "boolean" and len(v) == 1:    # a MySQL BIT(1) arrives as one byte (s4)
            return bool(bytes(v)[0])
        return None                                   # binary never maps (unsupported columns aren't selected)
    if data_type == "boolean":
        if isinstance(v, bool):
            return v
        if isinstance(v, int | float | Decimal):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("t", "true", "1", "y", "yes"):
                return True
            if s in ("f", "false", "0", "n", "no", ""):
                return False
        return None
    if data_type == "number":
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, int):
            return v
        if isinstance(v, Decimal):
            if not v.is_finite():
                return None
            return int(v) if v == v.to_integral_value() else float(v)   # float64: fine for money/measurements
        if isinstance(v, float):
            return v if math.isfinite(v) else None
        if isinstance(v, str):
            try:
                f = float(v)
                return f if math.isfinite(f) else None
            except ValueError:
                return None
        return None
    if data_type == "date":
        if isinstance(v, _dt.datetime):
            return v.date().isoformat()                # an explicit admin override truncates — never silent
        if isinstance(v, _dt.date):
            return v.isoformat()
        return str(v)
    # text
    if temporal or isinstance(v, _dt.datetime | _dt.date | _dt.time):
        if isinstance(v, _dt.datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=_dt.timezone.utc if utc else zone)
            return v.isoformat()
        if isinstance(v, _dt.time):
            if v.tzinfo is None and utc:
                v = v.replace(tzinfo=_dt.timezone.utc)
            return v.isoformat()
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v).lower()
    if isinstance(v, dict | list):
        return json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, Decimal | int | float | bool):
        return str(v)
    return str(v)


# ---------------------------------------------------------------------------------------------
# PostgreSQL — the first dialect (psycopg, already in the image)
# ---------------------------------------------------------------------------------------------
_PG_TEXT = {"text", "varchar", "bpchar", "char", "name", "citext", "uuid", "json", "jsonb", "xml",
            "inet", "cidr", "macaddr", "macaddr8", "interval", "tsvector", "bit", "varbit", "hstore", "ltree"}
# Talus numeric subtypes (V051): short_integer · long_integer · float · double.
# ★ Talus `long_integer` is Esri's "long" — 32-bit (V051 validator: -2147483648..2147483647). A 64-bit
# integer maps to `double` (a JSON number; exact to 2^53, §5 notes the loss above) — not to long_integer,
# which refused every bigint row (SQL Server e2e, 2026-10-01).
_PG_INT = {"int2": "short_integer", "int4": "long_integer", "int8": "double", "oid": "double", "xid": "double"}
_PG_DEC = {"numeric": "double", "float4": "float", "float8": "double", "money": "double"}
_PG_UNSUPPORTED = {
    "bytea": "binary data is not pulled",
    "geometry": "a shape column — geometry arrives in a later slice (s4)",
    "geography": "a shape column — geometry arrives in a later slice (s4)",
    "raster": "raster data is not pulled",
}
_IDENT_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def is_ip_literal_host(host: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def pg_quote_ident(name: str) -> str:
    """Double-quote an identifier from the catalog. The name is still validated upstream against
    the live catalog; quoting is the second wall (a `"` inside the name is doubled)."""
    if "\x00" in name or len(name) > 63:
        raise SourceError("bad_identifier", "an identifier from the catalog could not be used")
    return '"' + name.replace('"', '""') + '"'


def pg_map_type(udt: str, data_type_text: str = "") -> ColumnInfo:
    """information_schema `udt_name` (+ `data_type` for arrays) → a Talus column mapping (§5)."""
    u = (udt or "").lower()
    shown = data_type_text or udt
    if u in _PG_UNSUPPORTED:
        return ColumnInfo("", shown, None, unsupported_reason=_PG_UNSUPPORTED[u])
    if u.startswith("_") or data_type_text.upper() == "ARRAY":
        return ColumnInfo("", shown, "text")           # arrays → JSON text
    if u in _PG_INT:
        return ColumnInfo("", shown, "number", _PG_INT[u])
    if u in _PG_DEC:
        return ColumnInfo("", shown, "number", _PG_DEC[u])
    if u == "bool":
        return ColumnInfo("", shown, "boolean")
    if u == "date":
        return ColumnInfo("", shown, "date")
    if u in TEMPORAL_TYPES:
        return ColumnInfo("", shown, "text", temporal=True)
    if u in _PG_TEXT:
        return ColumnInfo("", shown, "text")
    # Enums, domains, composites, anything else: a readable text rendering (str()).
    return ColumnInfo("", shown, "text")


def _pg_sslmode(info: ConnInfo) -> dict[str, str]:
    if info.tls_mode == "disable":
        return {"sslmode": "disable"}
    if info.tls_mode == "require":
        return {"sslmode": "require"}
    # verify-full: an organization CA bundle if given, else the OS trust store (libpq ≥ 16: sslrootcert=system)
    return {"sslmode": "verify-full", "sslrootcert": "system"}


class _PgSession:
    def __init__(self, conn, info: ConnInfo, ca_file: str | None):
        self._conn = conn
        self._info = info
        self._ca_file = ca_file

    async def ping(self) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute("SELECT 1")
            await cur.fetchone()

    async def catalog_tables(self) -> list[TableInfo]:
        async with self._conn.cursor() as cur:
            await cur.execute(
                "SELECT table_schema, table_name, table_type FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'tiger', 'tiger_data', 'topology') "
                "  AND table_schema NOT LIKE 'pg_toast%%' AND table_type IN ('BASE TABLE', 'VIEW') "
                "  AND NOT (table_schema = 'public' AND table_name IN "
                "           ('spatial_ref_sys', 'geometry_columns', 'geography_columns', 'raster_columns', 'raster_overviews')) "
                "ORDER BY table_schema, table_name LIMIT %s",
                (CATALOG_MAX_TABLES + 1,),
            )
            rows = await cur.fetchall()
            # ★ Keys come from pg_index, NOT information_schema.table_constraints: that view shows
            # constraints only to a login that OWNS the table or holds a privilege other than SELECT —
            # so the recommended read-only login (the e2e proof, 2026-10-01) saw no keys at all.
            # Single-column, non-partial, non-expression unique indexes (a PK or UNIQUE constraint
            # always has one) — F10: a composite key cannot be ONE diff key.
            await cur.execute(
                "SELECT n.nspname, c.relname, a.attname "
                "FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = i.indkey[0] "
                "WHERE i.indisunique AND i.indisvalid AND i.indnkeyatts = 1 AND i.indkey[0] <> 0 "
                "  AND i.indpred IS NULL AND NOT a.attisdropped "
                "  AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY 1, 2, i.indisprimary DESC, 3",
            )
            krows = await cur.fetchall()
        keys: dict[tuple[str, str], list[str]] = {}
        for s, t, col in krows:
            if col not in keys.setdefault((s, t), []):
                keys[(s, t)].append(col)
        if len(rows) > CATALOG_MAX_TABLES:
            raise SourceError("catalog_too_large",
                              f"the database has more than {CATALOG_MAX_TABLES} tables and views; ask the DBA for a view-only schema")
        return [TableInfo(s, t, "view" if k == "VIEW" else "table", tuple(keys.get((s, t), ())))
                for s, t, k in rows]

    async def catalog_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        async with self._conn.cursor() as cur:
            await cur.execute(
                "SELECT column_name, data_type, udt_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (schema, table),
            )
            rows = await cur.fetchall()
        if not rows:
            raise SourceError("table_missing", "that table or view is not in the database catalog (renamed or dropped?)")
        out: list[ColumnInfo] = []
        for name, dtt, udt in rows:
            m = pg_map_type(udt, dtt)
            out.append(ColumnInfo(name, m.source_type, m.data_type, m.numeric_subtype, m.temporal, m.unsupported_reason))
        return out

    def _from(self, schema: str, table: str) -> str:
        return f"{pg_quote_ident(schema)}.{pg_quote_ident(table)}"

    async def counts(self, schema: str, table: str, columns: list[str],
                     located: tuple[str, str] | None = None) -> SourceCounts:
        """Row and per-column non-null counts. With `located` = (x, y), counts only the rows whose
        coordinate pair is non-null — the rows an x/y pull can place — so the run's count guard
        compares like with like (s2)."""
        cols = ", ".join(f"count({pg_quote_ident(c)})" for c in columns)
        where = (f" WHERE {pg_quote_ident(located[0])} IS NOT NULL AND {pg_quote_ident(located[1])} IS NOT NULL"
                 if located else "")
        sql = f"SELECT count(*){', ' + cols if cols else ''} FROM {self._from(schema, table)}{where}"
        async with self._conn.cursor() as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
        return SourceCounts(int(row[0]), {c: int(n) for c, n in zip(columns, row[1:])})

    async def stream(self, schema: str, table: str, columns: list[str]) -> AsyncIterator[tuple]:
        sql = f"SELECT {', '.join(pg_quote_ident(c) for c in columns)} FROM {self._from(schema, table)}"
        # A named (server-side) cursor streams: flat memory at county scale (the 9d discipline).
        async with self._conn.cursor(name="talus_sql_pull") as cur:
            cur.itersize = STREAM_BATCH
            await cur.execute(sql)
            async for row in cur:
                yield row

    async def close(self) -> None:
        try:
            await self._conn.close()
        finally:
            if self._ca_file:
                try:
                    os.unlink(self._ca_file)
                except OSError:
                    pass


def _pg_error(exc: BaseException, info: ConnInfo) -> SourceError:
    """psycopg exception → a SourceError with a fixed sentence. The driver's text NEVER crosses:
    it can carry the resolved address, the server version banner, or a path."""
    import psycopg

    state = getattr(exc, "sqlstate", None) or ""
    text = str(exc).lower()
    log.warning("sql_source_error", host=info.host, port=info.port, dialect="postgresql",
                exc=type(exc).__name__, sqlstate=state or None)
    if state in ("28P01", "28000"):
        return SourceError("auth_failed", "the database refused the user name or password")
    if state == "3D000":
        return SourceError("database_missing", "that database does not exist on the server")
    if state in ("42501",):
        return SourceError("permission_denied", "the login lacks permission on that table or schema (ask the DBA for SELECT)")
    if state == "42P01":
        return SourceError("table_missing", "that table or view is not in the database catalog (renamed or dropped?)")
    if state == "42703":
        return SourceError("column_missing", "a column in this pull no longer exists in the source (renamed or dropped?)")
    if state == "57014":
        return SourceError("timeout", "the source query ran past the time limit")
    if state.startswith("25") and "read-only" in text:
        return SourceError("read_only", "the session is read-only by design; nothing is ever written to a source")
    if isinstance(exc, psycopg.OperationalError):
        if "ssl" in text or "certificate" in text or "tls" in text:
            return SourceError("tls_failed", "the TLS handshake failed — the server certificate is not trusted by this TLS mode (add your organization's CA bundle, or change the mode)")
        return SourceError("connect_failed", f"could not reach {info.host}:{info.port} (refused, timed out, or the port is wrong)")
    return SourceError("query_failed", f"the source database returned an error ({type(exc).__name__})")


class PostgresDialect:
    name = "postgresql"
    default_port = 5432
    available = True
    unavailable_reason = None

    def map_type(self, source_type: str) -> ColumnInfo:
        return pg_map_type(source_type)

    async def open(self, info: ConnInfo, pinned_ip: str) -> SourceSession:
        import psycopg

        ca_file: str | None = None
        kw: dict[str, Any] = {
            "host": info.host,          # the NAME: verify-full checks the certificate against it
            "hostaddr": pinned_ip,      # the socket goes to the vetted address (no second DNS answer)
            "port": info.port,
            "dbname": info.database,
            "user": info.username,
            "password": info.password or "",
            "connect_timeout": CONNECT_TIMEOUT_S,
            "application_name": "talus-sql-pull",
            # Read-only by the session's own rules, before any login privilege is relied on.
            "options": (f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS} "
                        f"-c idle_in_transaction_session_timeout={STATEMENT_TIMEOUT_MS}"),
        }
        kw.update(_pg_sslmode(info))
        if info.tls_mode == "verify-full" and info.ca_pem:
            fd, ca_file = tempfile.mkstemp(prefix="talus-ca-", suffix=".pem")
            with os.fdopen(fd, "w") as fh:
                fh.write(info.ca_pem)
            kw["sslrootcert"] = ca_file
        try:
            conn = await psycopg.AsyncConnection.connect(**kw)
        except (psycopg.Error, OSError) as exc:
            if ca_file:
                try:
                    os.unlink(ca_file)
                except OSError:
                    pass
            raise _pg_error(exc, info) from None
        await conn.set_autocommit(False)
        return _PgSessionGuarded(conn, info, ca_file)


class _PgSessionGuarded(_PgSession):
    """Every session call translates driver errors through `_pg_error` so the SourceError contract
    holds for catalog, counts and stream alike."""

    async def ping(self) -> None:
        try:
            await super().ping()
        except Exception as exc:  # noqa: BLE001
            raise _pg_error(exc, self._info) from None

    async def catalog_tables(self) -> list[TableInfo]:
        try:
            return await super().catalog_tables()
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _pg_error(exc, self._info) from None

    async def catalog_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        try:
            return await super().catalog_columns(schema, table)
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _pg_error(exc, self._info) from None

    async def counts(self, schema: str, table: str, columns: list[str],
                     located: tuple[str, str] | None = None) -> SourceCounts:
        try:
            return await super().counts(schema, table, columns, located)
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _pg_error(exc, self._info) from None

    async def stream(self, schema: str, table: str, columns: list[str]) -> AsyncIterator[tuple]:
        try:
            async for row in super().stream(schema, table, columns):
                yield row
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _pg_error(exc, self._info) from None


# ---------------------------------------------------------------------------------------------
# SQL Server — the second dialect (python-tds, pure Python; spike 2026-10-01: 109k rows/s, flat memory)
# ---------------------------------------------------------------------------------------------
# What the spike settled (decision-sql-source-pull.md §8):
#   - pytds is SYNCHRONOUS: every call runs in a worker thread (`asyncio.to_thread`); the stream pulls
#     `fetchmany(STREAM_BATCH)` per thread hop, so the event loop never blocks on the wire.
#   - host pinning: pytds takes a pre-connected `sock`, so the socket goes to the guard's vetted address
#     while `server=<name>` drives the certificate's host-name check — the hostaddr/host split, again.
#   - TLS exists in pytds only with a `cafile` (VERIFY_PEER against it, via pyOpenSSL). verify-full =
#     the organization's CA bundle or the OS bundle; a self-signed server certificate (the common municipal
#     "Force Encryption" setup) is its OWN CA — paste the server certificate as the bundle. `require`
#     cannot be expressed as "encrypt but don't verify" in pytds, so it behaves as verify-full against
#     the OS bundle + the pasted bundle; `disable` = no TLS.
#   - naive `datetime2`/`datetime` arrive naive when `use_tz` is NOT passed (the F9 zone is applied by
#     `coerce_value`); `datetimeoffset` arrives aware; `money` → Decimal; `uniqueidentifier` → UUID.
#   - keys come from sys.indexes (single-column, unique, unfiltered), visible to a SELECT-only login.
#   - `geometry`/`geography` are NOT WKB on the wire (Microsoft's CLR serialisation): s4 composes
#     `.STAsBinary()` + `.STSrid`; in this slice shape columns are unsupported, like PostGIS's.
_MS_TEXT = {"char", "varchar", "nchar", "nvarchar", "text", "ntext", "xml", "uniqueidentifier", "sysname"}
_MS_INT = {"tinyint": "short_integer", "smallint": "short_integer", "int": "long_integer", "bigint": "double"}   # bigint: see _PG_INT
_MS_DEC = {"decimal": "double", "numeric": "double", "money": "double", "smallmoney": "double", "float": "double", "real": "float"}
_MS_TEMPORAL = {"datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}
_MS_UNSUPPORTED = {
    "varbinary": "binary data is not pulled", "binary": "binary data is not pulled", "image": "binary data is not pulled",
    "timestamp": "a row-version stamp, not data", "rowversion": "a row-version stamp, not data",
    "sql_variant": "a variant column has no single type", "hierarchyid": "hierarchyid is not pulled",
    "geometry": "a shape column — geometry arrives in a later slice (s4)",
    "geography": "a shape column — geometry arrives in a later slice (s4)",
}
_MS_SYSTEM_SCHEMAS = ("sys", "INFORMATION_SCHEMA", "guest", "db_owner", "db_accessadmin", "db_securityadmin",
                      "db_ddladmin", "db_backupoperator", "db_datareader", "db_datawriter", "db_denydatareader",
                      "db_denydatawriter")
_OS_CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")


def ms_quote_ident(name: str) -> str:
    """Bracket-quote a SQL Server identifier from the catalog (`]` doubled); the second wall after the
    live-catalog match."""
    if "\x00" in name or len(name) > 128:
        raise SourceError("bad_identifier", "an identifier from the catalog could not be used")
    return "[" + name.replace("]", "]]") + "]"


def ms_map_type(data_type: str) -> ColumnInfo:
    """INFORMATION_SCHEMA.COLUMNS `DATA_TYPE` → a Talus column mapping (§5)."""
    u = (data_type or "").lower()
    if u in _MS_UNSUPPORTED:
        return ColumnInfo("", data_type, None, unsupported_reason=_MS_UNSUPPORTED[u])
    if u in _MS_INT:
        return ColumnInfo("", data_type, "number", _MS_INT[u])
    if u in _MS_DEC:
        return ColumnInfo("", data_type, "number", _MS_DEC[u])
    if u == "bit":
        return ColumnInfo("", data_type, "boolean")
    if u == "date":
        return ColumnInfo("", data_type, "date")
    if u in _MS_TEMPORAL:
        return ColumnInfo("", data_type, "text", temporal=True)
    if u in _MS_TEXT:
        return ColumnInfo("", data_type, "text")
    return ColumnInfo("", data_type, "text")        # user-defined / alias types: a readable text rendering


def _os_ca_bundle() -> str | None:
    for p in _OS_CA_BUNDLES:
        if os.path.exists(p):
            return p
    try:
        import certifi
        return certifi.where()
    except Exception:  # noqa: BLE001
        return None


def _ms_error(exc: BaseException, info: ConnInfo) -> SourceError:
    """pytds exception → a SourceError with a fixed sentence. The driver's text never crosses."""
    import pytds
    number = int(getattr(exc, "number", 0) or 0)
    text = str(exc).lower()
    log.warning("sql_source_error", host=info.host, port=info.port, dialect="sqlserver",
                exc=type(exc).__name__, number=number or None)
    if "routing refused" in text:
        return SourceError("address_denied", "the server asked to redirect the connection to another host; Talus follows no redirects — point the connection at that host directly")
    if isinstance(exc, pytds.LoginError) or number in (18456, 18452) or "login failed" in text:
        return SourceError("auth_failed", "the database refused the user name or password")
    if number == 4060 or "cannot open database" in text:
        return SourceError("database_missing", "that database does not exist on the server, or the login may not open it")
    if number in (229, 230, 297):
        return SourceError("permission_denied", "the login lacks permission on that table or schema (ask the DBA for SELECT)")
    if number == 208:
        return SourceError("table_missing", "that table or view is not in the database catalog (renamed or dropped?)")
    if number == 207:
        return SourceError("column_missing", "a column in this pull no longer exists in the source (renamed or dropped?)")
    if number in (3906, 3930) or "read-only" in text:
        return SourceError("read_only", "the session is read-only by design; nothing is ever written to a source")
    if "certificate" in text or "tls" in text or "ssl" in text or "handshake" in text:
        return SourceError("tls_failed", "the TLS handshake failed — the server certificate is not trusted by this TLS mode (paste the server's or your organization's CA certificate, or change the mode)")
    if isinstance(exc, (TimeoutError, OSError, ConnectionError)) or "timed out" in text or "refused" in text or "closed" in text:
        return SourceError("connect_failed", f"could not reach {info.host}:{info.port} (refused, timed out, or the port is wrong)")
    if "queryexecution" in text or number == 1205:
        return SourceError("query_failed", f"the source database returned an error ({type(exc).__name__})")
    return SourceError("query_failed", f"the source database returned an error ({type(exc).__name__})")


class _MsSession:
    def __init__(self, conn, info: ConnInfo, ca_file: str | None):
        self._conn = conn
        self._info = info
        self._ca_file = ca_file

    def _q(self, sql: str, params: tuple | None = None) -> list[tuple]:
        """`params=None` runs a plain query: pytds %-formats the SQL when ANY params object is given,
        so a quoted catalog name containing '%' must never travel with an empty tuple."""
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params if params else None)
            return [tuple(r) for r in cur.fetchall()]
        finally:
            cur.close()

    async def _run(self, fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _ms_error(exc, self._info) from None

    async def ping(self) -> None:
        await self._run(self._q, "SELECT 1")

    def _catalog_tables_sync(self) -> list[TableInfo]:
        rows = self._q(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') AND TABLE_SCHEMA NOT IN (" + ", ".join("%s" for _ in _MS_SYSTEM_SCHEMAS) + ") "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME",
            tuple(_MS_SYSTEM_SCHEMAS),
        )
        if len(rows) > CATALOG_MAX_TABLES:
            raise SourceError("catalog_too_large",
                              f"the database has more than {CATALOG_MAX_TABLES} tables and views; ask the DBA for a view-only schema")
        # Single-column unique indexes (a PK or UNIQUE constraint always has one), unfiltered — from
        # sys.indexes, which a SELECT-only login can read (the spike, 2026-10-01).
        krows = self._q(
            "SELECT s.name, t.name, c.name FROM sys.indexes i "
            "JOIN sys.tables t ON t.object_id = i.object_id JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.key_ordinal = 1 "
            "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
            "WHERE i.is_unique = 1 AND i.filter_definition IS NULL "
            "  AND (SELECT COUNT(*) FROM sys.index_columns k WHERE k.object_id = i.object_id AND k.index_id = i.index_id AND k.key_ordinal > 0) = 1 "
            "ORDER BY 1, 2, i.is_primary_key DESC, 3"
        )
        keys: dict[tuple[str, str], list[str]] = {}
        for sch, tab, col in krows:
            if col not in keys.setdefault((sch, tab), []):
                keys[(sch, tab)].append(col)
        return [TableInfo(sch, tab, "view" if k == "VIEW" else "table", tuple(keys.get((sch, tab), ())))
                for sch, tab, k in rows]

    async def catalog_tables(self) -> list[TableInfo]:
        return await self._run(self._catalog_tables_sync)

    def _catalog_columns_sync(self, schema: str, table: str) -> list[ColumnInfo]:
        rows = self._q("SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                       "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION", (schema, table))
        if not rows:
            raise SourceError("table_missing", "that table or view is not in the database catalog (renamed or dropped?)")
        out: list[ColumnInfo] = []
        for name, dtt in rows:
            m = ms_map_type(dtt)
            out.append(ColumnInfo(name, m.source_type, m.data_type, m.numeric_subtype, m.temporal, m.unsupported_reason))
        return out

    async def catalog_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        return await self._run(self._catalog_columns_sync, schema, table)

    def _from(self, schema: str, table: str) -> str:
        return f"{ms_quote_ident(schema)}.{ms_quote_ident(table)}"

    def _counts_sync(self, schema: str, table: str, columns: list[str], located) -> SourceCounts:
        cols = ", ".join(f"COUNT({ms_quote_ident(c)})" for c in columns)
        where = (f" WHERE {ms_quote_ident(located[0])} IS NOT NULL AND {ms_quote_ident(located[1])} IS NOT NULL"
                 if located else "")
        row = self._q(f"SELECT COUNT_BIG(*){', ' + cols if cols else ''} FROM {self._from(schema, table)}{where}")[0]
        return SourceCounts(int(row[0]), {c: int(n) for c, n in zip(columns, row[1:])})

    async def counts(self, schema: str, table: str, columns: list[str],
                     located: tuple[str, str] | None = None) -> SourceCounts:
        return await self._run(self._counts_sync, schema, table, columns, located)

    async def stream(self, schema: str, table: str, columns: list[str]) -> AsyncIterator[tuple]:
        sql = f"SELECT {', '.join(ms_quote_ident(c) for c in columns)} FROM {self._from(schema, table)}"
        cur = self._conn.cursor()
        try:
            await self._run(cur.execute, sql)
            while True:
                rows = await self._run(cur.fetchmany, STREAM_BATCH)
                if not rows:
                    break
                for row in rows:
                    yield tuple(row)
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        try:
            await asyncio.to_thread(self._conn.close)
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self._ca_file:
                try:
                    os.unlink(self._ca_file)
                except OSError:
                    pass


class SqlServerDialect:
    name = "sqlserver"
    default_port = 1433
    available = True
    unavailable_reason = None

    def map_type(self, source_type: str) -> ColumnInfo:
        return ms_map_type(source_type)

    async def open(self, info: ConnInfo, pinned_ip: str) -> SourceSession:
        import socket

        import pytds

        from . import tds_tls
        tds_tls.install_routing_guard()      # every open, TLS or not: no server-directed reconnects
        ca_file: str | None = None
        cafile: str | None = None
        validate = True
        if info.tls_mode == "disable":
            cafile = None
        else:
            # TLS rides the standard-library channel (tds_tls.py — F4 addendum, RULED C): pytds's own path
            # needs pyOpenSSL, which cannot install beside this build's `cryptography` pin.
            tds_tls.install()
            if info.ca_pem:
                fd, ca_file = tempfile.mkstemp(prefix="talus-ca-", suffix=".pem")
                with os.fdopen(fd, "w") as fh:
                    fh.write(info.ca_pem)
                cafile = ca_file
            else:
                cafile = _os_ca_bundle()
            if cafile is None:
                raise SourceError("tls_failed", "no CA bundle is available for TLS on this server — paste the server's or your organization's CA certificate")
            # TLS mode `require` = encrypted; pytds always verifies the chain against the bundle (see above);
            # the host-name check is the part `require` relaxes.
            validate = info.tls_mode == "verify-full"

        def _connect():
            # The socket goes to the VETTED address (the pin); pytds sees the NAME for the certificate check.
            sock = socket.create_connection((pinned_ip, info.port), timeout=CONNECT_TIMEOUT_S)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                return pytds.connect(
                    server=info.host, port=info.port, database=info.database, user=info.username,
                    password=info.password or "", sock=sock, cafile=cafile, validate_host=validate,
                    login_timeout=CONNECT_TIMEOUT_S, timeout=STATEMENT_TIMEOUT_MS // 1000,
                    appname="talus-sql-pull", readonly=True,      # ApplicationIntent=ReadOnly (availability groups)
                    autocommit=True, disable_connect_retry=True, use_mars=False, pooling=False,
                )
            except BaseException:
                try:
                    sock.close()
                except OSError:
                    pass
                raise

        try:
            conn = await asyncio.to_thread(_connect)
        except Exception as exc:  # noqa: BLE001
            if ca_file:
                try:
                    os.unlink(ca_file)
                except OSError:
                    pass
            raise _ms_error(exc, info) from None
        return _MsSession(conn, info, ca_file)


# ---------------------------------------------------------------------------------------------
# MySQL / MariaDB (s4, decision §14) — PyMySQL (MIT, pure Python), threaded like SQL Server. One dialect
# serves both servers; `VERSION()` decides only where they differ (the statement-time variable).
# ---------------------------------------------------------------------------------------------
_MY_TEXT = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext", "enum", "set", "json"}
_MY_INT = {"tinyint": "short_integer", "smallint": "short_integer", "mediumint": "long_integer",
           "int": "long_integer", "integer": "long_integer", "bigint": "double"}           # bigint: see _PG_INT
_MY_INT_UNSIGNED = {"tinyint": "short_integer", "smallint": "long_integer", "mediumint": "long_integer",
                    "int": "double", "integer": "double", "bigint": "double"}            # unsigned int > 32-bit
_MY_DEC = {"decimal": "double", "numeric": "double", "dec": "double", "fixed": "double",
           "float": "float", "double": "double", "real": "double", "double precision": "double"}
_MY_TEMPORAL = {"datetime", "timestamp", "time"}
_MY_UNSUPPORTED = {
    "binary": "binary data is not pulled", "varbinary": "binary data is not pulled",
    "tinyblob": "binary data is not pulled", "blob": "binary data is not pulled",
    "mediumblob": "binary data is not pulled", "longblob": "binary data is not pulled",
    **{g: "a shape column — geometry arrives in a later slice (s4)" for g in
       ("geometry", "point", "linestring", "polygon", "multipoint", "multilinestring", "multipolygon",
        "geometrycollection", "geomcollection")},
}
_MY_SYSTEM_SCHEMAS = ("mysql", "information_schema", "performance_schema", "sys")


def my_quote_ident(name: str) -> str:
    """Back-tick an identifier from the catalog (the back-tick doubled); 64 is MySQL's own limit."""
    if "\x00" in name or len(name) > 64:
        raise SourceError("bad_identifier", "an identifier from the catalog could not be used")
    return "`" + name.replace("`", "``") + "`"


def my_map_type(data_type: str, column_type: str = "") -> ColumnInfo:
    """information_schema.COLUMNS `DATA_TYPE` (+ `COLUMN_TYPE` for unsigned / tinyint(1) / bit(1)) → a Talus mapping."""
    u = (data_type or "").lower()
    ct = (column_type or "").lower()
    if u in _MY_UNSUPPORTED:
        return ColumnInfo("", data_type, None, unsupported_reason=_MY_UNSUPPORTED[u])
    if u == "tinyint" and ct.startswith("tinyint(1)"):
        return ColumnInfo("", column_type or data_type, "boolean")
    if u in _MY_INT:
        sub = _MY_INT_UNSIGNED[u] if "unsigned" in ct else _MY_INT[u]
        return ColumnInfo("", column_type or data_type, "number", sub)
    if u in _MY_DEC:
        return ColumnInfo("", column_type or data_type, "number", _MY_DEC[u])
    if u == "bit":
        if ct in ("bit(1)", "bit"):
            return ColumnInfo("", column_type or data_type, "boolean")
        return ColumnInfo("", column_type or data_type, None, unsupported_reason="a bit field wider than one bit is not pulled")
    if u == "year":
        return ColumnInfo("", data_type, "number", "short_integer")
    if u == "date":
        return ColumnInfo("", data_type, "date")
    if u in _MY_TEMPORAL:
        return ColumnInfo("", data_type, "text", temporal=True)
    if u in _MY_TEXT:
        return ColumnInfo("", column_type if u in ("enum", "set") else data_type, "text")
    return ColumnInfo("", data_type, "text")        # anything else: a readable text rendering


def _my_error(exc: BaseException, info: ConnInfo) -> SourceError:
    """PyMySQL exception → a SourceError with a fixed sentence. The driver's text (which carries the
    client address and, for 1045, the user name) never crosses."""
    import ssl as _ssl

    import pymysql
    code = 0
    if isinstance(exc, pymysql.MySQLError) and exc.args and isinstance(exc.args[0], int):
        code = int(exc.args[0])
    text = str(exc).lower()
    log.warning("sql_source_error", host=info.host, port=info.port, dialect="mysql",
                exc=type(exc).__name__, number=code or None)
    if not isinstance(exc, (pymysql.MySQLError, OSError, TimeoutError, ConnectionError)):
        # Not the driver's and not the network's: a defect in this module. Its text is ours, so the
        # traceback may be logged (the SourceError that crosses stays the fixed sentence).
        log.error("sql_source_internal_error", host=info.host, dialect="mysql", exc_info=exc)
    if isinstance(exc, _ssl.SSLError) or "ssl" in text or "certificate" in text or "handshake" in text:
        return SourceError("tls_failed", "the TLS handshake failed — the server certificate is not trusted by this TLS mode (paste the server's or your organization's CA certificate, or change the mode)")
    if code == 3159:
        return SourceError("tls_failed", "the server requires an encrypted connection — choose a TLS mode other than disable")
    if code == 1045:
        return SourceError("auth_failed", "the database refused the user name or password")
    if code in (1044, 1049):
        return SourceError("database_missing", "that database does not exist on the server, or the login may not open it")
    if code in (1142, 1143, 1227):
        return SourceError("permission_denied", "the login lacks permission on that table or schema (ask the DBA for SELECT)")
    if code == 1146:
        return SourceError("table_missing", "that table or view is not in the database catalog (renamed or dropped?)")
    if code == 1054:
        return SourceError("column_missing", "a column in this pull no longer exists in the source (renamed or dropped?)")
    if code in (1290, 1792) or "read only" in text or "read-only" in text:
        return SourceError("read_only", "the session is read-only by design; nothing is ever written to a source")
    if code in (3024, 1317, 1969):
        return SourceError("query_failed", "the source database stopped the query at the time bound (the table may be too large for one pull; ask the DBA for a narrower view)")
    if code in (2003, 2006, 2013) or isinstance(exc, (TimeoutError, OSError, ConnectionError)) \
            or "timed out" in text or "refused" in text or "can't connect" in text or "lost connection" in text:
        return SourceError("connect_failed", f"could not reach {info.host}:{info.port} (refused, timed out, or the port is wrong)")
    return SourceError("query_failed", f"the source database returned an error ({type(exc).__name__})")


class _MySession:
    def __init__(self, conn, info: ConnInfo, ca_file: str | None):
        self._conn = conn
        self._info = info
        self._ca_file = ca_file

    def _q(self, sql: str, params: tuple | None = None) -> list[tuple]:
        """`params=None` runs a plain query: PyMySQL %-formats the SQL when ANY args are given, so a
        quoted catalog name containing '%' must never travel with an empty tuple (the pytds rule)."""
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params if params else None)
            return [tuple(r) for r in cur.fetchall()]
        finally:
            cur.close()

    async def _run(self, fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _my_error(exc, self._info) from None

    async def ping(self) -> None:
        await self._run(self._q, "SELECT 1")

    def _catalog_tables_sync(self) -> list[TableInfo]:
        ph = ", ".join("%s" for _ in _MY_SYSTEM_SCHEMAS)
        rows = self._q(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
            "WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') AND TABLE_SCHEMA NOT IN (" + ph + ") "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME", tuple(_MY_SYSTEM_SCHEMAS))
        if len(rows) > CATALOG_MAX_TABLES:
            raise SourceError("catalog_too_large",
                              f"the database has more than {CATALOG_MAX_TABLES} tables and views; ask the DBA for a view-only schema")
        # Single-column unique indexes (PRIMARY first) from information_schema.STATISTICS — visible to a
        # SELECT-only login, which is the login this product asks for.
        krows = self._q(
            "SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX FROM information_schema.STATISTICS "
            "WHERE NON_UNIQUE = 0 AND TABLE_SCHEMA NOT IN (" + ph + ") "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME, (INDEX_NAME = 'PRIMARY') DESC, INDEX_NAME, SEQ_IN_INDEX",
            tuple(_MY_SYSTEM_SCHEMAS))
        width: dict[tuple[str, str, str], int] = {}
        first: dict[tuple[str, str, str], str] = {}
        order: list[tuple[str, str, str]] = []
        for sch, tab, idx, col, seq in krows:
            k = (sch, tab, idx)
            if k not in width:
                order.append(k)
            width[k] = max(width.get(k, 0), int(seq))
            if int(seq) == 1:
                first[k] = col
        keys: dict[tuple[str, str], list[str]] = {}
        for k in order:
            if width[k] == 1 and k in first:
                lst = keys.setdefault((k[0], k[1]), [])
                if first[k] not in lst:
                    lst.append(first[k])
        return [TableInfo(sch, tab, "view" if k == "VIEW" else "table", tuple(keys.get((sch, tab), ())))
                for sch, tab, k in rows]

    async def catalog_tables(self) -> list[TableInfo]:
        return await self._run(self._catalog_tables_sync)

    def _catalog_columns_sync(self, schema: str, table: str) -> list[ColumnInfo]:
        rows = self._q("SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE FROM information_schema.COLUMNS "
                       "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION", (schema, table))
        if not rows:
            raise SourceError("table_missing", "that table or view is not in the database catalog (renamed or dropped?)")
        out: list[ColumnInfo] = []
        for name, dtt, ctt in rows:
            m = my_map_type(dtt, ctt)
            out.append(ColumnInfo(name, m.source_type, m.data_type, m.numeric_subtype, m.temporal, m.unsupported_reason))
        return out

    async def catalog_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        return await self._run(self._catalog_columns_sync, schema, table)

    def _from(self, schema: str, table: str) -> str:
        return f"{my_quote_ident(schema)}.{my_quote_ident(table)}"

    def _counts_sync(self, schema: str, table: str, columns: list[str], located) -> SourceCounts:
        cols = ", ".join(f"COUNT({my_quote_ident(c)})" for c in columns)
        where = (f" WHERE {my_quote_ident(located[0])} IS NOT NULL AND {my_quote_ident(located[1])} IS NOT NULL"
                 if located else "")
        row = self._q(f"SELECT COUNT(*){', ' + cols if cols else ''} FROM {self._from(schema, table)}{where}")[0]
        return SourceCounts(int(row[0]), {c: int(n) for c, n in zip(columns, row[1:])})

    async def counts(self, schema: str, table: str, columns: list[str],
                     located: tuple[str, str] | None = None) -> SourceCounts:
        return await self._run(self._counts_sync, schema, table, columns, located)

    async def stream(self, schema: str, table: str, columns: list[str]) -> AsyncIterator[tuple]:
        import pymysql.cursors
        sql = f"SELECT {', '.join(my_quote_ident(c) for c in columns)} FROM {self._from(schema, table)}"
        cur = self._conn.cursor(pymysql.cursors.SSCursor)      # unbuffered: rows arrive as they are read
        try:
            await self._run(cur.execute, sql)
            while True:
                rows = await self._run(cur.fetchmany, STREAM_BATCH)
                if not rows:
                    break
                for row in rows:
                    yield tuple(row)
        finally:
            try:
                await asyncio.to_thread(cur.close)
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        try:
            await asyncio.to_thread(self._conn.close)
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self._ca_file:
                try:
                    os.unlink(self._ca_file)
                except OSError:
                    pass


class _Sha2Handler:
    """PyMySQL 1.2.3: `_auth.caching_sha2_password_auth` ends without a `return` after the RSA full-auth
    round trip (a PLAIN connection whose password the server has not cached yet — the first login after
    the account is created or its password changed), so the auth loop dereferences None and raises
    AttributeError. The loop tolerates None only from a CUSTOM handler (it breaks on the OK packet the
    round trip already read and checked). Registering the built-in AS the custom handler is the whole
    fix; it is a no-op on the cached and TLS paths, which return the packet themselves."""

    def __init__(self, con):
        self._con = con

    def authenticate(self, pkt):
        from pymysql import _auth
        return _auth.caching_sha2_password_auth(self._con, pkt)


class _RefuseCleartext:
    """PyMySQL answers an AuthSwitchRequest naming `mysql_clear_password` or `dialog` with the password in
    clear, unconditionally. A read-only pull login never needs either plugin, and a rogue or on-path server
    could ask for one — so both are refused (security review 2026-10-02, path 2)."""

    def __init__(self, con):
        self._con = con

    def authenticate(self, pkt):
        raise SourceError("auth_refused", "the server asked for the password in clear text; Talus never sends it that way "
                                          "(ask the DBA for a login that uses caching_sha2_password or mysql_native_password)")


class MySqlDialect:
    name = "mysql"
    default_port = 3306
    available = True
    unavailable_reason = None

    def map_type(self, source_type: str) -> ColumnInfo:
        return my_map_type(source_type)

    async def open(self, info: ConnInfo, pinned_ip: str) -> SourceSession:
        import socket

        import pymysql

        ca_file: str | None = None
        ssl_opt: dict[str, Any] | None = None
        ssl_disabled = False
        if info.tls_mode == "disable":
            ssl_disabled = True
        else:
            cafile: str | None = None
            if info.ca_pem:
                fd, ca_file = tempfile.mkstemp(prefix="talus-ca-", suffix=".pem")
                with os.fdopen(fd, "w") as fh:
                    fh.write(info.ca_pem)
                cafile = ca_file
            if info.tls_mode == "verify-full":
                cafile = cafile or _os_ca_bundle()
                if cafile is None:
                    raise SourceError("tls_failed", "no CA bundle is available for TLS on this server — paste the server's or your organization's CA certificate")
                ssl_opt = {"ca": cafile, "check_hostname": True, "verify_mode": "required"}
            elif cafile:
                ssl_opt = {"ca": cafile, "check_hostname": False, "verify_mode": "required"}   # require + a pasted CA: chain, not name
            else:
                ssl_opt = {"verify_mode": "none"}                 # require: encrypted, like libpq's sslmode=require

        def _connect():
            conn = pymysql.Connection(
                host=info.host,                 # the NAME: the TLS layer checks the certificate against it
                port=info.port, user=info.username, password=info.password or "", database=info.database,
                charset="utf8mb4", connect_timeout=CONNECT_TIMEOUT_S,
                read_timeout=STATEMENT_TIMEOUT_MS // 1000, write_timeout=CONNECT_TIMEOUT_S,
                autocommit=True, program_name="talus-sql-pull", defer_connect=True,
                ssl=ssl_opt, ssl_disabled=ssl_disabled, local_infile=False,
                auth_plugin_map={"caching_sha2_password": _Sha2Handler,
                                 "mysql_clear_password": _RefuseCleartext, "dialog": _RefuseCleartext},
            )
            # The socket goes to the VETTED address (the pin); the driver never resolves the name itself.
            sock = socket.create_connection((pinned_ip, info.port), timeout=CONNECT_TIMEOUT_S)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                conn.connect(sock=sock)
                cur = conn.cursor()
                try:
                    # Read-only by the session's own rules, before any login privilege is relied on.
                    cur.execute("SET SESSION TRANSACTION READ ONLY")
                    cur.execute("SELECT VERSION()")
                    ver = str((cur.fetchone() or ("",))[0]).lower()
                    try:
                        if "mariadb" in ver:
                            cur.execute("SET SESSION max_statement_time = %s", (STATEMENT_TIMEOUT_MS // 1000,))
                        else:
                            cur.execute("SET SESSION max_execution_time = %s", (STATEMENT_TIMEOUT_MS,))
                    except Exception:  # noqa: BLE001 — an older server without the variable keeps the socket timeout
                        pass
                finally:
                    cur.close()
                return conn
            except BaseException:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
                raise

        try:
            conn = await asyncio.to_thread(_connect)
        except Exception as exc:  # noqa: BLE001
            if ca_file:
                try:
                    os.unlink(ca_file)
                except OSError:
                    pass
            if isinstance(exc, SourceError):          # the refusing auth handler speaks for itself
                raise
            raise _my_error(exc, info) from None
        return _MySession(conn, info, ca_file)


class _SlotDialect:
    """A dialect the schema admits (the CHECK names all four) but this release does not ship."""
    available = False

    def __init__(self, name: str, default_port: int, reason: str):
        self.name = name
        self.default_port = default_port
        self.unavailable_reason = reason

    def map_type(self, source_type: str) -> ColumnInfo:
        raise SourceError("dialect_unavailable", self.unavailable_reason)

    async def open(self, info: ConnInfo, pinned_ip: str) -> SourceSession:
        raise SourceError("dialect_unavailable", self.unavailable_reason)


DIALECTS: dict[str, Dialect] = {
    "postgresql": PostgresDialect(),
    "sqlserver": SqlServerDialect(),
    "mysql": MySqlDialect(),
    "oracle": _SlotDialect("oracle", 1521, "Oracle is a later slice (python-oracledb thin mode)"),
}
