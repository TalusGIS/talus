"""Talus 43 — effective backup settings (the COALESCE-over-env reader).

The editable backup knobs (cadence / retention / age recipients / scheduled on-off) live in the
`platform.backup_settings` singleton (V042) as NULLABLE overrides. The EFFECTIVE value of each knob is
`COALESCE(db_override, env_default)` (decision-backup-settings.md §2 F-precedence): env stays the live
deploy default, the UI optionally overrides a knob, and "reset to default" clears the column (falls back
to env). This module is that resolution, in one place, shared by:

  - the scheduler (api/app/maintenance.py) — reads the effective cadence + scheduled-enabled per tick,
  - the backup jobs (api/app/backup.py) — read the effective recipients + retention,
  - the admin Backups endpoint (api/app/routers/backups.py) — surfaces effective values + which knobs are
    overridden (so the panel can show "default" vs "custom").

The backup DESTINATION (decision-backup-destination.md) resolves here too: the V090 override group
(s3_endpoint / s3_bucket / s3_access_key / s3_secret_key ciphertext) over the BACKUP_S3_* env trio —
get_effective_backup_destination(). Resolved PER CALL, never cached: that is the entire no-restart
mechanism (the old lru_cache accessor could not serve a DB-driven value).

No cycle: this imports only config + psycopg + secrets (which imports config); maintenance/backup/
routers import IT.
"""
from __future__ import annotations

from dataclasses import dataclass

from psycopg import AsyncConnection

from .config import settings
from .secrets import decrypt_secret

# The five editable knobs, by their platform.backup_settings column names — also the membership of the
# `overrides` set returned below (a knob is "overridden" iff its column is non-NULL / non-blank).
OVERRIDABLE_KNOBS = (
    "interval_seconds",
    "retention_days",
    "retention_min_keep",
    "age_recipients",
    "scheduled_enabled",
    "include_env",
)


@dataclass(frozen=True)
class EffectiveBackupSettings:
    """The resolved (override-or-env) backup knobs. `overrides` names the knobs currently driven by a DB
    override (the rest fall back to env) — for the admin panel's default/custom affordance only; the
    scheduler/jobs use the resolved values directly."""

    interval_seconds: int
    retention_days: int
    retention_min_keep: int
    age_recipients: tuple[str, ...]
    scheduled_enabled: bool
    overrides: frozenset[str]
    # Talus 45: the include-`.env`-lifeboat TOGGLE (default last so existing keyword constructions still
    # work). This is only the setting — the RUNTIME inclusion is `include_env AND encryption_enabled AND the
    # file is readable` (the fail-closed AND), computed by the caller (run_backup / the GET config).
    include_env: bool = False

    @property
    def encryption_enabled(self) -> bool:
        """True when ≥1 recipient is effective => artifacts are age-encrypted before upload."""
        return bool(self.age_recipients)


def _parse_recipients(text: str | None) -> tuple[str, ...]:
    """Comma-joined `age1…` text -> a trimmed tuple of keys (empty when None/blank)."""
    if not text:
        return ()
    return tuple(r.strip() for r in text.split(",") if r.strip())


async def get_effective_backup_settings(conn: AsyncConnection) -> EffectiveBackupSettings:
    """Resolve the singleton over the BACKUP_*/MAINTENANCE_* env defaults (one cheap SELECT). A NULL (or
    blank, for recipients) override falls back to env and is NOT flagged in `overrides`; a present override
    wins and IS flagged. Reads on the connection it is given — the scheduler passes its lock-holding conn,
    so the per-tick read costs nothing extra. Falls back to all-env if the row is somehow missing (defensive;
    V042 seeds exactly one row)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT interval_seconds, retention_days, retention_min_keep, age_recipients, scheduled_enabled, "
            "include_env FROM platform.backup_settings WHERE id = true"
        )
        row = await cur.fetchone()
    iv, rd, rmk, ar, se, ie = row if row is not None else (None, None, None, None, None, None)

    overrides: set[str] = set()
    if iv is not None:
        overrides.add("interval_seconds")
    if rd is not None:
        overrides.add("retention_days")
    if rmk is not None:
        overrides.add("retention_min_keep")
    if se is not None:
        overrides.add("scheduled_enabled")
    if ie is not None:
        overrides.add("include_env")
    # age_recipients: NULL OR blank => not an override (use env). A non-blank string is a real override
    # (the PUT stores NULL, never '', when the recipient list is empty — so blank can only be legacy data).
    ar_clean = ar.strip() if ar is not None else ""
    if ar_clean:
        overrides.add("age_recipients")

    return EffectiveBackupSettings(
        interval_seconds=iv if iv is not None else settings.backup_interval_seconds,
        retention_days=rd if rd is not None else settings.backup_retention_days,
        retention_min_keep=rmk if rmk is not None else settings.backup_retention_min_keep,
        age_recipients=(
            _parse_recipients(ar_clean) if ar_clean else tuple(settings.backup_age_recipients_list)
        ),
        scheduled_enabled=se if se is not None else settings.backup_enabled,
        overrides=frozenset(overrides),
        include_env=ie if ie is not None else settings.backup_include_env,
    )


# ---- The backup DESTINATION (decision-backup-destination.md) -------------------------------------

@dataclass(frozen=True)
class EffectiveBackupDestination:
    """The resolved backup destination: the V090 override group when set, else the BACKUP_S3_* env
    trio, else the local default. `kind` mirrors the env twin's display semantics exactly: an
    endpoint being SET makes it 's3' even if the credential set is partial — the partial-config
    loud raise stays where it always was (storage.build_backup_storage), so the panel stays
    renderable while the backup job surfaces the error row. `secret_key` is plaintext IN APP
    MEMORY ONLY, and only when resolved with_secret=True (backup run / download / connection
    test); the GET-config path resolves without it and never touches the ciphertext."""

    kind: str            # 's3' | 'local'
    overridden: bool     # True = the DB override group drives it; False = env
    endpoint: str = ""   # s3 only
    bucket: str = ""     # effective bucket (subtree name for local)
    access_key: str = ""
    secret_key: str = ""     # empty when with_secret=False, or for the local kind
    secret_key_set: bool = False  # an override secret EXISTS (for the panel; never the value)

    @property
    def is_offhost(self) -> bool:
        return self.kind == "s3"

    @property
    def label(self) -> str:
        """The honest-DR label (catalog rows / panel / error rows): a same-host copy says so
        plainly (B6 — a copy is not disaster recovery)."""
        if self.kind == "s3":
            return f"s3://{self.bucket} (off-host)"
        return f"file://{settings.storage_root}/{self.bucket} (same-host — not DR)"


async def get_effective_backup_destination(
    conn: AsyncConnection, *, with_secret: bool = False,
) -> EffectiveBackupDestination:
    """Resolve the destination override-or-env, PER CALL (never cached — the no-restart mechanism).
    with_secret=True decrypts the override's Fernet ciphertext (SecretKeyUnavailable /
    SecretDecryptError propagate — a backup run turns them into a visible error row, per the
    secrets.py read-path rule: never proceed without the credential). The V090 CHECK guarantees
    the override group is never partial at rest."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT s3_endpoint, s3_bucket, s3_access_key, s3_secret_key "
            "FROM platform.backup_settings WHERE id = true"
        )
        row = await cur.fetchone()
    endpoint, bucket, access_key, secret_ct = row if row is not None else (None, None, None, None)

    if endpoint:  # the DB override group (V090 CHECK: access + secret are set with it)
        return EffectiveBackupDestination(
            kind="s3",
            overridden=True,
            endpoint=endpoint,
            bucket=(bucket or settings.backup_bucket),
            access_key=access_key or "",
            secret_key=decrypt_secret(secret_ct) if (with_secret and secret_ct) else "",
            secret_key_set=secret_ct is not None,
        )
    if settings.backup_s3_endpoint:  # env off-host (partial env still raises at construction)
        return EffectiveBackupDestination(
            kind="s3",
            overridden=False,
            endpoint=settings.backup_s3_endpoint,
            bucket=settings.backup_bucket,
            access_key=settings.backup_s3_access_key,
            secret_key=settings.backup_s3_secret_key if with_secret else "",
            secret_key_set=bool(settings.backup_s3_secret_key),
        )
    return EffectiveBackupDestination(
        kind="local", overridden=False, bucket=settings.backup_bucket,
    )
