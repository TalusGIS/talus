"""SQL-source pull — connections, catalog, pulls and runs (decision-sql-source-pull.md, s1).

The ★ question: this module holds a credential and opens
a server-side connection to a host the customer names. Who chooses where it points: a PUBLISHER —
the same trust tier that creates tables and runs bulk imports (not admin-only:
many users carry many departmental databases, and that cannot be managed
at the admin level). Connections and pulls are PER-USER: a publisher sees and uses only their own;
admins see every connection read-only (host and owner, never the password) and may delete one
(oversight without custody of other people's credentials). Can untrusted input steer it: no — the host
is judged by `vet_host` at create, at test and at EVERY run (the core guard `app.netguard` with the
private classes allowed — a City LAN is the normal destination — plus a permanent never-list of
this deployment's own services, by name AND by resolved address); table and column names come
from the remote catalog and are rendered only through the dialect's identifier quoting; the
SELECT is composed from a closed grammar. No free-text SQL exists anywhere in this feature.

Credentials: Fernet (`app.secrets`), encrypted before any SQL, write-only (`hasPassword` out,
never the value), never logged. Pulled data lands in a hosted non-spatial table owned by the admin
who made the pull, shared with no one, with editing locked (F3). A run is one transaction: the
previous copy is replaced only when the whole new copy is in and its counts match the source —
a failed run leaves the last good copy untouched (F6, trap 5.4). s1 replaces the copy whole; the
keyed / content-hash diff (F2/F10) is s3.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import time
import zoneinfo
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import structlog
from psycopg import AsyncConnection
from psycopg import errors as pg_errors
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from .. import admin_audit
from ..audit_context import set_audit_context
from ..config import settings
from ..netguard import EgressRefused, Resolver, default_resolver, resolve_host
from ..secrets import SecretDecryptError, SecretKeyUnavailable, decrypt_secret, encrypt_secret
from .dialects import (
    DIALECT_NAMES,
    DIALECTS,
    TLS_MODES,
    ColumnInfo,
    ConnInfo,
    SourceError,
    SourceSession,
    TableInfo,
    coerce_value,
)

log = structlog.get_logger(__name__)

MAX_COLUMNS = 200
MAX_CA_PEM = 64 * 1024
ERROR_SAMPLE_MAX = 50
_BATCH = 2500
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")
_IDENT_RE = re.compile(r"^[^\x00-\x1f\x7f\s]{1,128}$")

# The deployment's own services, by NAME — refused always, regardless of what they resolve to.
NEVER_HOSTNAMES = frozenset({
    "localhost", "postgres", "openfga", "keycloak", "api", "caddy", "pg-tileserv", "pg_tileserv",
    "keycloak-postgres", "openfga-postgres",
    "talusgis-postgres", "talusgis-api", "talusgis-openfga", "talusgis-keycloak", "talusgis-caddy",
    "talusgis-keycloak-postgres", "talusgis-openfga-postgres", "talusgis-pg-tileserv",
})
_UPSTREAM_REASONS = frozenset({"connect_failed", "auth_failed", "tls_failed", "database_missing",
                               "permission_denied", "timeout", "query_failed", "read_only"})


def is_upstream_reason(reason: str) -> bool:
    return reason in _UPSTREAM_REASONS


# ---------------------------------------------------------------------------------------------
# The never-list — this deployment's own services, by name and by address
# ---------------------------------------------------------------------------------------------
def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        h = urlsplit(url).hostname
    except ValueError:
        return None
    return h.lower().rstrip(".") if h else None


def stack_hostnames() -> set[str]:
    """Hostnames of the services this api itself talks to (from settings), lower-cased — the
    sibling databases Talus backs up are included on purpose (security review 2026-10-01: the
    by-address half of the never-list must cover every service, not only the four the api
    queries)."""
    out: set[str] = set()
    for u in (settings.database_url, settings.owner_database_url, settings.openfga_api_url, settings.keycloak_issuer,
              getattr(settings, "backup_keycloak_db_url", None), getattr(settings, "backup_openfga_db_url", None),
              getattr(settings, "keycloak_jwks_url", None), getattr(settings, "tileserv_url", None)):
        h = _host_of(u)
        if h:
            out.add(h)
    return out


_STACK_ADDR_CACHE: tuple[float, frozenset[str]] | None = None
_STACK_ADDR_TTL_S = 60.0


async def stack_addresses(resolver: Resolver = default_resolver) -> frozenset[str]:
    """The resolved addresses of the stack's own services, cached a minute. A name that will not
    resolve (a laptop running the api outside compose) is simply absent."""
    global _STACK_ADDR_CACHE
    now = time.monotonic()
    if _STACK_ADDR_CACHE is not None and now - _STACK_ADDR_CACHE[0] < _STACK_ADDR_TTL_S:
        return _STACK_ADDR_CACHE[1]
    addrs: set[str] = set()
    # Every name on the never-list AND every settings host: the by-address half covers exactly what
    # the by-name half covers, so a compose-bridge IP literal (or a name resolving to it) for
    # keycloak-postgres / openfga-postgres is refused like the name (security review 2026-10-01).
    for h in NEVER_HOSTNAMES | stack_hostnames():
        try:
            addrs.update(await resolver(h, 0))
        except Exception:  # noqa: BLE001 — unresolvable stack name: nothing to deny by address
            pass
    _STACK_ADDR_CACHE = (now, frozenset(addrs))
    return _STACK_ADDR_CACHE[1]


async def vet_host(host: str, port: int, *, resolver: Resolver = default_resolver) -> str:
    """The destination fence, run at create, test and EVERY run. Returns the pinned address.
    Private classes are allowed (F11: the connection row is the allow-list; a LAN database is the
    normal case); loopback, link-local, metadata, multicast, broadcast, documentation ranges and the
    deployment's own services are refused always."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        raise SourceError("address_denied", "a host is required")
    if h in NEVER_HOSTNAMES or h in stack_hostnames() or h.endswith(".localhost"):
        raise SourceError("address_denied", "that host is part of this Talus deployment and cannot be a source")
    try:
        ip = await resolve_host(h, port, resolver=resolver, allow_private=True, what="database host")
    except EgressRefused as exc:
        raise SourceError(exc.reason, str(exc)) from None
    if ip in await stack_addresses(resolver):
        raise SourceError("address_denied", "that host is part of this Talus deployment and cannot be a source")
    return ip


# ---------------------------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------------------------
def _validate_host(v: str) -> str:
    h = (v or "").strip().lower().rstrip(".")
    if not h or len(h) > 253:
        raise ValueError("host is required (a hostname or IP address, at most 253 characters)")
    import ipaddress
    try:
        ipaddress.ip_address(h)
        return h
    except ValueError:
        pass
    if any(not _LABEL_RE.match(label) for label in h.split(".")):
        raise ValueError("host must be a valid hostname (letters, digits and hyphens) or an IP address")
    return h


def _validate_timezone(v: str) -> str:
    z = (v or "UTC").strip()
    if len(z) > 64:
        raise ValueError("time zone name too long")
    try:
        zoneinfo.ZoneInfo(z)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ValueError("time zone must be an IANA name such as America/Chicago")
    return z


class ConnectionWrite(BaseModel):
    name: str
    dialect: str
    host: str
    port: int = Field(ge=1, le=65535)
    database: str
    username: str
    # Write-only credential: a value replaces the stored one; `clearPassword` removes it; neither = keep.
    password: str | None = None
    clearPassword: bool = False
    tlsMode: str = "verify-full"
    caPem: str | None = None
    clearCaBundle: bool = False
    timezone: str = "UTC"

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not _NAME_RE.match(v):
            raise ValueError("name is required (1–80 printable characters)")
        return v

    @field_validator("dialect")
    @classmethod
    def _v_dialect(cls, v: str) -> str:
        if v not in DIALECT_NAMES:
            raise ValueError(f"dialect must be one of {', '.join(DIALECT_NAMES)}")
        return v

    _v_host = field_validator("host")(staticmethod(_validate_host))

    @field_validator("database", "username")
    @classmethod
    def _v_ident(cls, v: str) -> str:
        v = (v or "").strip()
        if not _IDENT_RE.match(v):
            raise ValueError("required; 1–128 characters, no whitespace or control characters")
        return v

    @field_validator("password")
    @classmethod
    def _v_password(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if len(v) > 512 or any(ord(ch) < 0x20 for ch in v):
            raise ValueError("password may not contain control characters (max 512)")
        return v

    @field_validator("tlsMode")
    @classmethod
    def _v_tls(cls, v: str) -> str:
        if v not in TLS_MODES:
            raise ValueError(f"tlsMode must be one of {', '.join(TLS_MODES)}")
        return v

    @field_validator("caPem")
    @classmethod
    def _v_ca(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if len(v) > MAX_CA_PEM or "-----BEGIN CERTIFICATE-----" not in v:
            raise ValueError("the CA bundle must be PEM text containing at least one certificate (max 64 KiB)")
        return v.strip() + "\n"

    _v_tz = field_validator("timezone")(staticmethod(_validate_timezone))


class PullColumn(BaseModel):
    name: str
    utc: bool = False          # a naive timestamp column that is stored in UTC (F9)
    toDate: bool = False       # explicit truncation of a timestamp to a Talus `date` (F9 override)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        if not v or len(v) > 128 or "\x00" in v:
            raise ValueError("column name is required (max 128)")
        return v


class PullGeometry(BaseModel):
    """s2: build POINT geometry from two pulled coordinate columns. `srid` follows the bulk import's E4
    rule — a lon/lat-named pair detects 4326; a projected x/y pair must declare its EPSG."""
    kind: Literal["xy"] = "xy"
    x: str
    y: str
    srid: int | None = Field(default=None, ge=1, le=998999)

    @field_validator("x", "y")
    @classmethod
    def _v_col(cls, v: str) -> str:
        if not v or len(v) > 128 or "\x00" in v:
            raise ValueError("coordinate column name is required (max 128)")
        return v


SCHEDULE_KINDS = ("manual", "nightly", "interval")


class PullSchedule(BaseModel):
    """S3-B: manual · nightly at `hour` (0–23, in the connection's time zone) · every `minutes` (≥ 60)."""
    kind: Literal["manual", "nightly", "interval"] = "manual"
    hour: int | None = Field(default=None, ge=0, le=23)
    minutes: int | None = Field(default=None, ge=60, le=60 * 24 * 31)

    def normalised(self) -> "PullSchedule":
        if self.kind == "nightly":
            return PullSchedule(kind="nightly", hour=2 if self.hour is None else self.hour, minutes=None)
        if self.kind == "interval":
            return PullSchedule(kind="interval", hour=None, minutes=self.minutes or 60)
        return PullSchedule(kind="manual")


def next_run_at(schedule: PullSchedule, zone: _dt.tzinfo, now: _dt.datetime | None = None) -> _dt.datetime | None:
    """When the schedule next fires, after `now`. Nightly: the next `hour`:00 in the connection's zone (a wall-clock
    time, so DST shifts are absorbed — 02:00 Central is 02:00 Central in March and November alike; a skipped 02:00
    on the spring-forward night lands on the next valid instant). Interval: now + minutes. Manual: None."""
    sch = schedule.normalised()
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if sch.kind == "manual":
        return None
    if sch.kind == "interval":
        return now + _dt.timedelta(minutes=int(sch.minutes or 60))
    local = now.astimezone(zone)
    hour = int(sch.hour or 0)
    candidate = _dt.datetime.combine(local.date(), _dt.time(hour), tzinfo=zone)
    if candidate.astimezone(_dt.timezone.utc) <= now:
        candidate = _dt.datetime.combine(local.date() + _dt.timedelta(days=1), _dt.time(hour), tzinfo=zone)
    # A wall-clock time the zone skips (02:00 on the spring-forward night) converts to the next real instant.
    return candidate.astimezone(_dt.timezone.utc)


class PullCreate(BaseModel):
    connectionId: UUID
    schema_: str = Field(alias="schema")
    table: str
    columns: list[PullColumn] = Field(min_length=1, max_length=MAX_COLUMNS)
    keyColumn: str | None = None
    name: str
    geometry: PullGeometry | None = None     # None = a non-spatial table (s1); xy = a point layer (s2)
    schedule: PullSchedule | None = None     # S3-B; None = the wizard's default (nightly at 02:00)

    model_config = {"populate_by_name": True}

    @field_validator("schema_", "table")
    @classmethod
    def _v_ident(cls, v: str) -> str:
        if not v or len(v) > 128 or "\x00" in v:
            raise ValueError("required (max 128)")
        return v

    @field_validator("name")
    @classmethod
    def _v_label(cls, v: str) -> str:
        v = (v or "").strip()
        if not (1 <= len(v) <= 120) or any(ord(ch) < 0x20 for ch in v):
            raise ValueError("name is required (1–120 characters)")
        return v


# ---------------------------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------------------------
_CONN_COLS = ("id, name, dialect, host, port, database, username, password_ciphertext, "
              "tls_mode, ca_pem, timezone, created_by, created_at, updated_by, updated_at, "
              "last_tested_at, last_test_ok, last_test_error")


@dataclass
class ConnectionRow:
    id: str
    name: str
    dialect: str
    host: str
    port: int
    database: str
    username: str
    password_ciphertext: bytes | None
    tls_mode: str
    ca_pem: str | None
    timezone: str
    created_by: str
    created_at: Any
    updated_by: str | None
    updated_at: Any
    last_tested_at: Any
    last_test_ok: bool | None
    last_test_error: str | None

    def api(self) -> dict[str, Any]:
        unavailable = False
        if self.password_ciphertext is not None:
            try:
                decrypt_secret(self.password_ciphertext)
            except (SecretDecryptError, SecretKeyUnavailable):
                unavailable = True
        d = DIALECTS.get(self.dialect)
        return {
            "id": self.id, "name": self.name, "dialect": self.dialect, "ownerId": self.created_by,
            "dialectAvailable": bool(d and d.available),
            "host": self.host, "port": self.port, "database": self.database, "username": self.username,
            "tlsMode": self.tls_mode, "hasCaBundle": self.ca_pem is not None, "timezone": self.timezone,
            "hasPassword": self.password_ciphertext is not None,
            "passwordUnavailable": unavailable,
            "lastTestedAt": _iso(self.last_tested_at), "lastTestOk": self.last_test_ok,
            "lastTestError": self.last_test_error,
            "createdAt": _iso(self.created_at), "updatedAt": _iso(self.updated_at),
        }


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, _dt.datetime) else (None if v is None else str(v))


def _conn_row(r: tuple) -> ConnectionRow:
    return ConnectionRow(str(r[0]), r[1], r[2], r[3], int(r[4]), r[5], r[6],
                         bytes(r[7]) if r[7] is not None else None, r[8], r[9], r[10],
                         str(r[11]), r[12], str(r[13]) if r[13] else None, r[14], r[15], r[16], r[17])


async def list_connections(conn: AsyncConnection, owner: UUID | str | None) -> list[dict[str, Any]]:
    """`owner` = the caller's own rows; None = every row (the admin overview, which carries the
    owner's display name — and never a secret, by construction of `ConnectionRow.api`)."""
    cols = ", ".join("c." + c.strip() for c in _CONN_COLS.split(","))
    async with conn.cursor() as cur:
        if owner is None:
            await cur.execute(f"SELECT {cols}, u.display_name FROM platform.sql_connections c "
                              "JOIN platform.users u ON u.id = c.created_by ORDER BY c.name")
        else:
            await cur.execute(f"SELECT {cols}, u.display_name FROM platform.sql_connections c "
                              "JOIN platform.users u ON u.id = c.created_by WHERE c.created_by = %s ORDER BY c.name",
                              (str(owner),))
        rows = await cur.fetchall()
    out = []
    for r in rows:
        d = _conn_row(r[:-1]).api()
        d["ownerName"] = r[-1]
        out.append(d)
    return out


def owns(row_owner: UUID | str, user_id: UUID | str) -> bool:
    return str(row_owner) == str(user_id)


async def get_connection_row(conn: AsyncConnection, cid: UUID | str) -> ConnectionRow | None:
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT {_CONN_COLS} FROM platform.sql_connections WHERE id = %s", (str(cid),))
        r = await cur.fetchone()
    return _conn_row(r) if r else None


def _require_available(dialect: str) -> None:
    d = DIALECTS.get(dialect)
    if d is None or not d.available:
        raise SourceError("dialect_unavailable", (d.unavailable_reason if d else "unknown dialect"))


async def create_connection(conn: AsyncConnection, body: ConnectionWrite, actor: UUID) -> dict[str, Any]:
    _require_available(body.dialect)
    await vet_host(body.host, body.port)                       # the fence at CREATE
    cipher = encrypt_secret(body.password) if body.password else None   # encrypt BEFORE any SQL
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO platform.sql_connections "
            "(name, dialect, host, port, database, username, password_ciphertext, "
            " tls_mode, ca_pem, timezone, created_by, updated_by) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_CONN_COLS}",
            (body.name, body.dialect, body.host, body.port, body.database, body.username, cipher,
             body.tlsMode, body.caPem, body.timezone, actor, actor),
        )
        r = await cur.fetchone()
    await conn.commit()
    row = _conn_row(r)
    await admin_audit.record(conn, actor_id=actor, action="sql_connection.created", target_type="sql_connection",
                             target_id=row.id, detail={"name": row.name, "dialect": row.dialect, "host": row.host,
                                                       "port": row.port, "hasPassword": cipher is not None})
    log.info("sql_connection_created", id=row.id, host=row.host, dialect=row.dialect, actor=str(actor))
    return row.api()


async def update_connection(conn: AsyncConnection, cid: UUID, body: ConnectionWrite, actor: UUID) -> dict[str, Any] | None:
    existing = await get_connection_row(conn, cid)
    if existing is None:
        return None
    _require_available(body.dialect)
    await vet_host(body.host, body.port)
    if body.password:
        pw_set, pw_params = "password_ciphertext = %s, ", (encrypt_secret(body.password),)
    elif body.clearPassword:
        pw_set, pw_params = "password_ciphertext = NULL, ", ()
    else:
        pw_set, pw_params = "", ()
    if body.caPem:
        ca_set, ca_params = "ca_pem = %s, ", (body.caPem,)
    elif body.clearCaBundle:
        ca_set, ca_params = "ca_pem = NULL, ", ()
    else:
        ca_set, ca_params = "", ()
    async with conn.cursor() as cur:
        await cur.execute(
            f"UPDATE platform.sql_connections SET name = %s, dialect = %s, host = %s, port = %s, database = %s, "
            f"username = %s, {pw_set}{ca_set}tls_mode = %s, timezone = %s, updated_by = %s, updated_at = now() "
            f"WHERE id = %s RETURNING {_CONN_COLS}",
            (body.name, body.dialect, body.host, body.port, body.database, body.username,
             *pw_params, *ca_params, body.tlsMode, body.timezone, actor, str(cid)),
        )
        r = await cur.fetchone()
    await conn.commit()
    row = _conn_row(r)
    await admin_audit.record(conn, actor_id=actor, action="sql_connection.updated", target_type="sql_connection",
                             target_id=row.id, detail={"name": row.name, "host": row.host, "port": row.port,
                                                       "passwordReplaced": bool(body.password),
                                                       "passwordCleared": body.clearPassword and not body.password})
    log.info("sql_connection_updated", id=row.id, host=row.host, actor=str(actor))
    return row.api()


async def delete_connection(conn: AsyncConnection, cid: UUID, actor: UUID) -> bool:
    existing = await get_connection_row(conn, cid)
    if existing is None:
        return False
    async with conn.cursor() as cur:
        await cur.execute("SELECT target_layer_id FROM platform.sql_pulls WHERE connection_id = %s", (str(cid),))
        layers = [r[0] for r in await cur.fetchall()]
        await cur.execute("DELETE FROM platform.sql_connections WHERE id = %s", (str(cid),))   # cascades pulls + runs
        for lid in layers:
            await _unlock_layer(cur, lid)               # detached copies are frozen, no longer locked (F3) — after the cascade (S3-D)
    await conn.commit()
    await admin_audit.record(conn, actor_id=actor, action="sql_connection.deleted", target_type="sql_connection",
                             target_id=existing.id, detail={"name": existing.name, "host": existing.host, "pulls": len(layers)})
    log.info("sql_connection_deleted", id=existing.id, host=existing.host, pulls=len(layers), actor=str(actor))
    return True


async def open_session(row: ConnectionRow, *, resolver: Resolver = default_resolver) -> tuple[Any, SourceSession]:
    """Vet the host, decrypt the password, open the dialect's session. Every caller closes it."""
    _require_available(row.dialect)
    password: str | None = None
    if row.password_ciphertext is not None:
        try:
            password = decrypt_secret(row.password_ciphertext)
        except (SecretDecryptError, SecretKeyUnavailable):
            raise SourceError("password_unavailable",
                              "the stored password cannot be opened with this server's encryption key — re-enter it") from None
    ip = await vet_host(row.host, row.port, resolver=resolver)
    info = ConnInfo(host=row.host, port=row.port, database=row.database, username=row.username,
                    password=password, tls_mode=row.tls_mode, ca_pem=row.ca_pem, timezone=row.timezone)
    dialect = DIALECTS[row.dialect]
    session = await dialect.open(info, ip)
    return dialect, session


async def test_connection(conn: AsyncConnection, cid: UUID, actor: UUID) -> dict[str, Any] | None:
    row = await get_connection_row(conn, cid)
    if row is None:
        return None
    ok, reason, message, tables = False, None, None, None
    try:
        _d, session = await open_session(row)
        try:
            await session.ping()
            tables = len(await session.catalog_tables())
            ok = True
        finally:
            await session.close()
    except SourceError as exc:
        reason, message = exc.reason, str(exc)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.sql_connections SET last_tested_at = now(), last_test_ok = %s, last_test_error = %s WHERE id = %s",
            (ok, None if ok else (message or "")[:300], str(cid)),
        )
    await conn.commit()
    await admin_audit.record(conn, actor_id=actor, action="sql_connection.tested", target_type="sql_connection",
                             target_id=row.id, detail={"host": row.host, "ok": ok, "reason": reason})
    return {"ok": ok, "reason": reason, "message": message, "tables": tables}


# ---------------------------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------------------------
def _table_dict(t: TableInfo) -> dict[str, Any]:
    return {"schema": t.schema, "name": t.name, "kind": t.kind, "keyColumns": list(t.key_columns)}


def _column_dict(c: ColumnInfo) -> dict[str, Any]:
    return {"name": c.name, "sourceType": c.source_type, "dataType": c.data_type,
            "numericSubtype": c.numeric_subtype, "temporal": c.temporal, "unsupportedReason": c.unsupported_reason}


async def catalog_tables(conn: AsyncConnection, cid: UUID) -> list[dict[str, Any]] | None:
    row = await get_connection_row(conn, cid)
    if row is None:
        return None
    _d, session = await open_session(row)
    try:
        return [_table_dict(t) for t in await session.catalog_tables()]
    finally:
        await session.close()


async def catalog_columns(conn: AsyncConnection, cid: UUID, schema: str, table: str) -> dict[str, Any] | None:
    row = await get_connection_row(conn, cid)
    if row is None:
        return None
    _d, session = await open_session(row)
    try:
        tables = await session.catalog_tables()
        match = next((t for t in tables if t.schema == schema and t.name == table), None)
        if match is None:
            raise SourceError("table_missing", "that table or view is not in the database catalog (renamed or dropped?)")
        cols = await session.catalog_columns(schema, table)
    finally:
        await session.close()
    return {"table": _table_dict(match), "columns": [_column_dict(c) for c in cols],
            "xyCandidates": xy_candidates([c.name for c in cols if c.data_type in ("number", "text")])}


# ---------------------------------------------------------------------------------------------
# Pulls
# ---------------------------------------------------------------------------------------------
_PULL_COLS = ("p.id, p.connection_id, c.name, p.target_layer_id, l.display_name, p.source_schema, p.source_table, "
              "p.columns, p.key_column, p.created_by, p.created_at, p.updated_at, u.display_name, p.geometry, l.geom_type, "
              "p.schedule_kind, p.schedule_hour, p.schedule_minutes, p.next_run_at, p.stale_since, p.last_success_at, p.last_error")
_PULL_FROM = ("FROM platform.sql_pulls p JOIN platform.sql_connections c ON c.id = p.connection_id "
              "JOIN platform.layers l ON l.layer_id = p.target_layer_id JOIN platform.users u ON u.id = p.created_by")
_RUN_COLS = ("id, pull_id, status, trigger, total_rows, processed_rows, inserted_rows, failed_rows, error_sample, "
             "error, column_stats, created_by, created_at, started_at, finished_at, updated_at, "
             "updated_rows, deleted_rows, unchanged_rows")


def _run_dict(r: tuple) -> dict[str, Any]:
    keys = ["id", "pullId", "status", "trigger", "totalRows", "processedRows", "insertedRows", "failedRows",
            "errorSample", "error", "columnStats", "createdBy", "createdAt", "startedAt", "finishedAt", "updatedAt",
            "updatedRows", "deletedRows", "unchangedRows"]
    d = dict(zip(keys, r))
    d["id"], d["pullId"], d["createdBy"] = str(d["id"]), str(d["pullId"]), str(d["createdBy"])
    for k in ("createdAt", "startedAt", "finishedAt", "updatedAt"):
        d[k] = _iso(d[k])
    return d


def _pull_dict(r: tuple, last_run: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": str(r[0]), "connectionId": str(r[1]), "connectionName": r[2],
        "targetLayerId": r[3], "targetName": r[4], "schema": r[5], "table": r[6],
        "columns": r[7], "keyColumn": r[8], "createdBy": str(r[9]), "ownerId": str(r[9]), "ownerName": r[12],
        "geometry": r[13], "targetGeomType": r[14],
        "schedule": {"kind": r[15], "hour": r[16], "minutes": r[17]}, "nextRunAt": _iso(r[18]),
        "staleSince": _iso(r[19]), "lastSuccessAt": _iso(r[20]), "lastError": r[21],
        "createdAt": _iso(r[10]), "updatedAt": _iso(r[11]), "lastRun": last_run,
    }


async def _last_run(conn: AsyncConnection, pull_id: str) -> dict[str, Any] | None:
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT {_RUN_COLS} FROM platform.sql_pull_runs WHERE pull_id = %s "
                          "ORDER BY created_at DESC LIMIT 1", (pull_id,))
        r = await cur.fetchone()
    return _run_dict(r) if r else None


async def list_pulls(conn: AsyncConnection, owner: UUID | str | None) -> list[dict[str, Any]]:
    async with conn.cursor() as cur:
        if owner is None:
            await cur.execute(f"SELECT {_PULL_COLS} {_PULL_FROM} ORDER BY p.created_at DESC")
        else:
            await cur.execute(f"SELECT {_PULL_COLS} {_PULL_FROM} WHERE p.created_by = %s ORDER BY p.created_at DESC", (str(owner),))
        rows = await cur.fetchall()
    return [_pull_dict(r, await _last_run(conn, str(r[0]))) for r in rows]


async def get_pull(conn: AsyncConnection, pid: UUID) -> dict[str, Any] | None:
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT {_PULL_COLS} {_PULL_FROM} WHERE p.id = %s", (str(pid),))
        r = await cur.fetchone()
    return _pull_dict(r, await _last_run(conn, str(r[0]))) if r else None


async def run_owner(conn: AsyncConnection, rid: UUID) -> str | None:
    """The owner (pull creator) of a run, for the visibility check — None when the run does not exist."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT p.created_by FROM platform.sql_pull_runs r JOIN platform.sql_pulls p ON p.id = r.pull_id "
                          "WHERE r.id = %s", (str(rid),))
        r = await cur.fetchone()
    return str(r[0]) if r else None


async def _lock_layer(cur, layer_id: str) -> None:
    """F3: a pulled copy is read-only in Talus while a source is attached — the master switch and
    all four V109 capabilities off. The run writes directly, never through the edit doors."""
    await cur.execute("UPDATE platform.layers SET editing_enabled = false, edit_add = false, edit_attributes = false, "
                      "edit_geometry = false, edit_delete = false WHERE layer_id = %s", (layer_id,))


async def _unlock_layer(cur, layer_id: str) -> None:
    await cur.execute("UPDATE platform.layers SET editing_enabled = true, edit_add = true, edit_attributes = true, "
                      "edit_geometry = true, edit_delete = true WHERE layer_id = %s", (layer_id,))


def plan_columns(requested: list[PullColumn], catalog: list[ColumnInfo], key_column: str | None) -> list[dict[str, Any]]:
    """The column plan for a pull: every requested column must exist in the catalog and be
    supported; the key (if any) must be requested. Pure, so it is unit-tested without a source."""
    by_name = {c.name: c for c in catalog}
    seen: set[str] = set()
    plan: list[dict[str, Any]] = []
    for rc in requested:
        c = by_name.get(rc.name)
        if c is None:
            raise SourceError("column_missing", f"column '{rc.name}' is not in the source table")
        if rc.name in seen:
            raise SourceError("column_duplicate", f"column '{rc.name}' is listed twice")
        seen.add(rc.name)
        if c.data_type is None:
            raise SourceError("column_unsupported", f"column '{rc.name}' cannot be pulled: {c.unsupported_reason}")
        data_type = "date" if (rc.toDate and c.temporal) else c.data_type
        entry = {"name": c.name, "sourceType": c.source_type, "dataType": data_type,
                 "numericSubtype": c.numeric_subtype if data_type == "number" else None,
                 "temporal": c.temporal, "utc": bool(rc.utc), "fieldKey": None}
        if c.name.strip().lower() == "notes":
            # The hosted table's own `notes` column is reserved as a field key (the core refuses it). The
            # bulk import maps a source `notes` column onto the ROW's notes — pulls do the same: it is
            # never a draft, and the run writes it to `notes`, not `props` (SQL Server e2e, 2026-10-01).
            entry.update({"fieldKey": "notes", "isNotes": True, "dataType": "text", "numericSubtype": None})
        plan.append(entry)
    if key_column is not None and key_column not in seen:
        raise SourceError("key_not_selected", "the key column must be one of the pulled columns")
    return plan


def xy_candidates(names: list[str]) -> dict[str, Any] | None:
    """The bulk import's coordinate-column detector (`_csv_xy`) over a column list: {x, y, srid|None}
    for a lon/lat-named or projected x/y pair, None when the names say nothing. Pure; never raises."""
    from ..import_jobs_service import _csv_xy
    try:
        xi, yi, srid = _csv_xy(names)
    except Exception:  # noqa: BLE001 — the detector's 422 is "nothing to pre-select" here
        return None
    return {"x": names[xi], "y": names[yi], "srid": srid}


def plan_geometry(geometry: PullGeometry | None, plan: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The geometry plan for an x/y pull: both coordinate columns must be pulled columns of a numeric
    or text type, and the SRID must be known — detected 4326 for a lon/lat-named pair, else declared.
    Pure, so it is unit-tested without a source. (spatial_ref_sys validation happens at create.)"""
    if geometry is None:
        return None
    by_name = {p["name"]: p for p in plan}
    for axis, col in (("x", geometry.x), ("y", geometry.y)):
        p = by_name.get(col)
        if p is None:
            raise SourceError("geometry_column_missing", f"the {axis} column '{col}' must be one of the pulled columns")
        if p["dataType"] not in ("number", "text"):
            raise SourceError("geometry_column_type", f"the {axis} column '{col}' is {p['dataType']}, not a coordinate")
    if geometry.x == geometry.y:
        raise SourceError("geometry_column_missing", "x and y must be different columns")
    srid = geometry.srid
    if srid is None:
        det = xy_candidates([geometry.x, geometry.y])
        srid = det["srid"] if det else None
    if srid is None:
        raise SourceError("srid_required", "these coordinate columns do not name a lon/lat pair — declare the EPSG code of the projected system they are in")
    return {"kind": "xy", "x": geometry.x, "y": geometry.y, "srid": int(srid)}


async def create_pull(conn: AsyncConnection, body: PullCreate, actor_user: Any) -> dict[str, Any]:
    """Validate against the LIVE catalog first (nothing is created on a bad request), then: the
    hosted table (owned by the admin, editing locked), its fields through the shared field-import
    core, the pull row, and the first run. `conn` is an OWNER-pool connection (runtime DDL)."""
    from ..routers.layer_fields import _run_field_import
    from ..routers.publisher import _create_hosted_table, _create_spatial_layer
    from ..import_jobs_service import _draft

    row = await get_connection_row(conn, body.connectionId)
    if row is None or not owns(row.created_by, actor_user.id):
        raise SourceError("connection_missing", "that connection does not exist")   # existence not disclosed
    _d, session = await open_session(row)
    try:
        tables = await session.catalog_tables()
        match = next((t for t in tables if t.schema == body.schema_ and t.name == body.table), None)
        if match is None:
            raise SourceError("table_missing", "that table or view is not in the database catalog (renamed or dropped?)")
        catalog = await session.catalog_columns(body.schema_, body.table)
    finally:
        await session.close()
    plan = plan_columns(body.columns, catalog, body.keyColumn)
    if body.keyColumn is not None and body.keyColumn not in match.key_columns:
        raise SourceError("key_not_unique", "the key column must be a primary key or a single-column unique index in the source")
    geom = plan_geometry(body.geometry, plan)
    if geom is not None:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM public.spatial_ref_sys WHERE srid = %s", (geom["srid"],))
            if await cur.fetchone() is None:
                raise SourceError("srid_unknown", f"EPSG {geom['srid']} is not a known coordinate system on this server")

    drafts = [_draft(p["name"], p["dataType"], set(), p["numericSubtype"]) for p in plan if not p.get("isNotes")]

    # Dry-run the field drafts through the shared core BEFORE anything is created: a draft the core
    # would refuse (its verdict, not an exception) must fail the request with nothing provisioned —
    # otherwise a table with no fields is left behind (the e2e proof's first orphan, 2026-10-01).
    probe = await _run_field_import(conn, "__sql_pull_probe__", drafts, False, str(actor_user.id))
    bad = [r for r in probe["rows"] if r.get("status") not in ("ok", "renamed")]
    if bad:
        names = ", ".join(f"{r.get('fieldKey', '?')} ({r.get('reason', '')})" for r in bad[:10])
        raise SourceError("fields_not_created", f"these columns cannot be created as fields: {names}")

    if geom is None:
        created = await _create_hosted_table(conn, label=body.name, color=None, actor=actor_user.id)
    else:
        # A point layer, canonical 4326 storage; the run reprojects from the plan's SRID (the import's E4 rule).
        created = await _create_spatial_layer(conn, label=body.name, color=None, working_srid=4326, native=False,
                                              geom_type="point", actor=actor_user.id)
    layer_id = str(created["layer_id"])
    async with conn.cursor() as cur:
        await _lock_layer(cur, layer_id)
    await conn.commit()

    summary = (await _run_field_import(conn, layer_id, drafts, True, str(actor_user.id)))["summary"]
    if summary.get("created", 0) + summary.get("renamed", 0) == 0:
        log.warning("sql_pull_field_import_empty", layer_id=layer_id, summary=summary)
        raise SourceError("fields_not_created", f"none of the {len(drafts)} columns could be created as fields ({summary})")
    async with conn.cursor() as cur:
        await cur.execute("SELECT field_key, label FROM platform.layer_fields WHERE layer_id = %s", (layer_id,))
        key_by_label = {label: fk for fk, label in await cur.fetchall()}
    missing = [p["name"] for p in plan if not p.get("isNotes") and p["name"] not in key_by_label]
    if missing:
        raise SourceError("fields_not_created", f"these columns could not be created as fields: {', '.join(missing[:10])}")
    for p in plan:
        if not p.get("isNotes"):
            p["fieldKey"] = key_by_label[p["name"]]

    pull_id = uuid4()
    sch = (body.schedule or PullSchedule(kind="nightly", hour=2)).normalised()
    try:
        zone: _dt.tzinfo = zoneinfo.ZoneInfo(row.timezone)
    except Exception:  # noqa: BLE001
        zone = _dt.timezone.utc
    nxt = next_run_at(sch, zone)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO platform.sql_pulls (id, connection_id, target_layer_id, source_schema, source_table, columns, "
            "key_column, created_by, geometry, schedule_kind, schedule_hour, schedule_minutes, next_run_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (pull_id, str(body.connectionId), layer_id, body.schema_, body.table, Jsonb(plan), body.keyColumn, actor_user.id,
             Jsonb(geom) if geom is not None else None, sch.kind, sch.hour, sch.minutes, nxt),
        )
    await conn.commit()
    await admin_audit.record(conn, actor_id=actor_user.id, action="sql_pull.created", target_type="sql_pull",
                             target_id=str(pull_id), detail={"connection": row.name, "source": f"{body.schema_}.{body.table}",
                                                             "layer": layer_id, "columns": len(plan), "key": body.keyColumn,
                                                             "geometry": geom})
    log.info("sql_pull_created", pull_id=str(pull_id), layer_id=layer_id, source=f"{body.schema_}.{body.table}",
             columns=len(plan), actor=str(actor_user.id))
    run = await enqueue_run(conn, pull_id, actor_user.id)
    pull = await get_pull(conn, pull_id)
    assert pull is not None
    pull["lastRun"] = run
    return pull


async def set_schedule(conn: AsyncConnection, pid: UUID, schedule: PullSchedule, actor: UUID) -> dict[str, Any] | None:
    pull = await get_pull(conn, pid)
    if pull is None:
        return None
    crow = await get_connection_row(conn, pull["connectionId"])
    try:
        zone: _dt.tzinfo = zoneinfo.ZoneInfo(crow.timezone if crow else "UTC")
    except Exception:  # noqa: BLE001
        zone = _dt.timezone.utc
    sch = schedule.normalised()
    nxt = next_run_at(sch, zone)
    async with conn.cursor() as cur:
        await cur.execute("UPDATE platform.sql_pulls SET schedule_kind = %s, schedule_hour = %s, schedule_minutes = %s, "
                          "next_run_at = %s, updated_at = now() WHERE id = %s", (sch.kind, sch.hour, sch.minutes, nxt, str(pid)))
    await conn.commit()
    await admin_audit.record(conn, actor_id=actor, action="sql_pull.schedule_set", target_type="sql_pull", target_id=str(pid),
                             detail={"kind": sch.kind, "hour": sch.hour, "minutes": sch.minutes, "nextRunAt": _iso(nxt)})
    return await get_pull(conn, pid)


async def enqueue_due(conn: AsyncConnection) -> int:
    """S3-B, the worker's scheduler tick: every scheduled pull whose time has come and has no live run gets a
    run under its OWNER's identity (`trigger='schedule'`), and its next time is set. Returns the count."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT p.id, p.created_by, p.schedule_kind, p.schedule_hour, p.schedule_minutes, c.timezone "
            "FROM platform.sql_pulls p JOIN platform.sql_connections c ON c.id = p.connection_id "
            "JOIN platform.users u ON u.id = p.created_by "
            "WHERE p.schedule_kind <> 'manual' AND p.next_run_at IS NOT NULL AND p.next_run_at <= now() "
            # §11.2 L5, the no-network backstop: an owner who is disabled or removed gets no run even if a door
            # forgot the hook. A demotion lives in Keycloak and is invisible here — that is L3's hook, not this.
            "  AND u.disabled_at IS NULL AND u.removed_at IS NULL "
            "  AND NOT EXISTS (SELECT 1 FROM platform.sql_pull_runs r WHERE r.pull_id = p.id AND r.status IN ('pending', 'running'))")
        due = await cur.fetchall()
        n = 0
        for pid, owner, kind, hour, minutes, tz in due:
            try:
                zone: _dt.tzinfo = zoneinfo.ZoneInfo(tz)
            except Exception:  # noqa: BLE001
                zone = _dt.timezone.utc
            nxt = next_run_at(PullSchedule(kind=kind, hour=hour, minutes=minutes), zone)
            await cur.execute("INSERT INTO platform.sql_pull_runs (pull_id, created_by, trigger) VALUES (%s, %s, 'schedule')", (pid, owner))
            await cur.execute("UPDATE platform.sql_pulls SET next_run_at = %s WHERE id = %s", (nxt, pid))
            n += 1
    await conn.commit()
    if n:
        log.info("sql_pull_scheduled_runs_enqueued", count=n)
    return n


async def delete_pull(conn: AsyncConnection, pid: UUID, actor: UUID) -> bool:
    pull = await get_pull(conn, pid)
    if pull is None:
        return False
    async with conn.cursor() as cur:
        # S3-D: the lock trigger refuses flags ON while a pull row targets the layer — delete the row FIRST.
        await cur.execute("DELETE FROM platform.sql_pulls WHERE id = %s", (str(pid),))
        await _unlock_layer(cur, pull["targetLayerId"])
    await conn.commit()
    await admin_audit.record(conn, actor_id=actor, action="sql_pull.deleted", target_type="sql_pull", target_id=str(pid),
                             detail={"layer": pull["targetLayerId"], "source": f"{pull['schema']}.{pull['table']}"})
    log.info("sql_pull_deleted", pull_id=str(pid), layer_id=pull["targetLayerId"], actor=str(actor))
    return True


# ---------------------------------------------------------------------------------------------
# Owner lifecycle (decision §11.2) — the hooks the user doors call. A pull runs under its owner's stored
# identity, so the owner's lifecycle IS the pull's: disable / demote flip it to manual with a stale reason
# (the owner re-sets the schedule by hand after a re-enable — no silent resume), transfer detaches it (a
# personal connection cannot follow a layer to another person), offboard deletes the connections with the
# identity. Each hook writes on the caller's connection and COMMITS (the door's own state rides the same
# transaction), then audits.
# ---------------------------------------------------------------------------------------------
LIFECYCLE_AUDIT_ACTION = "sql_pull.deactivated_by_lifecycle"


def lifecycle_reason(event: str, *, role: str | None = None, when: _dt.date | None = None) -> str:
    """The `last_error` the badge shows: 'owner disabled on 2026-10-02' / 'owner role changed to viewer on …'."""
    day = (when or _dt.datetime.now(_dt.timezone.utc).date()).isoformat()
    if event == "role_changed":
        short = (role or "").removeprefix("talus-").removesuffix("-role") or "a non-publisher role"
        return f"owner role changed to {short} on {day}"
    return f"owner disabled on {day}"


def role_may_run_pulls(role: str) -> bool:
    """The right to run pulls is the publisher gate (`_require_publisher`): publisher or admin."""
    return role in ("talus-publisher", "talus-admin")


async def owner_is_active(conn: AsyncConnection, owner_id: UUID | str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT disabled_at IS NULL AND removed_at IS NULL FROM platform.users WHERE id = %s", (str(owner_id),))
        r = await cur.fetchone()
    return bool(r and r[0])


async def _cancel_live_runs(cur, pull_ids: list[str]) -> int:
    if not pull_ids:
        return 0
    await cur.execute("UPDATE platform.sql_pull_runs SET status = 'canceled', finished_at = now(), updated_at = now() "
                      "WHERE pull_id = ANY(%s::uuid[]) AND status IN ('pending', 'running')", (pull_ids,))
    return cur.rowcount or 0


async def deactivate_owner_pulls(conn: AsyncConnection, owner_id: UUID | str, *, event: str, actor: UUID | str,
                                 role: str | None = None) -> list[str]:
    """L1 / L3: every pull of this owner → manual, no next time, stale with the reason; live runs canceled.
    The copy stays; nothing is deleted. Returns the pull ids touched (empty for an owner with no pulls)."""
    reason = lifecycle_reason(event, role=role)
    async with conn.cursor() as cur:
        await cur.execute("UPDATE platform.sql_pulls SET schedule_kind = 'manual', schedule_hour = NULL, schedule_minutes = NULL, "
                          "next_run_at = NULL, stale_since = COALESCE(stale_since, now()), last_error = %s, updated_at = now() "
                          "WHERE created_by = %s RETURNING id::text, target_layer_id", (reason, str(owner_id)))
        rows = await cur.fetchall()
        canceled = await _cancel_live_runs(cur, [r[0] for r in rows])
    await conn.commit()
    for pid, layer_id in rows:
        await admin_audit.record(conn, actor_id=actor, action=LIFECYCLE_AUDIT_ACTION, target_type="sql_pull", target_id=pid,
                                 detail={"event": event, "owner": str(owner_id), "layer": layer_id, "role": role, "reason": reason})
    if rows:
        log.info("sql_pulls_deactivated_by_lifecycle", lifecycle_event=event, owner=str(owner_id), pulls=len(rows), canceled_runs=canceled,
                 actor=str(actor))
    return [r[0] for r in rows]


async def detach_layer_pull(conn: AsyncConnection, layer_id: str, *, actor: UUID | str, previous_owner: str | None,
                            new_owner: str) -> str | None:
    """L4, transfer: the pull row goes first (the V113 trigger then lets the unlock through), the successor
    gets an ordinary, editable copy. Returns the detached pull id, or None when the layer had no pull."""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM platform.sql_pulls WHERE target_layer_id = %s RETURNING id::text, created_by::text, "
                          "source_schema, source_table", (layer_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        await _unlock_layer(cur, layer_id)
    await conn.commit()
    pid, owner, schema, table = row
    await admin_audit.record(conn, actor_id=actor, action=LIFECYCLE_AUDIT_ACTION, target_type="sql_pull", target_id=pid,
                             detail={"event": "transferred", "owner": owner, "layer": layer_id, "previousOwner": previous_owner,
                                     "newOwner": new_owner, "source": f"{schema}.{table}"})
    log.info("sql_pull_detached_by_transfer", pull_id=pid, layer_id=layer_id, new_owner=new_owner, actor=str(actor))
    return pid


async def purge_owner_connections(conn: AsyncConnection, owner_id: UUID | str, *, actor: UUID | str) -> int:
    """L4, offboard: a personal credential never survives its person. Deleting the connections cascades any
    remaining pulls, runs and diff state; their layers are unlocked after (the s3 ordering). Returns the
    number of connections removed."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT p.id::text, p.target_layer_id FROM platform.sql_pulls p JOIN platform.sql_connections c "
                          "ON c.id = p.connection_id WHERE c.created_by = %s", (str(owner_id),))
        pulls = await cur.fetchall()
        await cur.execute("DELETE FROM platform.sql_connections WHERE created_by = %s RETURNING id::text, name, host",
                          (str(owner_id),))
        conns = await cur.fetchall()
        for _pid, layer_id in pulls:
            await _unlock_layer(cur, layer_id)
    await conn.commit()
    for pid, layer_id in pulls:
        await admin_audit.record(conn, actor_id=actor, action=LIFECYCLE_AUDIT_ACTION, target_type="sql_pull", target_id=pid,
                                 detail={"event": "offboarded", "owner": str(owner_id), "layer": layer_id})
    for cid, name, host in conns:
        await admin_audit.record(conn, actor_id=actor, action="sql_connection.deleted", target_type="sql_connection", target_id=cid,
                                 detail={"name": name, "host": host, "event": "offboarded", "owner": str(owner_id)})
    if conns:
        log.info("sql_connections_purged_by_offboard", owner=str(owner_id), connections=len(conns), pulls=len(pulls), actor=str(actor))
    return len(conns)


# ---------------------------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------------------------
class RunConflict(Exception):
    """A run is already pending or running for this pull."""


async def enqueue_run(conn: AsyncConnection, pid: UUID, actor: UUID) -> dict[str, Any]:
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.sql_pull_runs WHERE pull_id = %s AND status IN ('pending', 'running') LIMIT 1",
                          (str(pid),))
        if await cur.fetchone():
            raise RunConflict()
        await cur.execute(f"INSERT INTO platform.sql_pull_runs (pull_id, created_by) VALUES (%s, %s) RETURNING {_RUN_COLS}",
                          (str(pid), actor))
        r = await cur.fetchone()
    await conn.commit()
    run = _run_dict(r)
    await admin_audit.record(conn, actor_id=actor, action="sql_pull.run_requested", target_type="sql_pull",
                             target_id=str(pid), detail={"run": run["id"]})
    return run


async def get_run(conn: AsyncConnection, rid: UUID) -> dict[str, Any] | None:
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT {_RUN_COLS} FROM platform.sql_pull_runs WHERE id = %s", (str(rid),))
        r = await cur.fetchone()
    return _run_dict(r) if r else None


async def claim_next_run(conn: AsyncConnection) -> dict[str, Any] | None:
    """Oldest live run, FIFO. A 'running' row can only be an orphan (single worker under the
    advisory lock); its transaction died with the process, so re-running IS the resume path."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"UPDATE platform.sql_pull_runs SET status = 'running', started_at = now(), updated_at = now(), "
            f"processed_rows = 0, inserted_rows = 0, failed_rows = 0, error_sample = '[]'::jsonb, error = NULL "
            f"WHERE id = (SELECT id FROM platform.sql_pull_runs WHERE status IN ('pending', 'running') "
            f"            ORDER BY created_at LIMIT 1) RETURNING {_RUN_COLS}",
        )
        r = await cur.fetchone()
    await conn.commit()
    return _run_dict(r) if r else None


async def _mark_failed(conn: AsyncConnection, run_id: str, reason: str) -> None:
    """S3-C: the run fails, the copy is untouched, and the PULL turns stale (first failure sets `stale_since`;
    every failure refreshes `last_error`). The schedule keeps trying at its next slot."""
    async with conn.cursor() as cur:
        await cur.execute("UPDATE platform.sql_pull_runs SET status = 'failed', error = %s, finished_at = now(), "
                          "updated_at = now() WHERE id = %s", (reason[:300], run_id))
        await cur.execute("UPDATE platform.sql_pulls SET stale_since = COALESCE(stale_since, now()), last_error = %s "
                          "WHERE id = (SELECT pull_id FROM platform.sql_pull_runs WHERE id = %s)", (reason[:300], run_id))
    await conn.commit()


class _RunAbort(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def process_run(conn: AsyncConnection, run: dict[str, Any]) -> None:
    """One run = ONE transaction on `conn`: replace the copy, verify the counts, commit. Progress
    and the cancel check ride a SECOND autocommit connection so the admin sees movement while the
    transaction is open. Never raises — a bad run is marked failed with an honest first line."""
    from ..db import get_pool
    from ..import_jobs_service import _load_actor
    from ..layer_props import PropsError, active_field_defs, validate_props

    run_id = run["id"]
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT p.connection_id, p.target_layer_id, p.source_schema, p.source_table, p.columns, p.key_column, "
                "p.created_by, l.geom_type, p.geometry, l.native_storage, l.working_srid "
                "FROM platform.sql_pulls p JOIN platform.layers l ON l.layer_id = p.target_layer_id WHERE p.id = %s",
                (run["pullId"],))
            prow = await cur.fetchone()
        if prow is None:
            await _mark_failed(conn, run_id, "the pull or its target table no longer exists")
            return
        connection_id, layer_id, schema, table, plan, _key, owner_id, geom_type, geom, native, working_srid = prow
        if _key is not None and _key not in [p["name"] for p in plan]:
            _key = None       # a key that is no longer pulled falls back to content identity
        expected_geom = "none" if geom is None else "point"
        if geom_type != expected_geom:
            await _mark_failed(conn, run_id, f"the target is a {geom_type} layer; this pull expects {expected_geom}")
            return
        storage_srid = int(working_srid) if native else 4326
        actor = await _load_actor(conn, str(owner_id))
        if actor is None:
            await _mark_failed(conn, run_id, "the pull's owner no longer exists")
            return
        if not await owner_is_active(conn, owner_id):
            # §11.2 L5: a run claimed in the window between a lifecycle event and its cancel never writes.
            await _mark_failed(conn, run_id, "the pull's owner is no longer active")
            return
        crow = await get_connection_row(conn, connection_id)
        if crow is None:
            await _mark_failed(conn, run_id, "the connection no longer exists")
            return
        try:
            zone: _dt.tzinfo = zoneinfo.ZoneInfo(crow.timezone)
        except Exception:  # noqa: BLE001
            zone = _dt.timezone.utc

        names = [p["name"] for p in plan]
        field_defs = await active_field_defs(conn, layer_id)
        for p in plan:
            if not p.get("isNotes") and p["fieldKey"] not in field_defs:
                await _mark_failed(conn, run_id, f"field '{p['fieldKey']}' for column '{p['name']}' was removed from the table")
                return

        try:
            _d, session = await open_session(crow)     # the fence, at EVERY run
        except SourceError as exc:
            await _mark_failed(conn, run_id, f"{exc.reason}: {exc}")
            return
        try:
            try:
                live = {c.name: c for c in await session.catalog_columns(schema, table)}
                # Schema drift (trap 5.4): a renamed/dropped column or a changed type FAILS the run loudly
                # and keeps the last good copy — never a silently nulled field.
                for p in plan:
                    c = live.get(p["name"])
                    if c is None:
                        raise _RunAbort(f"source column '{p['name']}' no longer exists — the pull needs editing (last good copy kept)")
                    expected = "date" if (p["dataType"] == "date" and p["temporal"]) else p["dataType"]
                    if c.data_type != expected and not (p["temporal"] and c.temporal):
                        raise _RunAbort(f"source column '{p['name']}' changed type ({p['sourceType']} → {c.source_type}) — last good copy kept")
                located = (geom["x"], geom["y"]) if geom is not None else None
                src_counts = await session.counts(schema, table, names, located)
                cap = settings.import_max_features
                if src_counts.total > cap:
                    raise _RunAbort(f"the source has {src_counts.total} rows, above the per-run cap ({cap})")
                async with get_pool().connection() as pconn:
                    await pconn.set_autocommit(True)
                    await _replace_copy(conn, pconn, session, run_id, layer_id, schema, table, plan, names,
                                        field_defs, actor, zone, src_counts, validate_props, PropsError,
                                        geom=geom, storage_srid=storage_srid, pull_id=str(run["pullId"]), key_column=_key)
            except SourceError as exc:
                raise _RunAbort(f"{exc.reason}: {exc}") from None
        finally:
            await session.close()
    except _RunAbort as exc:
        try:
            await conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.warning("sql_pull_run_failed", run_id=run_id, reason=exc.reason)
        await _mark_failed(conn, run_id, exc.reason)
    except Exception as exc:  # noqa: BLE001 — the worker never dies on a bad run
        try:
            await conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.warning("sql_pull_run_crashed", run_id=run_id, exc_info=True)
        try:
            await _mark_failed(conn, run_id, (str(exc).splitlines() or [type(exc).__name__])[0][:300] or type(exc).__name__)
        except Exception:  # noqa: BLE001
            log.warning("sql_pull_run_mark_failed", run_id=run_id, exc_info=True)


def row_hash(props: dict[str, Any], notes: str | None, x: float | None, y: float | None) -> str:
    """The canonical content hash of a copied row (S3-A): the coerced props, the notes and the coordinate pair,
    exactly as they will be stored — so an unchanged source row hashes identically run after run."""
    payload = json.dumps([props, notes, x, y], sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_refresh(existing: dict[str, tuple[str, str]], incoming: list[tuple[str, str]]) -> dict[str, Any]:
    """Pure: given the diff state (`source_key → (row_id, hash)`) and the incoming (key, hash) list, classify each
    incoming row as unchanged / update / insert and the absent keys as deletes. Unit-tested without a database."""
    unchanged: list[str] = []
    updates: list[tuple[str, str]] = []     # (key, row_id)
    inserts: list[str] = []
    seen: set[str] = set()
    for key, h in incoming:
        seen.add(key)
        cur = existing.get(key)
        if cur is None:
            inserts.append(key)
        elif cur[1] == h:
            unchanged.append(key)
        else:
            updates.append((key, cur[0]))
    deletes = [(k, rid) for k, (rid, _h) in existing.items() if k not in seen]
    return {"unchanged": unchanged, "updates": updates, "inserts": inserts, "deletes": deletes}


async def _replace_copy(conn, pconn, session, run_id, layer_id, schema, table, plan, names,
                        field_defs, actor, zone, src_counts, validate_props, PropsError,
                        geom: dict[str, Any] | None = None, storage_srid: int = 4326, pull_id: str | None = None,
                        key_column: str | None = None) -> None:
    """S3-A: the keyed refresh. One transaction: stream the source, hash each row, and against the diff state
    (`platform.sql_pull_rows`) UPDATE the changed rows in place (same id), INSERT the new, DELETE the absent — an
    unchanged row costs nothing, so the V003 audit trail reflects real change only. Keyless sources key by the
    content hash. Progress and the cancel check ride the second autocommit connection. The count guard and the
    all-refused abort are unchanged; a failure rolls everything back and the last good copy stays."""
    import math
    tbl = sql.SQL("assets.{}").format(sql.Identifier(layer_id))
    has_geom = geom is not None
    if not has_geom:
        insert = sql.SQL("INSERT INTO {tbl} (notes, props, created_by, updated_by) "
                         "VALUES (%(notes)s, %(props)s, %(actor)s, %(actor)s) RETURNING id").format(tbl=tbl)
        update = sql.SQL("UPDATE {tbl} SET notes = %(notes)s, props = %(props)s, updated_by = %(actor)s, updated_at = now() "
                         "WHERE id = %(id)s").format(tbl=tbl)
    else:
        geom_expr = sql.SQL("ST_Force2D(ST_Transform(ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), %(src)s), %(storage)s))")
        insert = sql.SQL("INSERT INTO {tbl} (geom, notes, props, created_by, updated_by) VALUES ({g}, %(notes)s, %(props)s, "
                         "%(actor)s, %(actor)s) RETURNING id").format(tbl=tbl, g=geom_expr)
        update = sql.SQL("UPDATE {tbl} SET geom = {g}, notes = %(notes)s, props = %(props)s, updated_by = %(actor)s, "
                         "updated_at = now() WHERE id = %(id)s").format(tbl=tbl, g=geom_expr)
    xi = names.index(geom["x"]) if has_geom else -1
    yi = names.index(geom["y"]) if has_geom else -1
    ki = names.index(key_column) if key_column else -1
    notes_i = next((i for i, p in enumerate(plan) if p.get("isNotes")), -1)

    def _coord(v: Any) -> float | None:
        n = coerce_value(v, data_type="number", temporal=False, utc=False, zone=zone)
        return float(n) if isinstance(n, int | float) and math.isfinite(float(n)) else None

    processed = inserted = updated = unchanged = deleted = failed = unplaced = 0
    samples: list[dict[str, Any]] = []
    actor_id = str(actor.id)
    pid = pull_id

    async def progress() -> None:
        async with pconn.cursor() as pc:
            await pc.execute("UPDATE platform.sql_pull_runs SET total_rows = %s, processed_rows = %s, inserted_rows = %s, "
                             "updated_rows = %s, unchanged_rows = %s, failed_rows = %s, error_sample = %s, updated_at = now() "
                             "WHERE id = %s", (src_counts.total, processed, inserted, updated, unchanged, failed, Jsonb(samples), run_id))

    async def canceled() -> bool:
        async with pconn.cursor() as pc:
            await pc.execute("SELECT status FROM platform.sql_pull_runs WHERE id = %s", (run_id,))
            st = await pc.fetchone()
        return st is None or st[0] == "canceled"

    async with conn.transaction():
        async with conn.cursor() as cur:
            await set_audit_context(cur, actor, "sql-pull")
            # the diff state: source_key -> (row_id, hash)
            await cur.execute("SELECT source_key, row_id, row_hash FROM platform.sql_pull_rows WHERE pull_id = %s", (pid,))
            existing: dict[str, tuple[str, str]] = {k: (str(rid), h) for k, rid, h in await cur.fetchall()}
            seen: set[str] = set()
            dup_counter: dict[str, int] = {}
            pending_ins: list[tuple[str, str, dict[str, Any]]] = []   # (key, hash, params)
            pending_upd: list[tuple[str, str, dict[str, Any]]] = []
            batch_n = 0

            async def flush() -> None:
                nonlocal inserted, updated, failed, batch_n
                # inserts, row by row (RETURNING id feeds the diff state); updates batched under a savepoint
                for key, h, params in pending_ins:
                    try:
                        async with conn.transaction():
                            await cur.execute(insert, params)
                            new_id = (await cur.fetchone())[0]
                            await cur.execute("INSERT INTO platform.sql_pull_rows (pull_id, source_key, row_id, row_hash, seen_run_id) "
                                              "VALUES (%s, %s, %s, %s, %s)", (pid, key, new_id, h, run_id))
                        inserted += 1
                    except (pg_errors.CheckViolation, pg_errors.DataError, pg_errors.InternalError_) as re:
                        failed += 1
                        if len(samples) < ERROR_SAMPLE_MAX:
                            samples.append({"row": params["_row"], "reason": str(re).splitlines()[0][:200]})
                for key, h, params in pending_upd:
                    try:
                        async with conn.transaction():
                            await cur.execute(update, params)
                            await cur.execute("UPDATE platform.sql_pull_rows SET row_hash = %s, seen_run_id = %s "
                                              "WHERE pull_id = %s AND source_key = %s", (h, run_id, pid, key))
                        updated += 1
                    except (pg_errors.CheckViolation, pg_errors.DataError, pg_errors.InternalError_) as re:
                        failed += 1
                        if len(samples) < ERROR_SAMPLE_MAX:
                            samples.append({"row": params["_row"], "reason": str(re).splitlines()[0][:200]})
                del pending_ins[:]
                del pending_upd[:]
                batch_n = 0
                await progress()

            idx = -1
            async for row in session.stream(schema, table, names):
                idx += 1
                processed += 1
                props: dict[str, Any] = {}
                notes_val = None
                for i, (p, v) in enumerate(zip(plan, row)):
                    cv = coerce_value(v, data_type=p["dataType"], temporal=p["temporal"], utc=p["utc"], zone=zone)
                    if i == notes_i:
                        notes_val = None if cv is None else str(cv)
                    else:
                        props[p["fieldKey"]] = cv
                x = y = None
                if has_geom:
                    rx, ry = row[xi], row[yi]
                    x, y = _coord(rx), _coord(ry)
                    if x is None or y is None:
                        failed += 1
                        if rx is None or ry is None:
                            unplaced += 1
                        if len(samples) < ERROR_SAMPLE_MAX:
                            samples.append({"row": idx, "reason": "missing or non-numeric coordinates"})
                        continue
                try:
                    validate_props(field_defs, props)
                except PropsError as e:
                    failed += 1
                    if len(samples) < ERROR_SAMPLE_MAX:
                        samples.append({"row": idx, "reason": "; ".join(str(x_) for x_ in e.errors)[:200]})
                    continue
                h = row_hash(props, notes_val, x, y)
                if ki >= 0 and row[ki] is not None:
                    key = str(row[ki])[:512]
                else:
                    # keyless (F10): the content IS the identity; duplicates get an ordinal suffix
                    n = dup_counter.get(h, 0)
                    dup_counter[h] = n + 1
                    key = h if n == 0 else f"{h}#{n}"
                if key in seen:
                    failed += 1
                    if len(samples) < ERROR_SAMPLE_MAX:
                        samples.append({"row": idx, "reason": f"duplicate key value in the source: {key[:60]}"})
                    continue
                seen.add(key)
                params: dict[str, Any] = {"props": Jsonb(props), "notes": notes_val, "actor": actor_id, "_row": idx}
                if has_geom:
                    params.update({"x": x, "y": y, "src": geom["srid"], "storage": storage_srid})
                cur_state = existing.get(key)
                if cur_state is None:
                    pending_ins.append((key, h, params))
                elif cur_state[1] == h:
                    unchanged += 1          # seen is tracked in memory: an unchanged row costs no write at all
                else:
                    params["id"] = cur_state[0]
                    pending_upd.append((key, h, params))
                batch_n += 1
                if batch_n >= _BATCH:
                    if await canceled():
                        raise _RunAbort("canceled")
                    await flush()
            await flush()

            # deletes: every key the source no longer has (and, once, any copy row the diff state never knew —
            # a pull made before s3 refreshes into the keyed world on its first s3 run)
            gone = [(k, rid) for k, (rid, _h) in existing.items() if k not in seen]
            if gone:
                ids = [rid for _k, rid in gone]
                await cur.execute(sql.SQL("DELETE FROM {tbl} WHERE id = ANY(%s::uuid[])").format(tbl=tbl), (ids,))
                deleted += cur.rowcount
                await cur.execute("DELETE FROM platform.sql_pull_rows WHERE pull_id = %s AND source_key = ANY(%s)", (pid, [k for k, _r in gone]))
            await cur.execute(sql.SQL("DELETE FROM {tbl} WHERE id NOT IN (SELECT row_id FROM platform.sql_pull_rows WHERE pull_id = %s)").format(tbl=tbl), (pid,))
            deleted += cur.rowcount

            # ★ Row counts are not success (trap 5.2): the copy must hold exactly the source's located rows minus the
            # refused ones, and — when nothing was refused — every column's non-null count must match.
            cols = sql.SQL(", ").join((sql.SQL("count(notes)") if p.get("isNotes")
                                       else sql.SQL("count(props->>{})").format(sql.Literal(p["fieldKey"]))) for p in plan)
            await cur.execute(sql.SQL("SELECT count(*), {cols} FROM {tbl}").format(cols=cols, tbl=tbl))
            trow = await cur.fetchone()
            target_total = int(trow[0])
            target_non_null = {p["name"]: int(n) for p, n in zip(plan, trow[1:])}
            if target_total != src_counts.total - (failed - unplaced):
                raise _RunAbort(f"row count mismatch: source {src_counts.total}, copied {target_total}, refused {failed} — last good copy kept")
            if src_counts.total > 0 and (inserted + updated + unchanged) == 0:
                why = samples[0]["reason"] if samples else "every row was refused"
                raise _RunAbort(f"every source row was refused ({why}) — last good copy kept")
            stats = {}
            for p in plan:
                s_n, t_n = src_counts.non_null.get(p["name"], 0), target_non_null.get(p["name"], 0)
                stats[p["name"]] = {"source": s_n, "target": t_n}
                if (failed - unplaced) == 0 and t_n != s_n:
                    raise _RunAbort(f"column '{p['name']}' lost values: {s_n} non-null in the source, {t_n} copied — last good copy kept")
            if await canceled():
                raise _RunAbort("canceled")
            await cur.execute("UPDATE platform.sql_pull_runs SET status = 'completed', total_rows = %s, processed_rows = %s, "
                              "inserted_rows = %s, updated_rows = %s, deleted_rows = %s, unchanged_rows = %s, failed_rows = %s, "
                              "error_sample = %s, column_stats = %s, finished_at = now(), updated_at = now() "
                              "WHERE id = %s AND status = 'running'",
                              (src_counts.total, processed, inserted, updated, deleted, unchanged, failed, Jsonb(samples), Jsonb(stats), run_id))
            # S3-C: success clears the stale posture
            await cur.execute("UPDATE platform.sql_pulls SET stale_since = NULL, last_error = NULL, last_success_at = now() WHERE id = %s", (pid,))
    log.info("sql_pull_run_completed", run_id=run_id, layer_id=layer_id, source=f"{schema}.{table}",
             total=src_counts.total, inserted=inserted, updated=updated, deleted=deleted, unchanged=unchanged, failed=failed)


async def cancel_run(conn: AsyncConnection, rid: UUID, actor: UUID) -> dict[str, Any] | None:
    async with conn.cursor() as cur:
        await cur.execute(f"UPDATE platform.sql_pull_runs SET status = 'canceled', finished_at = now(), updated_at = now() "
                          f"WHERE id = %s AND status IN ('pending', 'running') RETURNING {_RUN_COLS}", (str(rid),))
        r = await cur.fetchone()
    await conn.commit()
    return _run_dict(r) if r else None
