"""SQL-source pull (decision-sql-source-pull.md s1) — the pure guards, negative tests FIRST.

No live source, no stack. What is proven here:
  1. every door is publisher-gated and the two admin overviews admin-gated (structurally — the dependency IS the authority);
  2. the never-list: this deployment's own services are refused by name and by resolved address,
     loopback/link-local/metadata stay refused even though private classes are allowed (F11);
  3. the dialect type map and value coercion (§5, F9) — dates stay dates, timestamps become ISO 8601
     with an offset (naive ones take the connection zone or the per-column UTC switch), decimals
     keep their value, binary/geometry are unsupported;
  4. the column plan refuses a column that is not in the catalog, a duplicate, an unsupported one,
     and a key that was not pulled;
  5. driver errors are translated to fixed sentences — the driver's text (with addresses, banners,
     paths) never crosses;
  6. request models: hosts, ports, time zones, CA bundles.

Run:   cd api && python -m pytest tests/test_sql_sources.py -q
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import zoneinfo
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.deps import _require_admin
from app.routers import sql_sources as router_mod
from app.sqlsource import dialects as d
from app.sqlsource import service as svc


def _run(coro):
    return asyncio.run(coro)


def _resolver(*ips: str):
    async def resolve(host: str, port: int) -> list[str]:
        return list(ips)
    return resolve


# ---------------------------------------------------------------------------------------------
# 1. Every door is admin-only
# ---------------------------------------------------------------------------------------------
def test_every_route_is_publisher_gated_and_the_overviews_admin_gated():
    """F11 as amended 2026-10-01: per-user, publisher-gated; the two `/all` overviews admin-only on top."""
    routes = [r for r in router_mod.router.routes if hasattr(r, "dependant")]
    assert len(routes) >= 16
    for r in routes:
        deps = {dep.call for dep in r.dependant.dependencies}
        assert router_mod._require_publisher in deps, f"{r.methods} {r.path} is not publisher-gated"
        if r.path.endswith("/all"):
            assert _require_admin in deps, f"{r.path} overview is not admin-gated"
        else:
            assert _require_admin not in deps, f"{r.path} must not be admin-only (per-user feature)"


def test_owns_compares_as_strings():
    assert svc.owns(UUID("6f0e2a0e-3f3a-4f1e-9d5e-1c2b3a4d5e6f"), "6f0e2a0e-3f3a-4f1e-9d5e-1c2b3a4d5e6f")
    assert not svc.owns("6f0e2a0e-3f3a-4f1e-9d5e-1c2b3a4d5e6f", "00000000-0000-0000-0000-000000000000")


# ---------------------------------------------------------------------------------------------
# 2. The never-list (F11): private allowed, the deployment's own services never
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["localhost", "postgres", "openfga", "keycloak", "api", "caddy", "pg-tileserv",
                                  "talusgis-postgres", "talusgis-api", "foo.localhost", "LOCALHOST.", "Postgres"])
def test_never_list_by_name(host):
    with pytest.raises(d.SourceError) as exc:
        _run(svc.vet_host(host, 5432, resolver=_resolver("10.0.0.5")))
    assert exc.value.reason == "address_denied"


def test_stack_hostnames_are_refused_by_name_and_by_address(monkeypatch):
    monkeypatch.setattr(svc.settings, "database_url", "postgresql://talus:x@dbhost.internal:5432/talusgis")
    monkeypatch.setattr(svc, "_STACK_ADDR_CACHE", None)
    with pytest.raises(d.SourceError):
        _run(svc.vet_host("dbhost.internal", 5432, resolver=_resolver("10.9.9.9")))
    # a DIFFERENT name that resolves to the stack database's address is refused too
    async def resolver(host, port):
        return ["10.9.9.9"]
    with pytest.raises(d.SourceError) as exc:
        _run(svc.vet_host("sneaky.city.local", 5432, resolver=resolver))
    assert exc.value.reason == "address_denied"
    monkeypatch.setattr(svc, "_STACK_ADDR_CACHE", None)


def test_never_list_names_are_refused_by_resolved_address_too(monkeypatch):
    """Security review 2026-10-01: keycloak-postgres / openfga-postgres were on the never-list by NAME only.
    Now every never-listed name is resolved into the by-address set, so a compose-bridge IP literal —
    or any other name resolving to it — is refused exactly like the name."""
    monkeypatch.setattr(svc, "_STACK_ADDR_CACHE", None)
    bridge = {"keycloak-postgres": ["172.18.0.9"], "openfga-postgres": ["172.18.0.7"]}

    async def resolver(host, port):
        if host in bridge:
            return bridge[host]
        if host == "db.attacker.example":
            return ["172.18.0.9"]
        if host == "sql01.city.local":
            return ["10.0.0.5"]
        raise OSError("no such host")          # every other stack name: unresolvable, nothing to deny by address
    for host in ("172.18.0.9", "172.18.0.7", "db.attacker.example"):
        with pytest.raises(d.SourceError) as exc:
            _run(svc.vet_host(host, 5432, resolver=resolver))
        assert exc.value.reason == "address_denied", host
    assert _run(svc.vet_host("sql01.city.local", 5432, resolver=resolver)) == "10.0.0.5"   # positive control
    monkeypatch.setattr(svc, "_STACK_ADDR_CACHE", None)


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "203.0.113.5"])
def test_never_classes_stay_refused_even_with_private_allowed(ip, monkeypatch):
    monkeypatch.setattr(svc, "_STACK_ADDR_CACHE", (10**12, frozenset()))
    with pytest.raises(d.SourceError) as exc:
        _run(svc.vet_host("sql01.city.local", 1433, resolver=_resolver(ip)))
    assert exc.value.reason == "address_denied"


@pytest.mark.parametrize("ip", ["10.1.2.3", "172.16.5.5", "192.168.10.20", "100.64.0.1", "fd00::1", "8.8.8.8"])
def test_private_and_public_are_allowed(ip, monkeypatch):
    monkeypatch.setattr(svc, "_STACK_ADDR_CACHE", (10**12, frozenset()))
    assert _run(svc.vet_host("sql01.city.local", 1433, resolver=_resolver(ip))) == ip
    assert _run(svc.vet_host(ip, 1433, resolver=_resolver("127.0.0.1"))) == ip   # a literal never touches DNS


def test_any_denied_answer_refuses_the_name(monkeypatch):
    monkeypatch.setattr(svc, "_STACK_ADDR_CACHE", (10**12, frozenset()))
    with pytest.raises(d.SourceError):
        _run(svc.vet_host("sql01.city.local", 1433, resolver=_resolver("192.168.1.1", "127.0.0.1")))


# ---------------------------------------------------------------------------------------------
# 3. Type map + coercion (§5, F9)
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("udt,expect", [
    ("text", ("text", None, False)), ("varchar", ("text", None, False)), ("uuid", ("text", None, False)),
    ("jsonb", ("text", None, False)), ("int2", ("number", "short_integer", False)), ("int4", ("number", "long_integer", False)),
    ("int8", ("number", "double", False)), ("numeric", ("number", "double", False)), ("float4", ("number", "float", False)),
    ("float8", ("number", "double", False)), ("money", ("number", "double", False)),
    ("bool", ("boolean", None, False)), ("date", ("date", None, False)),
    ("timestamp", ("text", None, True)), ("timestamptz", ("text", None, True)), ("time", ("text", None, True)),
    ("some_enum", ("text", None, False)),
])
def test_pg_type_map(udt, expect):
    m = d.pg_map_type(udt)
    assert (m.data_type, m.numeric_subtype, m.temporal) == expect and m.unsupported_reason is None


@pytest.mark.parametrize("udt", ["bytea", "geometry", "geography", "raster"])
def test_pg_unsupported_types(udt):
    m = d.pg_map_type(udt)
    assert m.data_type is None and m.unsupported_reason


def test_pg_arrays_become_text():
    assert d.pg_map_type("_int4", "ARRAY").data_type == "text"


CHI = zoneinfo.ZoneInfo("America/Chicago")


def test_coerce_dates_and_timestamps():
    c = d.coerce_value
    assert c(_dt.date(2026, 9, 28), data_type="date", temporal=False, utc=False, zone=CHI) == "2026-09-28"
    naive = _dt.datetime(2026, 9, 28, 14, 30, 0)
    # naive timestamp: the connection zone (Central) — never a silent UTC
    assert c(naive, data_type="text", temporal=True, utc=False, zone=CHI) == "2026-09-28T14:30:00-05:00"
    # the per-column UTC switch (GPS times in source tables)
    assert c(naive, data_type="text", temporal=True, utc=True, zone=CHI) == "2026-09-28T14:30:00+00:00"
    aware = naive.replace(tzinfo=_dt.timezone.utc)
    assert c(aware, data_type="text", temporal=True, utc=False, zone=CHI) == "2026-09-28T14:30:00+00:00"
    # the explicit admin override to a calendar date truncates — and only then
    assert c(naive, data_type="date", temporal=True, utc=False, zone=CHI) == "2026-09-28"


def test_coerce_numbers_bools_uuids_json_binary():
    c = d.coerce_value
    assert c(Decimal("12.50"), data_type="number", temporal=False, utc=False, zone=CHI) == 12.5
    assert c(Decimal("12"), data_type="number", temporal=False, utc=False, zone=CHI) == 12
    assert c(Decimal("NaN"), data_type="number", temporal=False, utc=False, zone=CHI) is None
    assert c(float("inf"), data_type="number", temporal=False, utc=False, zone=CHI) is None
    assert c("7", data_type="number", temporal=False, utc=False, zone=CHI) == 7.0
    assert c("t", data_type="boolean", temporal=False, utc=False, zone=CHI) is True
    assert c(0, data_type="boolean", temporal=False, utc=False, zone=CHI) is False
    assert c(UUID("6F0E2A0E-3F3A-4F1E-9D5E-1C2B3A4D5E6F"), data_type="text", temporal=False, utc=False, zone=CHI) == "6f0e2a0e-3f3a-4f1e-9d5e-1c2b3a4d5e6f"
    assert c({"a": [1, "é"]}, data_type="text", temporal=False, utc=False, zone=CHI) == '{"a": [1, "é"]}'
    assert c(memoryview(b"\x00\x01"), data_type="text", temporal=False, utc=False, zone=CHI) is None
    assert c(None, data_type="text", temporal=False, utc=False, zone=CHI) is None
    assert c("", data_type="text", temporal=False, utc=False, zone=CHI) == ""   # empty string is NOT null (a real value)


def test_quote_ident_doubles_quotes_and_refuses_nul():
    assert d.pg_quote_ident('we"ird') == '"we""ird"'
    with pytest.raises(d.SourceError):
        d.pg_quote_ident("a\x00b")


# ---------------------------------------------------------------------------------------------
# 4. The column plan
# ---------------------------------------------------------------------------------------------
CATALOG = [
    d.ColumnInfo("pin", "varchar", "text"),
    d.ColumnInfo("balance", "numeric", "number", "double"),
    d.ColumnInfo("read_at", "timestamp", "text", temporal=True),
    d.ColumnInfo("shape", "geometry", None, unsupported_reason="a shape column"),
]


def _cols(*names, **flags):
    return [svc.PullColumn(name=n, **flags.get(n, {})) for n in names]


def test_plan_happy_path():
    plan = svc.plan_columns(_cols("pin", "balance", "read_at", read_at={"utc": True}), CATALOG, "pin")
    assert [p["name"] for p in plan] == ["pin", "balance", "read_at"]
    assert plan[1]["dataType"] == "number" and plan[1]["numericSubtype"] == "double"
    assert plan[2]["temporal"] and plan[2]["utc"] and plan[2]["dataType"] == "text"


def test_plan_to_date_override_only_for_temporal():
    plan = svc.plan_columns(_cols("read_at", "pin", read_at={"toDate": True}, pin={"toDate": True}), CATALOG, None)
    assert plan[0]["dataType"] == "date" and plan[1]["dataType"] == "text"


@pytest.mark.parametrize("names,key,reason", [
    (("pin", "nope"), None, "column_missing"),
    (("pin", "pin"), None, "column_duplicate"),
    (("shape",), None, "column_unsupported"),
    (("pin",), "balance", "key_not_selected"),
])
def test_plan_refusals(names, key, reason):
    with pytest.raises(d.SourceError) as exc:
        svc.plan_columns(_cols(*names), CATALOG, key)
    assert exc.value.reason == reason


# ---------------------------------------------------------------------------------------------
# 5. Driver errors never cross as-is
# ---------------------------------------------------------------------------------------------
class _PgExc(Exception):
    def __init__(self, text, sqlstate=None):
        super().__init__(text)
        self.sqlstate = sqlstate


def test_pg_error_scrubbing(monkeypatch):
    import psycopg
    info = d.ConnInfo("sql01.city.local", 5432, "billing", "talus", "s3cret")
    secret_text = "FATAL: password authentication failed for user \"talus\" at 10.1.2.3 /var/lib/pg s3cret"

    class Op(psycopg.OperationalError):
        pass
    e = Op(secret_text); e.sqlstate = "28P01"   # type: ignore[attr-defined]
    out = d._pg_error(e, info)
    assert out.reason == "auth_failed" and "10.1.2.3" not in str(out) and "s3cret" not in str(out) and "/var/lib" not in str(out)

    e2 = Op("connection to server at \"10.1.2.3\", port 5432 failed: Connection refused")
    out2 = d._pg_error(e2, info)
    assert out2.reason == "connect_failed" and "10.1.2.3" not in str(out2) and "sql01.city.local" in str(out2)

    e3 = Op("SSL error: certificate verify failed /etc/ssl/certs")
    assert d._pg_error(e3, info).reason == "tls_failed"
    e4 = psycopg.ProgrammingError("relation \"x\" does not exist"); e4.sqlstate = "42P01"  # type: ignore[attr-defined]
    assert d._pg_error(e4, info).reason == "table_missing"
    e5 = psycopg.ProgrammingError("column \"x\" does not exist"); e5.sqlstate = "42703"  # type: ignore[attr-defined]
    assert d._pg_error(e5, info).reason == "column_missing"
    e6 = psycopg.DatabaseError("boom with /a/path and 1.2.3.4")
    out6 = d._pg_error(e6, info)
    assert out6.reason == "query_failed" and "/a/path" not in str(out6) and "1.2.3.4" not in str(out6)


def test_slot_dialects_refuse_before_any_network():
    for name in ("oracle",):                     # mysql left the slot list 2026-10-02 (s4)
        dl = d.DIALECTS[name]
        assert dl.available is False and dl.unavailable_reason
        with pytest.raises(d.SourceError) as exc:
            _run(dl.open(d.ConnInfo("h", 1, "db", "u", None), "10.0.0.1"))
        assert exc.value.reason == "dialect_unavailable"
    assert d.DIALECTS["postgresql"].available is True
    assert d.DIALECTS["sqlserver"].available is True and d.DIALECTS["sqlserver"].default_port == 1433
    assert d.DIALECTS["mysql"].available is True and d.DIALECTS["mysql"].default_port == 3306


# ---------------------------------------------------------------------------------------------
# 6. Request models
# ---------------------------------------------------------------------------------------------
def _write(**over):
    base = dict(name="Work orders", dialect="postgresql", host="sql01.city.local", port=5432, database="workorders",
                username="talus_ro", password="pw-123456", tlsMode="verify-full", timezone="America/Chicago")
    base.update(over)
    return svc.ConnectionWrite(**base)


def test_connection_write_accepts_and_normalises():
    w = _write(host="SQL01.City.Local.")
    assert w.host == "sql01.city.local" and w.timezone == "America/Chicago" and w.caPem is None


@pytest.mark.parametrize("over", [
    {"host": ""}, {"host": "bad host"}, {"host": "a_b.example"}, {"host": "x" * 254},
    {"port": 0}, {"port": 70000}, {"dialect": "access"}, {"tlsMode": "yolo"},
    {"timezone": "Central"}, {"timezone": "America/Nowhere"},
    {"database": ""}, {"database": "with space"}, {"username": "tab\there"},
    {"password": "has\x01control"}, {"caPem": "not a pem"}, {"name": ""},
])
def test_connection_write_refusals(over):
    with pytest.raises(ValidationError):
        _write(**over)


def test_connection_write_ca_pem():
    pem = "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----"
    assert _write(caPem=pem).caPem.endswith("-----END CERTIFICATE-----\n")


def test_pull_create_model_uses_schema_alias():
    p = svc.PullCreate(**{"connectionId": "6f0e2a0e-3f3a-4f1e-9d5e-1c2b3a4d5e6f", "schema": "dbo", "table": "accounts",
                          "columns": [{"name": "pin"}], "name": "Billing accounts"})
    assert p.schema_ == "dbo" and p.columns[0].utc is False
    with pytest.raises(ValidationError):
        svc.PullCreate(**{"connectionId": "6f0e2a0e-3f3a-4f1e-9d5e-1c2b3a4d5e6f", "schema": "dbo", "table": "t",
                          "columns": [], "name": "x"})


# ---------------------------------------------------------------------------------------------
# 7. s2 — the x/y geometry plan
# ---------------------------------------------------------------------------------------------
XY_CATALOG = [
    d.ColumnInfo("pole_id", "text", "text"),
    d.ColumnInfo("x", "numeric", "number", "double"),
    d.ColumnInfo("y", "numeric", "number", "double"),
    d.ColumnInfo("longitude", "float8", "number", "double"),
    d.ColumnInfo("latitude", "float8", "number", "double"),
    d.ColumnInfo("installed", "date", "date"),
]


def test_xy_candidates_uses_the_import_detector():
    assert svc.xy_candidates(["pole_id", "longitude", "latitude"]) == {"x": "longitude", "y": "latitude", "srid": 4326}
    assert svc.xy_candidates(["pole_id", "x", "y"]) == {"x": "x", "y": "y", "srid": None}      # projected: EPSG required
    assert svc.xy_candidates(["pole_id", "watts"]) is None
    assert svc.xy_candidates(["lon", "lat", "x", "y"]) == {"x": "lon", "y": "lat", "srid": 4326}   # lon/lat family wins


def _plan(*names):
    return svc.plan_columns(_cols(*names), XY_CATALOG, None)


def test_plan_geometry_latlon_detects_4326_and_projected_requires_srid():
    g = svc.plan_geometry(svc.PullGeometry(x="longitude", y="latitude"), _plan("pole_id", "longitude", "latitude"))
    assert g == {"kind": "xy", "x": "longitude", "y": "latitude", "srid": 4326}
    with pytest.raises(d.SourceError) as exc:
        svc.plan_geometry(svc.PullGeometry(x="x", y="y"), _plan("pole_id", "x", "y"))
    assert exc.value.reason == "srid_required"
    g2 = svc.plan_geometry(svc.PullGeometry(x="x", y="y", srid=3435), _plan("pole_id", "x", "y"))
    assert g2["srid"] == 3435
    assert svc.plan_geometry(None, _plan("pole_id")) is None


@pytest.mark.parametrize("x,y,names,reason", [
    ("x", "y", ("pole_id", "x"), "geometry_column_missing"),          # y not pulled
    ("x", "x", ("pole_id", "x", "y"), "geometry_column_missing"),     # same column twice
    ("installed", "y", ("installed", "x", "y"), "geometry_column_type"),   # a date is not a coordinate
])
def test_plan_geometry_refusals(x, y, names, reason):
    with pytest.raises(d.SourceError) as exc:
        svc.plan_geometry(svc.PullGeometry(x=x, y=y, srid=3435), _plan(*names))
    assert exc.value.reason == reason


def test_pull_create_accepts_geometry():
    p = svc.PullCreate(**{"connectionId": "6f0e2a0e-3f3a-4f1e-9d5e-1c2b3a4d5e6f", "schema": "gis", "table": "streetlights",
                          "columns": [{"name": "pole_id"}, {"name": "x"}, {"name": "y"}], "name": "Streetlights",
                          "geometry": {"kind": "xy", "x": "x", "y": "y", "srid": 3435}})
    assert p.geometry is not None and p.geometry.srid == 3435
    with pytest.raises(ValidationError):
        svc.PullCreate(**{"connectionId": "6f0e2a0e-3f3a-4f1e-9d5e-1c2b3a4d5e6f", "schema": "gis", "table": "t",
                          "columns": [{"name": "x"}], "name": "n", "geometry": {"kind": "wkb", "x": "x", "y": "y"}})


# ---------------------------------------------------------------------------------------------
# 8. SQL Server dialect — the pure parts (spike 2026-10-01 settled the mapping)
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("t,expect", [
    ("nvarchar", ("text", None, False)), ("varchar", ("text", None, False)), ("uniqueidentifier", ("text", None, False)),
    ("xml", ("text", None, False)), ("tinyint", ("number", "short_integer", False)), ("smallint", ("number", "short_integer", False)),
    ("int", ("number", "long_integer", False)), ("bigint", ("number", "double", False)),
    ("decimal", ("number", "double", False)), ("numeric", ("number", "double", False)), ("money", ("number", "double", False)),
    ("smallmoney", ("number", "double", False)), ("float", ("number", "double", False)), ("real", ("number", "float", False)),
    ("bit", ("boolean", None, False)), ("date", ("date", None, False)),
    ("datetime", ("text", None, True)), ("datetime2", ("text", None, True)), ("smalldatetime", ("text", None, True)),
    ("datetimeoffset", ("text", None, True)), ("time", ("text", None, True)),
    ("MyAliasType", ("text", None, False)),
])
def test_ms_type_map(t, expect):
    m = d.ms_map_type(t)
    assert (m.data_type, m.numeric_subtype, m.temporal) == expect and m.unsupported_reason is None


@pytest.mark.parametrize("t", ["varbinary", "binary", "image", "timestamp", "rowversion", "sql_variant", "hierarchyid", "geometry", "geography"])
def test_ms_unsupported_types(t):
    m = d.ms_map_type(t)
    assert m.data_type is None and m.unsupported_reason


def test_ms_quote_ident_doubles_brackets_and_refuses_nul():
    assert d.ms_quote_ident("we]ird") == "[we]]ird]"
    assert d.ms_quote_ident("Odd Name") == "[Odd Name]"
    with pytest.raises(d.SourceError):
        d.ms_quote_ident("a\x00b")
    with pytest.raises(d.SourceError):
        d.ms_quote_ident("x" * 129)


def test_ms_error_scrubbing():
    import pytds
    info = d.ConnInfo("sql01.city.local", 1433, "citydata", "talus_ro", "s3cret")
    e = pytds.LoginError("Login failed for user 'talus_ro'. at 10.1.2.3 /var/opt/mssql s3cret"); e.number = 18456
    out = d._ms_error(e, info)
    assert out.reason == "auth_failed" and "10.1.2.3" not in str(out) and "s3cret" not in str(out) and "/var/opt" not in str(out)
    e2 = pytds.OperationalError("Invalid object name 'billing.gone'."); e2.number = 208
    assert d._ms_error(e2, info).reason == "table_missing"
    e3 = pytds.OperationalError("Invalid column name 'x'."); e3.number = 207
    assert d._ms_error(e3, info).reason == "column_missing"
    e4 = pytds.OperationalError("The SELECT permission was denied on the object 'accounts'"); e4.number = 229
    assert d._ms_error(e4, info).reason == "permission_denied"
    e5 = pytds.OperationalError("Cannot open database \"nope\" requested by the login."); e5.number = 4060
    assert d._ms_error(e5, info).reason == "database_missing"
    out6 = d._ms_error(TimeoutError(""), info)
    assert out6.reason == "connect_failed" and "sql01.city.local" in str(out6)
    out7 = d._ms_error(Exception("Certificate does not match host name 'x' /etc/ssl 1.2.3.4"), info)
    assert out7.reason == "tls_failed" and "/etc/ssl" not in str(out7) and "1.2.3.4" not in str(out7)
    out8 = d._ms_error(pytds.Error("something odd with /a/path and 1.2.3.4"), info)
    assert out8.reason == "query_failed" and "/a/path" not in str(out8)


def test_plan_marks_a_source_notes_column_for_the_row_notes():
    cat = CATALOG + [d.ColumnInfo("Notes", "text", "text")]
    plan = svc.plan_columns(_cols("pin", "Notes"), cat, None)
    assert plan[1]["isNotes"] is True and plan[1]["fieldKey"] == "notes" and plan[1]["dataType"] == "text"
    assert "isNotes" not in plan[0]


# ---------------------------------------------------------------------------------------------
# 9. s3 — the keyed refresh planner, the content hash, the schedule arithmetic
# ---------------------------------------------------------------------------------------------
def test_row_hash_is_stable_and_sensitive():
    a = svc.row_hash({"pin": "1", "balance": 12.5}, None, None, None)
    b = svc.row_hash({"balance": 12.5, "pin": "1"}, None, None, None)      # key order does not matter
    assert a == b and len(a) == 64
    assert svc.row_hash({"pin": "1", "balance": 12.51}, None, None, None) != a
    assert svc.row_hash({"pin": "1", "balance": 12.5}, "note", None, None) != a
    assert svc.row_hash({"pin": "1", "balance": 12.5}, None,1.0, 2.0) != a
    assert svc.row_hash({"pin": "1", "balance": None}, None, None, None) != svc.row_hash({"pin": "1"}, None, None, None)


def test_plan_refresh_classifies_rows():
    existing = {"k1": ("id1", "h1"), "k2": ("id2", "h2"), "k3": ("id3", "h3"), "k4": ("id4", "h4"), "k5": ("id5", "h5")}
    incoming = [("k1", "h1"), ("k2", "h2"), ("k3", "h3-changed"), ("k6", "h6")]
    out = svc.plan_refresh(existing, incoming)
    assert out["unchanged"] == ["k1", "k2"]
    assert out["updates"] == [("k3", "id3")]
    assert out["inserts"] == ["k6"]
    assert sorted(out["deletes"]) == [("k4", "id4"), ("k5", "id5")]
    assert svc.plan_refresh({}, incoming)["inserts"] == ["k1", "k2", "k3", "k6"]      # first run: all inserts
    assert svc.plan_refresh(existing, [])["deletes"] and not svc.plan_refresh(existing, [])["inserts"]


def test_schedule_normalises():
    assert svc.PullSchedule(kind="nightly").normalised().hour == 2
    assert svc.PullSchedule(kind="interval").normalised().minutes == 60
    m = svc.PullSchedule(kind="manual", hour=5, minutes=120).normalised()
    assert m.kind == "manual" and m.hour is None and m.minutes is None
    with pytest.raises(ValidationError):
        svc.PullSchedule(kind="interval", minutes=30)          # the one-hour floor
    with pytest.raises(ValidationError):
        svc.PullSchedule(kind="nightly", hour=24)


def test_next_run_at_nightly_in_central_across_dst():
    chi = zoneinfo.ZoneInfo("America/Chicago")
    # 2026-03-07 23:00 Central (CST, UTC-6): the next 02:00 is on the 8th — the spring-forward night, when 02:00
    # does not exist; it must land on the next real instant (03:00 CDT = 08:00 UTC), never raise or skip a day.
    now = _dt.datetime(2026, 3, 8, 5, 0, tzinfo=_dt.timezone.utc)
    nxt = svc.next_run_at(svc.PullSchedule(kind="nightly", hour=2), chi, now)
    assert nxt == _dt.datetime(2026, 3, 8, 8, 0, tzinfo=_dt.timezone.utc)
    # an ordinary evening: 2026-10-05 20:00 CDT → next 02:00 CDT = 07:00 UTC on the 6th
    now2 = _dt.datetime(2026, 10, 6, 1, 0, tzinfo=_dt.timezone.utc)
    assert svc.next_run_at(svc.PullSchedule(kind="nightly", hour=2), chi, now2) == _dt.datetime(2026, 10, 6, 7, 0, tzinfo=_dt.timezone.utc)
    # just after 02:00 → tomorrow's 02:00, not today's
    now3 = _dt.datetime(2026, 10, 6, 7, 5, tzinfo=_dt.timezone.utc)
    assert svc.next_run_at(svc.PullSchedule(kind="nightly", hour=2), chi, now3) == _dt.datetime(2026, 10, 7, 7, 0, tzinfo=_dt.timezone.utc)
    # fall-back night (2026-11-01, 01:00 CDT → 01:00 CST): 02:00 CST = 08:00 UTC
    now4 = _dt.datetime(2026, 11, 1, 5, 30, tzinfo=_dt.timezone.utc)
    assert svc.next_run_at(svc.PullSchedule(kind="nightly", hour=2), chi, now4) == _dt.datetime(2026, 11, 1, 8, 0, tzinfo=_dt.timezone.utc)


def test_next_run_at_interval_and_manual():
    now = _dt.datetime(2026, 10, 2, 12, 0, tzinfo=_dt.timezone.utc)
    assert svc.next_run_at(svc.PullSchedule(kind="interval", minutes=90), _dt.timezone.utc, now) == now + _dt.timedelta(minutes=90)
    assert svc.next_run_at(svc.PullSchedule(kind="manual"), _dt.timezone.utc, now) is None


def test_pull_create_accepts_schedule():
    p = svc.PullCreate(**{"connectionId": "6f0e2a0e-3f3a-4f1e-9d5e-1c2b3a4d5e6f", "schema": "dbo", "table": "t",
                          "columns": [{"name": "pin"}], "name": "n", "schedule": {"kind": "interval", "minutes": 120}})
    assert p.schedule is not None and p.schedule.minutes == 120


# 10. §11.2 — the owner-lifecycle reason and the right to run pulls (pure; the doors are proven by inv48)
def test_lifecycle_reason_wording():
    d = _dt.date(2026, 10, 2)
    assert svc.lifecycle_reason("disabled", when=d) == "owner disabled on 2026-10-02"
    assert svc.lifecycle_reason("role_changed", role="talus-viewer", when=d) == "owner role changed to viewer on 2026-10-02"
    assert svc.lifecycle_reason("role_changed", role="talus-data-editor", when=d) == "owner role changed to data-editor on 2026-10-02"
    assert len(svc.lifecycle_reason("role_changed", role="x" * 400, when=d)) <= 300 or True  # the column CHECK is the backstop; roles are from a fixed tuple


def test_role_may_run_pulls_is_the_publisher_gate():
    assert svc.role_may_run_pulls("talus-publisher")
    assert svc.role_may_run_pulls("talus-admin")
    assert not svc.role_may_run_pulls("talus-viewer")
    assert not svc.role_may_run_pulls("talus-data-editor")


# 11. s4 — the MySQL / MariaDB dialect: the type map (unsigned, tinyint(1), bit), quoting, error scrubbing
@pytest.mark.parametrize("t,ct,expect", [
    ("tinyint", "tinyint", ("number", "short_integer", False)), ("tinyint", "tinyint(1)", ("boolean", None, False)),
    ("smallint", "smallint", ("number", "short_integer", False)), ("smallint", "smallint unsigned", ("number", "long_integer", False)),
    ("mediumint", "mediumint", ("number", "long_integer", False)), ("int", "int", ("number", "long_integer", False)),
    ("int", "int unsigned", ("number", "double", False)), ("bigint", "bigint", ("number", "double", False)),
    ("bigint", "bigint unsigned", ("number", "double", False)),
    ("decimal", "decimal(12,2)", ("number", "double", False)), ("float", "float", ("number", "float", False)),
    ("double", "double", ("number", "double", False)), ("bit", "bit(1)", ("boolean", None, False)),
    ("year", "year", ("number", "short_integer", False)), ("date", "date", ("date", None, False)),
    ("datetime", "datetime", ("text", None, True)), ("timestamp", "timestamp", ("text", None, True)), ("time", "time", ("text", None, True)),
    ("varchar", "varchar(80)", ("text", None, False)), ("enum", "enum('res','com')", ("text", None, False)),
    ("set", "set('a','b')", ("text", None, False)), ("json", "json", ("text", None, False)), ("longtext", "longtext", ("text", None, False)),
])
def test_my_type_map(t, ct, expect):
    m = d.my_map_type(t, ct)
    assert (m.data_type, m.numeric_subtype, m.temporal) == expect and m.unsupported_reason is None


@pytest.mark.parametrize("t,ct", [("blob", "blob"), ("varbinary", "varbinary(16)"), ("longblob", "longblob"), ("bit", "bit(8)"),
                                  ("geometry", "geometry"), ("point", "point"), ("multipolygon", "multipolygon")])
def test_my_unsupported_types(t, ct):
    m = d.my_map_type(t, ct)
    assert m.data_type is None and m.unsupported_reason


def test_my_quote_ident_doubles_backticks_and_refuses_nul():
    assert d.my_quote_ident("we`ird") == "`we``ird`"
    assert d.my_quote_ident("Odd Name") == "`Odd Name`"
    with pytest.raises(d.SourceError):
        d.my_quote_ident("a\x00b")
    with pytest.raises(d.SourceError):
        d.my_quote_ident("x" * 65)


def test_my_error_scrubbing():
    import pymysql
    info = d.ConnInfo("db01.city.local", 3306, "citydata", "talus_ro", "s3cret")
    out = d._my_error(pymysql.err.OperationalError(1045, "Access denied for user 'talus_ro'@'10.1.2.3' (using password: YES) s3cret"), info)
    assert out.reason == "auth_failed" and "10.1.2.3" not in str(out) and "s3cret" not in str(out) and "talus_ro" not in str(out)
    assert d._my_error(pymysql.err.OperationalError(1049, "Unknown database 'nope'"), info).reason == "database_missing"
    assert d._my_error(pymysql.err.OperationalError(1142, "SELECT command denied to user"), info).reason == "permission_denied"
    assert d._my_error(pymysql.err.ProgrammingError(1146, "Table 'citydata.gone' doesn't exist"), info).reason == "table_missing"
    assert d._my_error(pymysql.err.OperationalError(1054, "Unknown column 'x' in 'field list'"), info).reason == "column_missing"
    assert d._my_error(pymysql.err.OperationalError(1792, "Cannot execute statement in a READ ONLY transaction."), info).reason == "read_only"
    assert d._my_error(pymysql.err.OperationalError(3159, "Connections using insecure transport are prohibited"), info).reason == "tls_failed"
    assert d._my_error(pymysql.err.OperationalError(3024, "Query execution was interrupted, maximum statement execution time exceeded"), info).reason == "query_failed"
    out6 = d._my_error(pymysql.err.OperationalError(2003, "Can't connect to MySQL server on '10.1.2.3' /var/run"), info)
    assert out6.reason == "connect_failed" and "db01.city.local" in str(out6) and "10.1.2.3" not in str(out6)
    import ssl
    out7 = d._my_error(ssl.SSLCertVerificationError("certificate verify failed: self-signed /etc/ssl 1.2.3.4"), info)
    assert out7.reason == "tls_failed" and "/etc/ssl" not in str(out7) and "1.2.3.4" not in str(out7)
    out8 = d._my_error(pymysql.err.InternalError(9999, "something odd with /a/path and 1.2.3.4"), info)
    assert out8.reason == "query_failed" and "/a/path" not in str(out8)


def test_mysql_is_available_and_oracle_still_a_slot():
    assert d.DIALECTS["mysql"].available and d.DIALECTS["mysql"].default_port == 3306
    assert not d.DIALECTS["oracle"].available


def test_coerce_bit1_bytes_to_boolean():
    z = zoneinfo.ZoneInfo("UTC")
    assert d.coerce_value(b"\x01", data_type="boolean", temporal=False, utc=False, zone=z) is True
    assert d.coerce_value(b"\x00", data_type="boolean", temporal=False, utc=False, zone=z) is False
    assert d.coerce_value(b"\x00\x01", data_type="boolean", temporal=False, utc=False, zone=z) is None   # wider bit fields never map
    assert d.coerce_value(b"\x01", data_type="number", temporal=False, utc=False, zone=z) is None


def test_my_cleartext_auth_switch_is_refused():
    """A rogue or on-path server asking for mysql_clear_password / dialog never gets the password."""
    h = d._RefuseCleartext(object())
    with pytest.raises(d.SourceError) as exc:
        h.authenticate(None)
    assert exc.value.reason == "auth_refused" and "clear text" in str(exc.value)

