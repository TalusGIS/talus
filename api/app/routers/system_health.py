"""Admin → System health (decision-system-health.md) — the one-glance,
admin-gated answer to "is my portal healthy?": live service probes + the operational state the
platform already records (backups, maintenance recency, storage, version/migration head, last
restore drill). STATUS, not a control plane — no remediation, no alerting, no history; Prometheus/
Grafana stay the operator-tier answer, this is the product-tier glance (operational
legibility as product).

Disclosure calculus (§5): the response names internal services and the backup destination, so the
_require_admin gate IS the control; probe errors are scrubbed to a fixed reason set — never a URL,
connection string, or traceback. Probe targets come ONLY from settings (no caller input → no SSRF
surface), run concurrently with a hard per-probe timeout, and a dead dependency renders a red row —
it must never hang or error the page. One honest bound on that promise: _require_admin itself runs
the users-row gate (auth.get_current_user → get_db), so a FULLY dead postgres 500s at auth before
this handler runs — fail-closed identity outranks fail-open display, and that is the right order.
The manual (non-Depends) acquisition below covers the narrower cases: postgres dying between auth
and the probes, and a pool that can hand auth a connection but hiccups on the next one.
"""
import asyncio
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog
from cryptography import x509
from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel

from .. import db as db_module
from ..backup_settings import get_effective_backup_settings
from ..config import settings
from ..deps import _require_admin
from ..maintenance import registered_job_names
from ..storage import get_media_storage
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["system-health"])

_PROBE_TIMEOUT = 2.0   # seconds, hard, per probe — worst-case page latency ≈ one timeout, not six
_SLOW_MS = 1000        # up-but-slow ⇒ amber
# The certificate and clock probes' thresholds.
_CERT_AMBER = timedelta(days=14)      # a public (ACME) certificate inside two weeks of expiry ⇒ amber
_CERT_AMBER_INTERNAL = timedelta(hours=1)  # Caddy's internal CA issues ~12 h leaves and renews them itself:
                                            # the short window is not a warning — only a renewal that LAPSED is
_CLOCK_RED_S = 60.0    # past this, JWT iat/exp checks and certificate validity start failing


class Probe(BaseModel):
    name: str
    status: str                # 'up' | 'slow' | 'down'
    latencyMs: int | None
    error: str | None          # scrubbed fixed reason — NEVER a URL/connection string/traceback
    detail: str | None = None  # informational, fixed vocabulary + scalars ("expires in 83 days", "database +0.1 s")


class BackupState(BaseModel):
    lastOkAt: str | None
    target: str | None
    scheduledEnabled: bool | None


class JobState(BaseModel):
    job: str
    lastRunAt: str | None
    lastStatus: str | None


class StorageState(BaseModel):
    dbBytes: int | None
    mediaBytes: int | None
    mediaObjects: int | None
    mediaAsOf: str | None      # the cache job's run time — honesty about staleness


class VersionState(BaseModel):
    service: str
    migrationHead: str | None


class JobFailure(BaseModel):
    """Consecutive-failure run for one job: everything since its last `ok` (F5)."""
    consecutive: int
    failingSince: str | None


class Issue(BaseModel):
    """One thing wrong with the SYSTEM, as opposed to one process being down.

    `summary` is composed from FIXED strings plus scalars this endpoint already returns (job name,
    timestamp, count). It must NEVER carry `maintenance_runs.detail` or an exception string — see
    the module docstring's disclosure calculus and `_scrub`. The worked example is the 2026-07-12
    DR failure, whose `detail` quoted a `LOCK TABLE` enumerating internal table names.
    """
    key: str
    severity: str              # 'warn' | 'critical'
    summary: str


class SystemHealth(BaseModel):
    generatedAt: str
    probes: list[Probe]
    backup: BackupState
    maintenance: list[JobState]
    storage: StorageState
    version: VersionState
    restoreDrillLastOkAt: str | None
    issues: list[Issue]


def _scrub(exc: BaseException) -> str:
    """Exception → a fixed, disclosure-safe reason. str(exc) is deliberately never used — httpx and
    urllib3 messages embed the target URL, which is exactly what must not escape (§5)."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, (httpx.ConnectError, ConnectionError, OSError)):
        return "connection refused"
    return "unreachable"


async def _probe(name: str, awaitable: Any) -> Probe:
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(awaitable, timeout=_PROBE_TIMEOUT)
    except asyncio.CancelledError:  # never swallow cancellation (client disconnect)
        raise
    except BaseException as exc:  # noqa: BLE001 — a probe result, never a page error
        return Probe(name=name, status="down", latencyMs=None, error=_scrub(exc))
    ms = int((time.perf_counter() - t0) * 1000)
    return Probe(name=name, status="slow" if ms > _SLOW_MS else "up", latencyMs=ms, error=None)


async def _http_ok(client: httpx.AsyncClient, url: str, headers_sink: dict[str, str] | None = None) -> None:
    resp = await client.get(url)
    if headers_sink is not None and resp.headers.get("date"):
        headers_sink["date"] = resp.headers["date"]
    resp.raise_for_status()


def _span(seconds: float) -> str:
    """Coarse human span, fixed vocabulary (hours below two days, else days)."""
    seconds = max(0.0, seconds)
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} minutes"
    if seconds < 172800:
        return f"{int(seconds // 3600)} hours"
    return f"{int(seconds // 86400)} days"


def _issuer_cn(cert: x509.Certificate) -> str:
    try:
        cn = cert.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        return str(cn[0].value) if cn else "unknown issuer"
    except Exception:  # noqa: BLE001
        return "unknown issuer"


_INTERNAL_LEAF_MAX = timedelta(days=2)   # Caddy's internal CA issues ~12 h leaves; ACME leaves live ~90 d


def _certificate_probe_from(not_after: datetime, now: datetime, issuer: str, latency_ms: int | None,
                            not_before: datetime | None = None) -> Probe:
    """Pure: the served leaf's expiry → a probe. Caddy's internal CA (LAN / air-gapped boxes, `tls internal`)
    issues short-lived leaves and renews them on its own, so for it only a LAPSED renewal is a fault; an ACME
    or any other certificate goes amber inside two weeks of expiry (the renewal window). An internal leaf is
    recognised by its LIFETIME (not_before → not_after ≤ 2 days), not by the issuer's name: the appliance names
    its CA per deployment since beta.17 ("Talus GIS (<address>) - <year> ECC Root"), and the name-only test
    flagged a test server's 12 h leaf as "expiring" the day it was upgraded. The old name still counts."""
    remaining = not_after - now
    internal = "Caddy Local Authority" in issuer or (
        not_before is not None and (not_after - not_before) <= _INTERNAL_LEAF_MAX)
    if remaining.total_seconds() <= 0:
        return Probe(name="certificate", status="down", latencyMs=latency_ms, error="certificate expired",
                     detail=f"expired {_span(-remaining.total_seconds())} ago; issuer {issuer}")
    if internal:
        status = "slow" if remaining < _CERT_AMBER_INTERNAL else "up"
        detail = f"internal CA, renewed automatically; current certificate expires in {_span(remaining.total_seconds())}"
    else:
        status = "slow" if remaining < _CERT_AMBER else "up"
        detail = f"expires in {_span(remaining.total_seconds())}; issuer {issuer}"
    return Probe(name="certificate", status=status, latencyMs=latency_ms, error=None, detail=detail)


def _read_served_cert(target: str, server_name: str) -> x509.Certificate:
    """One TLS handshake to the edge inside the compose network, verification OFF on purpose: this reads
    what is served (the internal CA and ACME alike), it does not trust it. Blocking — run in a thread."""
    host, _, port = target.rpartition(":")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host or target, int(port or 443)), timeout=_PROBE_TIMEOUT) as s:
        with ctx.wrap_socket(s, server_hostname=server_name) as t:
            der = t.getpeercert(binary_form=True)
    if not der:
        raise ssl.SSLError("no certificate served")
    return x509.load_der_x509_certificate(der)


async def _certificate_probe() -> Probe:
    """Days to expiry of the portal's served certificate (item 3). Shown only when TALUS_HOSTNAME is set."""
    server_name = settings.talus_hostname.split(",")[0].strip()
    t0 = time.perf_counter()
    try:
        cert = await asyncio.wait_for(run_in_threadpool(_read_served_cert, settings.edge_tls_target, server_name),
                                      timeout=_PROBE_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except ssl.SSLError:
        # The edge answered TLS but had nothing for this name (a hostname the edge was not configured for).
        return Probe(name="certificate", status="down", latencyMs=None, error="no certificate for the portal hostname")
    except BaseException as exc:  # noqa: BLE001
        return Probe(name="certificate", status="down", latencyMs=None, error=_scrub(exc))
    ms = int((time.perf_counter() - t0) * 1000)
    return _certificate_probe_from(cert.not_valid_after_utc, datetime.now(timezone.utc), _issuer_cn(cert), ms,
                                   not_before=cert.not_valid_before_utc)


def _clock_probe_from(api_now: datetime, db_now: datetime | None, auth_date_header: str | None) -> Probe:
    """Pure: the api's clock against the database's `now()` and the sign-in server's HTTP `Date` (1 s
    resolution, so a second or two is noise). Red past _CLOCK_RED_S — where JWT iat/exp and certificate
    validity start failing. Keycloak sends no Date header (finding, 2026-10-13), so that leg reads
    "sends no Date header" on a stock box and only lights up when a front that does sit in between."""
    parts: list[str] = []
    worst = 0.0
    if db_now is not None:
        skew_db = (db_now - api_now).total_seconds()
        parts.append(f"database {skew_db:+.1f} s")
        worst = max(worst, abs(skew_db))
    else:
        parts.append("database unreadable")
    if auth_date_header:
        try:
            skew_auth = (parsedate_to_datetime(auth_date_header) - api_now).total_seconds()
            parts.append(f"sign-in server {skew_auth:+.0f} s")
            worst = max(worst, abs(skew_auth))
        except (TypeError, ValueError):
            parts.append("sign-in server Date header unparseable")
    else:
        parts.append("sign-in server sends no Date header")
    down = worst > _CLOCK_RED_S
    return Probe(name="clock", status="down" if down else "up", latencyMs=None,
                 error="clock skew" if down else None, detail="; ".join(parts))


async def _openfga_probe(client: httpx.AsyncClient) -> None:
    if not settings.openfga_store_id:
        raise RuntimeError("unconfigured")  # scrubbed to 'unreachable'; store id absent = not healthy
    await _http_ok(client, f"{settings.openfga_api_url}/stores/{settings.openfga_store_id}")


def _storage_probe() -> None:
    # The bucket_exists-probe equivalent (decision-object-store-build.md B7): cheapest call
    # proving the media store is present AND writable. Same green/red semantics — returns
    # on green, raises on red.
    get_media_storage().healthcheck()


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_media_usage(detail: str | None) -> tuple[int | None, int | None]:
    """The media_usage job's cached 'objects=N bytes=M' detail line → (bytes, objects)."""
    if not detail:
        return None, None
    objects = bytes_ = None
    for part in detail.split():
        key, _, val = part.partition("=")
        if val.isdigit():
            if key == "objects":
                objects = int(val)
            elif key == "bytes":
                bytes_ = int(val)
    return bytes_, objects


async def _operational(conn: AsyncConnection) -> dict[str, Any]:
    """Every already-recorded operational read, one place. Runs only when postgres answered. Each
    block guards itself: a single failing read nulls ITS tile (logged server-side with the real
    error) — it never blanks the others and never masquerades as a dead postgres."""
    out: dict[str, Any] = {}
    async with conn.cursor(row_factory=dict_row) as cur:
        try:
            await cur.execute(
                "SELECT created_at, target FROM platform.backups "
                "WHERE status = 'ok' ORDER BY created_at DESC LIMIT 1")
            row = await cur.fetchone()
            eff = await get_effective_backup_settings(conn)
            out["backup"] = BackupState(
                lastOkAt=_iso(row["created_at"]) if row else None,
                target=row["target"] if row else None,
                scheduledEnabled=eff.scheduled_enabled,
            )
            # F3: staleness is judged against the CONFIGURED cadence, not a fixed 2 days — a weekly
            # schedule would false-positive daily on a constant, an hourly one would stay silent
            # through dozens of missed runs.
            out["backup_interval_seconds"] = eff.interval_seconds
        except Exception as exc:  # noqa: BLE001
            log.warning("system_health_read_failed", read="backup", error=repr(exc))
        latest: dict[str, Any] = {}
        try:
            await cur.execute(
                "SELECT DISTINCT ON (job_name) job_name, started_at, status, detail "
                "FROM platform.maintenance_runs ORDER BY job_name, started_at DESC")
            latest = {r["job_name"]: r for r in await cur.fetchall()}
            out["maintenance"] = [
                JobState(job=name,
                         lastRunAt=_iso(latest[name]["started_at"]) if name in latest else None,
                         lastStatus=latest[name]["status"] if name in latest else None)
                for name in registered_job_names()]
            drill = latest.get("restore_drill")
            if drill is not None and drill["status"] == "ok":
                out["restore_drill"] = _iso(drill["started_at"])
        except Exception as exc:  # noqa: BLE001
            log.warning("system_health_read_failed", read="maintenance", error=repr(exc))
        try:
            # F5: how long has each failing job been failing? Everything since its last 'ok'.
            # "failing since 2026-07-12 (119 consecutive)" is actionable in a way "error" is not.
            # NOTE: deliberately does NOT select `detail` — see the Issue docstring.
            await cur.execute(
                "SELECT job_name, count(*) AS consecutive, min(started_at) AS failing_since "
                "FROM platform.maintenance_runs m "
                "WHERE status <> 'ok' AND started_at > COALESCE("
                "  (SELECT max(started_at) FROM platform.maintenance_runs o "
                "    WHERE o.job_name = m.job_name AND o.status = 'ok'), '-infinity'::timestamptz) "
                "GROUP BY job_name")
            out["job_failures"] = {
                r["job_name"]: JobFailure(consecutive=r["consecutive"],
                                          failingSince=_iso(r["failing_since"]))
                for r in await cur.fetchall()}
        except Exception as exc:  # noqa: BLE001
            log.warning("system_health_read_failed", read="job_failures", error=repr(exc))
        try:
            await cur.execute("SELECT pg_database_size(current_database()) AS n")
            db_bytes = (await cur.fetchone())["n"]
            media_row = latest.get("media_usage")
            media_bytes, media_objects = _parse_media_usage(
                media_row["detail"] if media_row and media_row["status"] == "ok" else None)
            out["storage"] = StorageState(
                dbBytes=db_bytes, mediaBytes=media_bytes, mediaObjects=media_objects,
                mediaAsOf=_iso(media_row["started_at"]) if media_row else None)
        except Exception as exc:  # noqa: BLE001
            log.warning("system_health_read_failed", read="storage", error=repr(exc))
    return out


_STALE_FLOOR_S = 26 * 3600   # F3 floor: one missed cycle + slack, so a short interval isn't a hair-trigger
_DRILL_GRACE_S = 14 * 86400  # A4r: drill_never arms this long after install (§6)
_BACKUP_JOB = "scheduled_backup"


def _ago(seconds: float) -> str:
    """Coarse human age for issue text. Fixed vocabulary — no caller input reaches this."""
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} minutes"
    if seconds < 172800:
        return f"{int(seconds // 3600)} hours"
    return f"{int(seconds // 86400)} days"


def _issues(
    *,
    probes: list[Probe],
    backup: BackupState,
    maintenance: list[JobState],
    job_failures: dict[str, JobFailure],
    restore_drill_ok_at: str | None,
    backup_interval_seconds: int | None,
    now: datetime,
    installed_at: datetime | None = None,
) -> list[Issue]:
    """Derive the SYSTEM's issue list from state the page already shows (decision-system-health-
    issue-escalation.md §3).

    ★ Why this exists. Between 2026-07-12 and 2026-07-26 every input below was present and correct —
    `lastOkAt` 14 days stale, `scheduled_backup` at `status='error'`, the client's own `backupStale()`
    returning true — and the banner still read "All systems operational", because it derived from
    PROBES alone. Every service genuinely was up; the claim the page made was about the SYSTEM. This
    function is the missing step between "the processes are running" and "the system is fine".

    Pure and `now`-injectable so the outage can be replayed as a test fixture.
    """
    issues: list[Issue] = []

    # -- backups. Critical, not warn: it is the only condition here whose loss is unrecoverable and
    #    grows with every hour it goes unnoticed. A DISABLED schedule is an operator choice, not a
    #    fault — it suppresses both backup issues rather than nagging.
    if backup.scheduledEnabled:
        if backup.lastOkAt is None:
            issues.append(Issue(
                key="backup_never", severity="critical",
                summary="No successful backup has ever been recorded."))
        else:
            age = (now - datetime.fromisoformat(backup.lastOkAt)).total_seconds()
            window = max(2 * (backup_interval_seconds or 0), _STALE_FLOOR_S)
            if age > window:
                issues.append(Issue(
                    key="backup_stale", severity="critical",
                    summary=f"No successful backup in {_ago(age)} — the last one succeeded "
                            f"{backup.lastOkAt[:10]}."))

    # -- failing maintenance jobs. Job names come from registered_job_names(), a fixed internal set.
    for job in maintenance:
        if job.lastStatus is not None and job.lastStatus != "ok":
            fail = job_failures.get(job.job)
            detail = ""
            if fail is not None and fail.failingSince is not None:
                detail = (f" — failing since {fail.failingSince[:10]} "
                          f"({fail.consecutive} consecutive runs)")
            issues.append(Issue(
                key=f"job_failing:{job.job}",
                severity="critical" if job.job == _BACKUP_JOB else "warn",
                summary=f"Maintenance job '{job.job}' is failing{detail}."))

    # -- the portal certificate and the clock (item 3). Fixed text + the probe's own fixed-vocabulary detail.
    for p in probes:
        if p.name == "certificate":
            if p.status == "down" and p.error == "certificate expired":
                issues.append(Issue(key="certificate_expired", severity="critical",
                                    summary="The portal certificate has expired — browsers will refuse the site."))
            elif p.status == "slow":
                issues.append(Issue(key="certificate_expiring", severity="warn",
                                    summary=f"The portal certificate {p.detail}."))
        elif p.name == "clock" and p.status == "down":
            issues.append(Issue(key="clock_skew", severity="critical",
                                summary=f"The portal clock disagrees with its neighbours by over {int(_CLOCK_RED_S)} s "
                                        f"({p.detail}) — sign-ins and certificates fail past that."))

    # -- degraded-but-up dependencies. Today these are a banner aside; they belong in the count.
    slow = [p.name for p in probes if p.status == "slow" and p.name != "certificate"]
    if slow:
        issues.append(Issue(
            key="probe_slow", severity="warn",
            summary=f"{len(slow)} service(s) responding slowly: {', '.join(sorted(slow))}."))

    # -- DR is only proven by rehearsal (Talus 44). A4r ruling (decision-deployment-appliance.md
    #    §6): the nag ARMS 14 days after install (anchored on the first Flyway migration), so a
    #    fresh box gets an honest, quiet fortnight — and one that still never drills goes amber.
    #    installed_at=None (anchor unreadable) means NO grace: fail toward nagging, never silence.
    if restore_drill_ok_at is None:
        in_grace = (installed_at is not None
                    and (now - installed_at).total_seconds() < _DRILL_GRACE_S)
        if not in_grace:
            issues.append(Issue(
                key="drill_never", severity="warn",
                summary="No successful restore drill has been recorded."))

    issues.sort(key=lambda i: 0 if i.severity == "critical" else 1)
    return issues


async def _migration_head() -> str | None:
    """The Flyway head, read via the OWNER pool: S-1a's least-privilege runtime role deliberately
    cannot see public.flyway_schema_history, and that fence is worth keeping — this is a one-row,
    fixed-text, no-caller-input read of the schema VERSION, exactly the rare elevated read the
    owner pool exists to contain. Null (never an error) when unreadable."""
    try:
        if db_module.owner_pool is None:
            return None
        async with db_module.owner_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT version FROM public.flyway_schema_history "
                    "WHERE success ORDER BY installed_rank DESC LIMIT 1")
                head = await cur.fetchone()
        return f"V{head[0]}" if head and head[0] else None
    except Exception as exc:  # noqa: BLE001
        log.warning("system_health_read_failed", read="migration_head", error=repr(exc))
        return None


async def _installed_at() -> datetime | None:
    """When THIS deployment was installed = the first Flyway migration's installed_on — a record
    every install already has, on every path (prod install, DR rebuild, drill). Same owner-pool
    rationale and fail-open shape as _migration_head above; anchors the A4r drill grace period.
    Null (never an error) when unreadable — which _issues treats as NO grace, never silence."""
    try:
        if db_module.owner_pool is None:
            return None
        async with db_module.owner_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT min(installed_on) FROM public.flyway_schema_history WHERE success")
                row = await cur.fetchone()
        if row is None or row[0] is None:
            return None
        ts = row[0]
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
    except Exception as exc:  # noqa: BLE001
        log.warning("system_health_read_failed", read="installed_at", error=repr(exc))
        return None


@router.get(
    "/admin/system-health",
    response_model=SystemHealth,
    summary="One-glance portal health: service probes + operational state (admin-only)",
)
async def get_system_health(user: User = Depends(_require_admin)) -> SystemHealth:
    """Probes run concurrently on demand (no background beacon); the operational block reads only
    tables the platform already maintains. Fail-open on DISPLAY (red rows, null tiles), while every
    probed service keeps its own fail-closed semantics untouched."""
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
        pg_probe: Probe
        operational: dict[str, Any] = {}
        db_now: datetime | None = None
        auth_headers: dict[str, str] = {}
        try:
            # Manual acquisition (not Depends): postgres-down must render a red row, not a 500.
            if db_module.pool is None:
                raise RuntimeError("pool not initialized")
            async with db_module.pool.connection() as conn:
                t0 = time.perf_counter()
                cur = await asyncio.wait_for(conn.execute("SELECT now()"), timeout=_PROBE_TIMEOUT)
                ms = int((time.perf_counter() - t0) * 1000)
                row = await cur.fetchone()
                db_now = row[0] if row else None
                pg_probe = Probe(name="postgres", status="slow" if ms > _SLOW_MS else "up",
                                 latencyMs=ms, error=None)
                operational = await _operational(conn)  # self-guarding per tile — never raises
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            pg_probe = Probe(name="postgres", status="down", latencyMs=None, error=_scrub(exc))

        net_probes = await asyncio.gather(
            _probe("storage", run_in_threadpool(_storage_probe)),
            _probe("keycloak", _http_ok(
                client,
                f"{settings.keycloak_admin_base_url}/realms/{settings.keycloak_realm}"
                "/.well-known/openid-configuration", auth_headers)),
            _probe("openfga", _openfga_probe(client)),
            _probe("pg_tileserv", _http_ok(client, f"{settings.tileserv_url}/index.json")),
            # Item 3: the served certificate (only when the box knows its own hostname) and the clock.
            *([_certificate_probe()] if settings.talus_hostname.strip() else []),
        )

    probes = [pg_probe, *net_probes,
              _clock_probe_from(datetime.now(timezone.utc), db_now, auth_headers.get("date")),
              Probe(name="api", status="up", latencyMs=0, error=None)]  # it answered — trivially up
    down = [p.name for p in probes if p.status == "down"]
    if down:
        log.warning("system_health_degraded", down=down, actor=str(user.id))

    now = datetime.now(timezone.utc)
    backup_state = operational.get("backup", BackupState(lastOkAt=None, target=None,
                                                         scheduledEnabled=None))
    maintenance_state = operational.get("maintenance", [])
    # Self-guarding, like every read in _operational: this file's contract is fail-open on DISPLAY
    # (see the module docstring — a dead dependency renders a red row, it must never error the page).
    # _issues parses timestamps and does arithmetic on them, so it is not obviously total; an
    # unguarded call here would be the ONE path that 500s the whole page, and a health page that
    # 500s is the worst possible failure for a screen whose job is telling you something is wrong.
    # Degrade to "no issues computed" and log it, rather than taking the page down with us.
    issues: list[Issue] = []
    try:
        issues = _issues(
            probes=probes,
            backup=backup_state,
            maintenance=maintenance_state,
            job_failures=operational.get("job_failures", {}),
            restore_drill_ok_at=operational.get("restore_drill"),
            backup_interval_seconds=operational.get("backup_interval_seconds"),
            now=now,
            installed_at=await _installed_at(),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("system_health_issues_failed", error=repr(exc))
    if any(i.severity == "critical" for i in issues):
        # Server-side breadcrumb: the page telling the truth still depends on someone LOOKING at it
        # (§6 — notification is deliberately out of scope), so leave a trail in the log too.
        log.warning("system_health_issues", actor=str(user.id),
                    keys=[i.key for i in issues if i.severity == "critical"])
    return SystemHealth(
        generatedAt=now.isoformat(),
        probes=probes,
        backup=backup_state,
        maintenance=maintenance_state,
        storage=operational.get("storage", StorageState(dbBytes=None, mediaBytes=None,
                                                        mediaObjects=None, mediaAsOf=None)),
        version=VersionState(service=settings.service_version,
                             migrationHead=await _migration_head()),
        restoreDrillLastOkAt=operational.get("restore_drill"),
        issues=issues,
    )
