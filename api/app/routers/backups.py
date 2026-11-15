"""Talus 40/43 — Admin → Backups (disaster-recovery catalog + the safe actions, admin-gated).

Talus 40 shipped read-only (schedule + retention were BACKUP_* env). Talus 43 makes the editable subset
admin-writable from this panel (decision-backup-settings.md): cadence, retention (days + the keep-at-least
floor), encryption recipients (`age` PUBLIC keys), and a scheduled-on/off toggle — persisted in the
platform.backup_settings singleton (V042), COALESCE-over-env, effective with no restart. The off-host
DESTINATION joins them here (decision-backup-destination.md, V090): its own sub-resource — NOT extra
fields on the knobs PUT, because a secret needs "omitted = keep" semantics, and the knobs PUT is
full-replace-null-clears. The secret key is Fernet-encrypted at rest (secrets.py), never returned, never
logged. This is the A.1-★ shape (a client that calls out holding a credential, where a user chooses
where it points) — admin-only, endpoint fenced (scheme required, no embedded credentials), audited.

  - GET  /admin/backups            — the platform.backups artifact catalog (read-only list) + the EFFECTIVE
                                      config (cadence/retention/recipients/destination) + which knobs are
                                      overridden + the env defaults (so the panel shows default vs custom).
  - PUT  /admin/backups/settings   — update the editable knobs (admin-only; validated; audited). NULL/empty
                                      field = "use the env default" (clears the override; F-precedence).
  - PUT  /admin/backups/destination — set/update the off-host S3 destination override (secretKey omitted =
                                      keep the stored secret, so editing the endpoint never forces re-entry).
  - DELETE /admin/backups/destination — clear the override (revert to the BACKUP_S3_* env default).
  - POST /admin/backups/destination/test — connection test against the CANDIDATE values (nothing persisted):
                                      write a probe object, stat it, delete it — proves reach, auth, AND
                                      write permission. Bounded timeouts; 200 with ok/detail either way.
  - POST /admin/backups/run        — "Back up now": an on-demand backup (additive, never destructive). Runs
                                      as a background task under the SAME advisory lock as the scheduler, so
                                      it can never overlap a scheduled tick. 202 + a 409 on an overlapping click.
                                      Always runs — it does NOT consult the scheduled-on/off toggle.
  - GET  /admin/backups/{id}/download — stream a dump artifact from the CURRENT backup target (resolved per
                                      request — no restart), proxied like the media byte-proxy.

There is intentionally NO restore endpoint — restore is a documented, backup-gated operator runbook
(docs/dr-restore-runbook.md), never a casual button (decision §4 F-restore). All are behind _require_admin
(JWT-realm-role based, so OpenFGA-independent — fail-closed even with OpenFGA down).
"""
import asyncio
import re
import uuid
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel, Field, field_validator

from .. import admin_audit
from ..backup import env_file_readable, open_backup_stream, resolve_backup_store, run_backup
from ..backup_settings import get_effective_backup_destination, get_effective_backup_settings
from ..config import settings
from ..db import get_db
from ..deps import _require_admin
from ..maintenance import MaintenanceJob, run_job_once_locked
from ..models.user import User
from ..secrets import SecretDecryptError, SecretKeyUnavailable, decrypt_secret, encrypt_secret
from ..storage import MediaObjectMissing, S3Storage

log = structlog.get_logger(__name__)

router = APIRouter(tags=["backups"])


# A cheap `age` X25519 public-key sanity check: "age1" + 58 bech32 chars (62 total). NOT a full bech32
# checksum verify — just enough to reject a typo, an `AGE-SECRET-KEY-…` private key pasted by mistake, or
# junk, so a bad recipient can't silently disable encryption (decision §3). SSH recipients aren't UI-editable.
_AGE_RECIPIENT_RE = re.compile(r"age1[0-9a-z]{58}")
_MAX_RECIPIENTS = 20  # generous upper bound (primary + break-glass + a few custodians); guards against abuse

# Process-local guard so an accidental double-click gets an immediate 409 (the advisory lock is the real
# cross-replica/scheduler guard; this just gives fast, clean feedback within one replica).
_manual_lock = asyncio.Lock()


class BackupRow(BaseModel):
    """One catalog entry (most-recent-first)."""

    id: int
    createdAt: str
    kind: str                 # 'scheduled' | 'manual'
    status: str               # 'ok' | 'error'
    dbObjectKey: str | None
    dbSizeBytes: int
    dbSha256: str | None
    keycloakSizeBytes: int   # > 0 => the Keycloak (identity) dump is in this set (Talus 42)
    openfgaSizeBytes: int    # > 0 => the OpenFGA (authz) dump is in this set (Talus 42)
    envInBackup: bool        # the `.env` lifeboat is in this set (Talus 45; always age-encrypted)
    mediaObjectsCopied: int
    mediaBytesCopied: int
    target: str
    pgVersion: str | None
    detail: str | None
    encrypted: bool          # client-side age-encrypted (derived from the .age artifact suffix)


class BackupOverrides(BaseModel):
    """Which editable knobs are currently DB OVERRIDES (true) vs falling back to the env default (false) —
    drives the panel's "custom" vs "deployment default" affordance + the per-knob reset (Talus 43)."""

    intervalSeconds: bool
    retentionDays: bool
    retentionMinKeep: bool
    scheduledEnabled: bool
    ageRecipients: bool
    includeEnv: bool


class BackupEnvDefaults(BaseModel):
    """The BACKUP_*/MAINTENANCE_* env defaults — what a knob reverts to when its override is cleared. Shown
    (disabled) when the admin sets a knob to "use deployment default" (Talus 43)."""

    intervalSeconds: int
    retentionDays: int
    retentionMinKeep: int
    scheduledEnabled: bool
    ageRecipients: list[str]
    includeEnv: bool
    # decision-backup-destination.md: the env-side destination label, so "reset to default" can say
    # exactly what it reverts to. A LABEL only — env credentials are never disclosed here.
    destinationTarget: str


class BackupDestination(BaseModel):
    """The EFFECTIVE backup destination for the panel's editable form (decision-backup-destination.md).
    `accessKey` is returned only for the DB OVERRIDE (the admin entered it; it is an identifier, not the
    secret) — an env-sourced destination discloses its label/endpoint but never its credentials. The
    secret key is NEVER returned in any form: `secretKeySet` is the only signal (plus the test button)."""

    source: str          # 'override' (V090 group) | 'env' (BACKUP_S3_* or the local default)
    kind: str            # 's3' | 'local'
    target: str          # the honest-DR label (same string as config.target / catalog rows)
    endpoint: str        # '' for the local kind; env-sourced s3 shows its endpoint (already in the label)
    bucket: str
    accessKey: str       # override only; '' otherwise
    secretKeySet: bool   # an override secret exists — for the "configured" affordance, never the value


class BackupConfig(BaseModel):
    """The EFFECTIVE (override-or-env) DR settings for the panel + the override/env-default detail the
    editable form needs. `ageRecipients` are PUBLIC keys (the api holds only the public half) so returning
    them is safe — and necessary to edit them. The off-host target stays read-only (env-only)."""

    enabled: bool                # effective scheduled-on/off
    intervalSeconds: int
    retentionDays: int
    retentionMinKeep: int
    ageRecipients: list[str]     # effective recipients (PUBLIC age1… keys; editable)
    target: str                  # the effective destination's honest-DR label
    offHost: bool
    destination: BackupDestination  # decision-backup-destination.md: the editable detail (secret-free)
    objectBuckets: list[str]
    encryptionEnabled: bool      # client-side age encryption configured (artifacts stored as ciphertext)
    encryptionRecipients: int    # how many age recipients new backups are encrypted to (== len(ageRecipients))
    includesKeycloak: bool       # Talus 42: identity DB is part of the backup set (DSN configured)
    includesOpenfga: bool        # Talus 42: authz DB is part of the backup set (DSN configured)
    # Talus 45 — the `.env` lifeboat. `includeEnv` is the SETTING; `includeEnvEffective` is the runtime truth
    # (the fail-closed AND: includeEnv AND encryption-on AND the api can read .env). `includeEnvFileReadable`
    # lets the panel hint precisely (encryption-off vs can't-read-.env) and gates the toggle.
    includeEnv: bool
    includeEnvEffective: bool
    includeEnvFileReadable: bool
    overridden: BackupOverrides  # Talus 43: per-knob default-vs-custom
    envDefaults: BackupEnvDefaults  # Talus 43: the env fallback for each editable knob


class BackupsResponse(BaseModel):
    config: BackupConfig
    backups: list[BackupRow]


class BackupSettingsUpdate(BaseModel):
    """Admin PUT body for the editable backup knobs (Talus 43). Each field is OPTIONAL/NULLABLE — null =
    "use the deployment (env) default", which CLEARS the DB override (F-precedence). A full-replace write:
    every column is set from the body, so an omitted/null field becomes NULL in the singleton and falls back
    to env. The off-host target/credentials are deliberately absent — they stay env-only."""

    intervalSeconds: int | None = Field(default=None, gt=0)
    retentionDays: int | None = Field(default=None, ge=0)
    # The keep-at-least floor can never be set below 1 from the UI either (mirrors the DB CHECK >= 1).
    retentionMinKeep: int | None = Field(default=None, ge=1)
    # null OR an empty list => use the env recipients (clear the override). Each key must look like an
    # `age1…` public key (cheap format check), so a typo can't silently disable encryption.
    ageRecipients: list[str] | None = None
    scheduledEnabled: bool | None = None
    # Talus 45: include the `.env` lifeboat. null = use the env default (clears the override). true is gated:
    # the handler 422s if the api can't read .env (caught at config time), and at runtime it's still ANDed
    # with encryption-on (no recipients ⇒ no .env, regardless of this toggle).
    includeEnv: bool | None = None

    @field_validator("ageRecipients")
    @classmethod
    def _validate_recipients(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned: list[str] = []
        for raw in v:
            key = (raw or "").strip()
            if not key:
                continue  # tolerate blank lines from the textarea
            if not _AGE_RECIPIENT_RE.fullmatch(key):
                raise ValueError(
                    f"'{key[:16]}…' is not a valid age public key (expected age1…); "
                    "recipients are PUBLIC keys, never a secret/private key"
                )
            cleaned.append(key)
        if len(cleaned) > _MAX_RECIPIENTS:
            raise ValueError(f"at most {_MAX_RECIPIENTS} recipients")
        return cleaned  # may be [] => treated as "use env default" by the handler


def _env_destination_label() -> str:
    """What the ENV configures (ignoring any V090 override) — the "reset to default reverts to…" label."""
    if settings.backup_target_is_offhost:
        return f"s3://{settings.backup_bucket} (off-host)"
    return f"file://{settings.storage_root}/{settings.backup_bucket} (same-host — not DR)"


class BackupDestinationUpdate(BaseModel):
    """Admin body for the off-host destination override (PUT) and the connection test (POST …/test).
    NOT part of the knobs PUT (decision-backup-destination.md F3): a secret needs "omitted = KEEP the
    stored value" — the opposite of the knobs' null-clears convention — so the destination is its own
    sub-resource. Clearing the override is DELETE, never an empty field.

    The endpoint is the A.1-★ sharp edge (an admin-chosen outbound target for a held credential): the
    validators are the fence — explicit http(s) scheme (scheme-less used to degrade to plaintext),
    no embedded credentials, host[:port] only. Deliberately NO private-IP ban: the legitimate case
    includes an on-LAN NAS (decision §4 F4)."""

    endpoint: str = Field(max_length=512)
    bucket: str | None = Field(default=None, max_length=255)   # None/blank = env BACKUP_BUCKET
    accessKey: str = Field(max_length=256)
    secretKey: str | None = Field(default=None, max_length=512)  # None = keep stored (PUT) / test stored

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, v: str) -> str:
        v = (v or "").strip().rstrip("/")
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError(
                "endpoint must include an explicit http:// or https:// scheme "
                "(e.g. https://s3.example.com) — a scheme-less endpoint would silently disable TLS"
            )
        parsed = urlparse(v)
        if parsed.username or parsed.password:
            raise ValueError(
                "endpoint must not embed credentials (user@host) — access and secret keys are their own fields"
            )
        if not parsed.hostname:
            raise ValueError("endpoint has no host")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError(
                "endpoint must be scheme://host[:port] only — no path or query (the bucket is its own field)"
            )
        return v

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        if not v:
            return None  # use the env default
        if not re.fullmatch(r"[A-Za-z0-9._-]+", v):
            raise ValueError("bucket may contain only letters, digits, dots, dashes, and underscores")
        return v

    @field_validator("accessKey")
    @classmethod
    def _validate_access_key(cls, v: str) -> str:
        v = (v or "").strip()
        if not v or not re.fullmatch(r"\S+", v):
            raise ValueError("access key is required and may not contain whitespace")
        return v

    @field_validator("secretKey")
    @classmethod
    def _validate_secret_key(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            # An explicit empty string must never silently clear or keep — make the semantics loud.
            raise ValueError("secret key may not be blank — omit the field to keep the stored secret")
        return v


class BackupDestinationTestResult(BaseModel):
    ok: bool
    detail: str  # terse + secret-free: an S3 error CODE or a reachability line, never a response body


def _classify_probe_failure(message: str) -> str:
    """Secret-free failure reason for the panel: the S3 error CODE when one is present, else a generic
    reachability line. NEVER the store's response body/message — the test must not become a read-back
    oracle for whatever the admin pointed it at (decision §3)."""
    m = re.search(r"\bcode: (\w+)", message)
    if m:
        return (
            f"the destination refused the request (S3 error {m.group(1)}) — "
            "check the bucket name, access key, and secret key"
        )
    return "the endpoint could not be reached (connection failed or timed out) — check the URL and network path"


async def _config(conn: AsyncConnection) -> BackupConfig:
    """The EFFECTIVE config (COALESCE-over-env) + per-knob override flags + env defaults for the editable
    form. Reads the singleton via the shared resolvers, so the panel and the scheduler/jobs agree on what's
    live. The destination resolves WITHOUT the secret (with_secret=False) — this path never touches the
    ciphertext, so the panel stays renderable even under key loss."""
    eff = await get_effective_backup_settings(conn)
    dest = await get_effective_backup_destination(conn)
    env_readable = await run_in_threadpool(env_file_readable)
    return BackupConfig(
        enabled=eff.scheduled_enabled,
        intervalSeconds=eff.interval_seconds,
        retentionDays=eff.retention_days,
        retentionMinKeep=eff.retention_min_keep,
        ageRecipients=list(eff.age_recipients),
        target=dest.label,
        offHost=dest.is_offhost,
        destination=BackupDestination(
            source="override" if dest.overridden else "env",
            kind=dest.kind,
            target=dest.label,
            endpoint=dest.endpoint,
            bucket=dest.bucket,
            accessKey=dest.access_key if dest.overridden else "",
            secretKeySet=dest.secret_key_set,
        ),
        objectBuckets=settings.backup_object_buckets_list,
        encryptionEnabled=eff.encryption_enabled,
        encryptionRecipients=len(eff.age_recipients),
        includesKeycloak=bool(settings.backup_keycloak_db_url.strip()),
        includesOpenfga=bool(settings.backup_openfga_db_url.strip()),
        includeEnv=eff.include_env,
        # The runtime truth: the toggle AND encryption-on AND the api can read .env (the fail-closed AND that
        # run_backup enforces). The panel shows "inactive" whenever this is false despite includeEnv being on.
        includeEnvEffective=eff.include_env and eff.encryption_enabled and env_readable,
        includeEnvFileReadable=env_readable,
        overridden=BackupOverrides(
            intervalSeconds="interval_seconds" in eff.overrides,
            retentionDays="retention_days" in eff.overrides,
            retentionMinKeep="retention_min_keep" in eff.overrides,
            scheduledEnabled="scheduled_enabled" in eff.overrides,
            ageRecipients="age_recipients" in eff.overrides,
            includeEnv="include_env" in eff.overrides,
        ),
        envDefaults=BackupEnvDefaults(
            intervalSeconds=settings.backup_interval_seconds,
            retentionDays=settings.backup_retention_days,
            retentionMinKeep=settings.backup_retention_min_keep,
            scheduledEnabled=settings.backup_enabled,
            ageRecipients=settings.backup_age_recipients_list,
            includeEnv=settings.backup_include_env,
            destinationTarget=_env_destination_label(),
        ),
    )


async def _backups_response(conn: AsyncConnection, limit: int = 100) -> BackupsResponse:
    """The effective config + the artifact catalog (newest first). Shared by GET and PUT so both return the
    identical shape (PUT echoes the post-update state so the UI updates in one round-trip)."""
    config = await _config(conn)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, created_at, kind, status, db_object_key, db_size_bytes, db_sha256, "
            "       keycloak_size_bytes, openfga_size_bytes, env_size_bytes, "
            "       media_objects_copied, media_bytes_copied, target, pg_version, detail "
            "FROM platform.backups ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        rows = await cur.fetchall()
    backups = [
        BackupRow(
            id=r["id"],
            createdAt=r["created_at"].isoformat(),
            kind=r["kind"],
            status=r["status"],
            dbObjectKey=r["db_object_key"],
            dbSizeBytes=r["db_size_bytes"],
            dbSha256=r["db_sha256"],
            keycloakSizeBytes=r["keycloak_size_bytes"],
            openfgaSizeBytes=r["openfga_size_bytes"],
            envInBackup=r["env_size_bytes"] > 0,
            mediaObjectsCopied=r["media_objects_copied"],
            mediaBytesCopied=r["media_bytes_copied"],
            target=r["target"],
            pgVersion=r["pg_version"],
            detail=r["detail"],
            encrypted=(r["db_object_key"] or "").endswith(".age"),
        )
        for r in rows
    ]
    return BackupsResponse(config=config, backups=backups)


@router.get(
    "/admin/backups",
    response_model=BackupsResponse,
    summary="Disaster-recovery backup catalog (admin-only, read-only)",
)
async def list_backups(
    limit: int = Query(default=100, ge=1, le=500),
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(_require_admin),
) -> BackupsResponse:
    """The backup artifact catalog (newest first) + the effective DR config. No GIS data; the only write
    surface is the separate PUT below."""
    return await _backups_response(conn, limit)


@router.put(
    "/admin/backups/settings",
    response_model=BackupsResponse,
    summary="Update the editable backup settings (admin-only)",
)
async def update_backup_settings(
    payload: BackupSettingsUpdate,
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> BackupsResponse:
    """Update the editable knobs in the platform.backup_settings singleton (Talus 43). A null field (or an
    empty recipient list) CLEARS that override — the effective value falls back to env (F-precedence). Takes
    effect with no restart: the scheduler reads effective settings each tick, and the next backup reads the
    new recipients/retention. Admin-gated (OpenFGA-independent); writes carry actor attribution + a structlog
    line (no shadow-audit table — config, not a system of record, same call as org_settings)."""
    # Talus 45 PRECONDITION: enabling include_env requires the api can actually read .env — catch the common
    # misconfig (forgot the read-only mount) at config time with a clear 422, not silently at backup time.
    # Only checked when turning it ON (true); null/false never block. The runtime AND with encryption still
    # applies regardless (no recipients ⇒ no .env), so this gate is about file-access, not the encryption gate.
    if payload.includeEnv and not await run_in_threadpool(env_file_readable):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"cannot include the .env lifeboat: the API cannot read the .env file at "
                f"{settings.backup_env_path} — mount it read-only first (./.env:/secrets/.env:ro), then retry"
            ),
        )
    # Empty/missing recipients => store NULL (use env), never '' — so the effective reader treats it as
    # "no override" rather than "override to no recipients" (see backup_settings._parse_recipients).
    recipients_text = ",".join(payload.ageRecipients) if payload.ageRecipients else None
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.backup_settings SET "
            "interval_seconds = %s, retention_days = %s, retention_min_keep = %s, "
            "age_recipients = %s, scheduled_enabled = %s, include_env = %s, "
            "updated_by = %s, updated_at = now() WHERE id = true",
            (
                payload.intervalSeconds, payload.retentionDays, payload.retentionMinKeep,
                recipients_text, payload.scheduledEnabled, payload.includeEnv, admin.id,
            ),
        )
    await conn.commit()
    log.info(
        "backup_settings_updated",
        actor=str(admin.id),
        interval_seconds=payload.intervalSeconds,
        retention_days=payload.retentionDays,
        retention_min_keep=payload.retentionMinKeep,
        scheduled_enabled=payload.scheduledEnabled,
        age_recipients=len(payload.ageRecipients or []),
        include_env=payload.includeEnv,
    )
    # detail carries COUNTS/FLAGS only — never the recipient addresses (mirrors the structlog line
    # above and the geocoder-token redaction discipline; decision §6).
    await admin_audit.record(
        conn, actor_id=admin.id, action="backup_settings_updated", target_type="backup_settings",
        detail={"intervalSeconds": payload.intervalSeconds, "retentionDays": payload.retentionDays,
                "retentionMinKeep": payload.retentionMinKeep,
                "scheduledEnabled": payload.scheduledEnabled,
                "ageRecipientCount": len(payload.ageRecipients or []),
                "includeEnv": payload.includeEnv},
    )
    return await _backups_response(conn)


@router.put(
    "/admin/backups/destination",
    response_model=BackupsResponse,
    summary="Set the off-host backup destination override (admin-only)",
)
async def update_backup_destination(
    payload: BackupDestinationUpdate,
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> BackupsResponse:
    """Set/update the V090 destination override group in one transactional write. The secret key is
    Fernet-encrypted BEFORE the UPDATE (no key configured -> the actionable 422, secrets.py F1b);
    `secretKey` omitted keeps the stored ciphertext, so editing the endpoint never forces re-entry —
    but the FIRST save must include it (422 otherwise, so a half-configured override can't exist; the
    V090 CHECK pins the same at rest). Takes effect with NO restart: every backup run / download / test
    resolves the destination fresh. Audited WITHOUT the secret."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT s3_secret_key FROM platform.backup_settings WHERE id = true")
        row = await cur.fetchone()
    existing_ct = row[0] if row else None
    if payload.secretKey is not None:
        try:
            secret_ct = encrypt_secret(payload.secretKey)
        except SecretKeyUnavailable as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    elif existing_ct is not None:
        secret_ct = bytes(existing_ct)
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="no secret key is stored yet — enter the S3 secret key to configure this destination",
        )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.backup_settings SET "
            "s3_endpoint = %s, s3_bucket = %s, s3_access_key = %s, s3_secret_key = %s, "
            "updated_by = %s, updated_at = now() WHERE id = true",
            (payload.endpoint, payload.bucket, payload.accessKey, secret_ct, admin.id),
        )
    await conn.commit()
    # Endpoint/bucket/access key are identifiers an admin already sees in the panel; the secret is
    # NEVER logged or audited — only the fact that it changed.
    log.info(
        "backup_destination_updated",
        actor=str(admin.id), endpoint=payload.endpoint, bucket=payload.bucket,
        access_key=payload.accessKey, secret_changed=payload.secretKey is not None,
    )
    await admin_audit.record(
        conn, actor_id=admin.id, action="backup_destination_updated", target_type="backup_settings",
        detail={"endpoint": payload.endpoint, "bucket": payload.bucket,
                "accessKey": payload.accessKey, "secretChanged": payload.secretKey is not None},
    )
    return await _backups_response(conn)


@router.delete(
    "/admin/backups/destination",
    response_model=BackupsResponse,
    summary="Clear the destination override — revert to the deployment (env) default (admin-only)",
)
async def clear_backup_destination(
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> BackupsResponse:
    """Clear the whole V090 group (endpoint, bucket, access key, secret ciphertext) — the destination
    falls back to BACKUP_S3_* env, exactly as before the override existed (F-precedence). Existing
    backups at the override destination are NOT touched (F2: changes start fresh; the runbook covers
    recovering or clearing the old location)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.backup_settings SET "
            "s3_endpoint = NULL, s3_bucket = NULL, s3_access_key = NULL, s3_secret_key = NULL, "
            "updated_by = %s, updated_at = now() WHERE id = true",
            (admin.id,),
        )
    await conn.commit()
    log.info("backup_destination_cleared", actor=str(admin.id), reverts_to=_env_destination_label())
    await admin_audit.record(
        conn, actor_id=admin.id, action="backup_destination_cleared", target_type="backup_settings",
        detail={"revertsTo": _env_destination_label()},
    )
    return await _backups_response(conn)


@router.post(
    "/admin/backups/destination/test",
    response_model=BackupDestinationTestResult,
    summary="Test a backup destination before saving it (admin-only; persists nothing)",
)
async def test_backup_destination(
    payload: BackupDestinationUpdate,
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> BackupDestinationTestResult:
    """Probe the CANDIDATE destination: create the bucket if absent (exactly what a real backup would
    do), write a small probe object under `connection-test/`, stat it, delete it — proving reach,
    auth, and write permission, which is what "wrong endpoint ⇒ no backups until someone notices"
    makes the footgun (decision §2 deliverable 2). Nothing is persisted; `secretKey` omitted tests
    the STORED override secret so "test what's saved" needs no re-entry. Bounded timeouts so an
    unreachable endpoint can't hang a worker. Always 200 with ok/detail — a failed probe is a
    successful test run; the detail is terse and secret-free (see _classify_probe_failure)."""
    if payload.secretKey is not None:
        secret = payload.secretKey
    else:
        async with conn.cursor() as cur:
            await cur.execute("SELECT s3_secret_key FROM platform.backup_settings WHERE id = true")
            row = await cur.fetchone()
        ct = row[0] if row else None
        if ct is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="no secret key is stored yet — enter the S3 secret key to test",
            )
        try:
            secret = decrypt_secret(bytes(ct))
        except (SecretKeyUnavailable, SecretDecryptError) as exc:
            # A test failure, not a 500 — the panel gets the actionable reason (key unset / key changed).
            log.info("backup_destination_test", actor=str(admin.id), endpoint=payload.endpoint, ok=False)
            return BackupDestinationTestResult(ok=False, detail=str(exc))

    bucket = payload.bucket or settings.backup_bucket
    store = S3Storage(payload.endpoint, payload.accessKey, secret, bucket, timeout_seconds=10)
    probe_key = f"connection-test/probe-{uuid.uuid4().hex}"

    def _probe() -> None:
        store.ensure_root()
        store.write_bytes(probe_key, b"talus backup destination connection test", "text/plain")
        store.stat(probe_key)
        store.delete(probe_key)

    try:
        await run_in_threadpool(_probe)
    except Exception as exc:  # noqa: BLE001 — every failure class becomes a legible, secret-free reason
        log.info("backup_destination_test", actor=str(admin.id), endpoint=payload.endpoint, ok=False)
        return BackupDestinationTestResult(ok=False, detail=_classify_probe_failure(str(exc)))
    log.info("backup_destination_test", actor=str(admin.id), endpoint=payload.endpoint, ok=True)
    return BackupDestinationTestResult(
        ok=True, detail=f"wrote, verified, and removed a probe object in '{bucket}'",
    )


@router.post(
    "/admin/backups/run",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Take a backup now (admin-only; additive, never destructive)",
)
async def run_backup_now(user: User = Depends(_require_admin)) -> dict[str, str]:
    """Kick off an on-demand backup in the background (kind='manual'), under the scheduler advisory lock so it
    can't overlap a scheduled tick. Returns 202 immediately; the panel polls the catalog for the new row. A
    409 means a manual backup is already running on this replica."""
    if _manual_lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a manual backup is already in progress; try again shortly",
        )

    async def _task() -> None:
        async with _manual_lock:
            job = MaintenanceJob(
                "scheduled_backup",
                settings.backup_interval_seconds,
                lambda c: run_backup(c, kind="manual"),
            )
            try:
                ran = await run_job_once_locked(job)
                if not ran:
                    log.warning("manual_backup_lock_busy")  # scheduler tick / another replica held the lock
            except Exception:  # noqa: BLE001 — a backgrounded task must never raise into the loop
                log.warning("manual_backup_failed", exc_info=True)

    asyncio.create_task(_task())
    log.info("manual_backup_started", user_id=str(user.id))
    return {"status": "started"}


@router.get(
    "/admin/backups/{backup_id}/download",
    summary="Download a backup's pg_dump artifact (admin-only)",
)
async def download_backup(
    backup_id: int,
    artifact: str = Query(default="db", pattern="^(db|keycloak|openfga|env)$"),
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(_require_admin),
) -> StreamingResponse:
    """Stream one artifact of a backup set for the runbook / an off-host copy. `artifact` selects which one:
    db = talusgis [default], keycloak = identity, openfga = authz, env = the `.env` lifeboat (Talus 45; always
    age-encrypted ciphertext — decrypt off-host with the private key). 404 if the backup row, the chosen
    artifact, or the object itself is gone; 502 if the backup store is unreachable. (Media isn't downloadable
    here — it is copied from the backup destination; see the restore runbook.)"""
    key_col, size_col = f"{artifact}_object_key", f"{artifact}_size_bytes"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {key_col} AS object_key, {size_col} AS size_bytes "  # noqa: S608 — artifact is regex-pinned
            "FROM platform.backups WHERE id = %s",
            (backup_id,),
        )
        row = await cur.fetchone()
    if row is None or not row["object_key"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no {artifact} artifact for this backup",
        )

    key = row["object_key"]
    try:
        # The CURRENT destination, resolved per request (no restart). An artifact that lives only at a
        # previous destination is a legible 404 (F2) — the runbook covers retrieving from an old target.
        store = await resolve_backup_store(conn)
        chunks = await run_in_threadpool(open_backup_stream, store, key)
    except MediaObjectMissing:
        log.warning("backup_artifact_missing", key=key)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backup artifact not found in store")
    except Exception as exc:  # noqa: BLE001 — store down/misconfigured, decrypt failure, connection failure
        log.warning("backup_store_unavailable", key=key, detail=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="backup store unreachable")

    filename = key.rsplit("/", 1)[-1] or f"talus-{artifact}-{backup_id}.dump"
    return StreamingResponse(
        chunks,  # generator closes its file on completion or client disconnect
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(row["size_bytes"]),
        },
    )
