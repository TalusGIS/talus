"""Talus 27 — human user administration (admin-gated).

The Admin → Users surface: a thin in-portal flow to onboard/offboard PEOPLE without the Keycloak
console. It extends the Talus 19 provisioner seam from service-account *clients* to interactive
*users*. The portal ORCHESTRATES + RECORDS; Keycloak ISSUES the identity + the one-time temporary
password (shown to the admin ONCE — never stored by Talus, recovered by reset not retrieval).

Create ordering (mirrors service_accounts.py / publisher.py's "visible, recoverable state"
discipline): Keycloak user (the identity) + temp password -> platform.users principal row (the
forced NOT-NULL FK target) -> realm role -> OpenFGA tuples (org membership + group memberships). A
failure partway leaves a fail-closed result — the user can't read anything (default-deny) until its
row + membership land, and an un-provisioned sub is rejected by auth.py (no_platform_user_row). The
temp password is returned only on full success.

Roles/groups are the EXISTING set (no new permission primitives): a single realm-role tier +
membership in registered platform.groups. Deliberately NO custom-role builder, NO per-permission
editor, NO self-service signup (North-Star drift guard — Keycloak stays the identity store; this is
the thin admin surface over it). Every endpoint is behind _require_admin.

This commit (c1) ships list + create; disable/re-enable, role/group edit, and offboard-reassign land
in c2 (decision-user-admin.md §5).
"""
import csv
import io
import re
import secrets
import string
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection, errors as pg_errors
from pydantic import BaseModel, field_validator

from .. import admin_audit, keycloak_admin
from ..sqlsource import service as sql_pulls
from ..authz import (
    _ORG_OBJECT,
    _openfga_delete,
    _openfga_list_objects,
    _openfga_read_user_groups,
    _openfga_write,
    transfer_object_owner,
)
from ..config import settings
from ..db import get_db
from ..deps import _require_admin
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["users"])

_MAX_NAME_LEN = 200
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._@+-]+$")

# The assignable realm-role tier, LOW -> HIGH precedence (a user holds exactly one; the list maps
# a sub to its highest if Keycloak ever shows several). The four D1 roles from bootstrap-7b; the
# legacy talus-editor boolean is deliberately NOT offered (decision-user-admin.md F3).
_ASSIGNABLE_ROLES: tuple[str, ...] = (
    "talus-viewer",
    settings.keycloak_data_editor_role,  # talus-data-editor
    "talus-publisher",
    "talus-admin",
)
_ROLE_SET = frozenset(_ASSIGNABLE_ROLES)


def _gen_temp_password() -> str:
    """A strong, URL-safe one-time temp password. Typeable once at first login; the user is forced to
    reset it (Keycloak temporary=true). Never stored by Talus.

    Talus 48: the realm now ships a password policy (length(12) + upper/lower/digit), and Keycloak
    VALIDATES admin reset-password against it (a non-compliant value 400s). A bare token_urlsafe(16)
    lacks a digit ~2.6% of the time, which would intermittently fail single-create AND bulk import —
    so we GUARANTEE one of each required class up front; token_urlsafe supplies the length+entropy."""
    return (
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.token_urlsafe(16)
    )


# --- request / response models --------------------------------------------------


class GroupRef(BaseModel):
    group_id: str
    display_name: str


class UserCreate(BaseModel):
    username: str
    email: str
    display_name: str
    role: str
    # platform.groups ids this user joins (OpenFGA `member` tuples). May be empty. Each must exist.
    groups: list[str] = []
    # Talus 48 (SSO): a federated/SSO user gets NO local password — they authenticate ONLY through a
    # linked identity provider (Keycloak first-broker-login links by trusted email on first sign-in).
    # The platform.users row + role + org/group tuples are written exactly as for a local user, so the
    # admin-provision gate (auth.py: no platform.users row -> 401) holds identically. Default False
    # keeps the local-password path (and the bulk importer) unchanged.
    federated: bool = False

    @field_validator("username")
    @classmethod
    def _v_username(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("username must not be empty")
        if len(v) > _MAX_NAME_LEN:
            raise ValueError(f"username must be at most {_MAX_NAME_LEN} characters")
        if not _USERNAME_RE.match(v):
            raise ValueError("username may contain only letters, digits, and . _ - @ +")
        return v

    @field_validator("email")
    @classmethod
    def _v_email(cls, v: str) -> str:
        v = v.strip()
        if not v or "@" not in v or "." not in v.split("@")[-1] or len(v) > _MAX_NAME_LEN:
            raise ValueError("a valid email address is required")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            raise ValueError("email must not contain control characters")
        return v

    @field_validator("display_name")
    @classmethod
    def _v_display_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("display name must not be empty")
        if len(v) > _MAX_NAME_LEN:
            raise ValueError(f"display name must be at most {_MAX_NAME_LEN} characters")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            raise ValueError("display name must not contain control characters")
        return v

    @field_validator("role")
    @classmethod
    def _v_role(cls, v: str) -> str:
        if v not in _ROLE_SET:
            raise ValueError(f"role must be one of: {', '.join(_ASSIGNABLE_ROLES)}")
        return v


class UserCreated(BaseModel):
    id: str
    username: str
    email: str
    display_name: str
    role: str
    groups: list[str]
    # The one-time temporary password — shown ONCE. Talus never stores or can re-display it; the
    # user must reset it at first login. A lost password is recovered by reset, not retrieval.
    # None for a federated/SSO user (Talus 48): they have no local credential to show.
    temp_password: str | None = None


class UserOut(BaseModel):
    id: str
    username: str
    email: str
    display_name: str
    created_at: Any
    disabled_at: datetime | None
    active: bool
    # NULL => provisioned but never signed in (the "Pending" summary metric, Talus 28).
    last_seen_at: datetime | None
    # None when Keycloak couldn't be reached to resolve roles (list degrades rather than failing).
    role: str | None
    groups: list[GroupRef]
    # F1 (decision-user-lifecycle.md): does this principal have a Keycloak identity at all? False =
    # an ATTRIBUTION-ONLY row — synthesised by bulk import so imported features have a valid
    # created_by, and unable to ever sign in. The page must not offer password/role actions on
    # those: there is no identity to give a password or a role to. True on every normal user.
    can_sign_in: bool = True


# --- role resolution (best-effort, bounded) -------------------------------------


async def _roles_by_sub() -> dict[str, str]:
    """Map each user sub -> its assigned role, via ONE Keycloak call per assignable role (four,
    regardless of user count). Iterating LOW->HIGH means a higher-precedence role overwrites, so a
    user who somehow holds several resolves to the strongest. Resilient: a per-role failure yields
    {} for that role, so the worst case is an unknown role, never a failed list."""
    out: dict[str, str] = {}
    for role in _ASSIGNABLE_ROLES:
        for sub in await keycloak_admin.subs_with_realm_role(role):
            out[sub] = role
    return out


# --- endpoints ------------------------------------------------------------------


async def gather_human_users(conn: AsyncConnection) -> list[UserOut]:
    """The human-user roster (role + groups + status + last_seen). Shared by the Users list AND the
    Talus 29 Member report so the two can never drift. Best-effort per-row groups: OpenFGA down ->
    that row's groups degrade to empty, never a failed list."""
    async with conn.cursor() as cur:
        # removed_at IS NULL = the ROSTER (V091). A removed row survives only to keep historical
        # created_by / updated_by / audit references resolvable — it is not a person on the books,
        # and the whole point of the feature is that this list stops growing while history does not.
        await cur.execute(
            "SELECT id, username, email, display_name, created_at, disabled_at, last_seen_at "
            "FROM platform.users WHERE principal_type = 'human' AND removed_at IS NULL "
            "ORDER BY COALESCE(display_name, username, email)"
        )
        rows = await cur.fetchall()
        await cur.execute("SELECT group_id, display_name FROM platform.groups")
        group_names = {r[0]: r[1] for r in await cur.fetchall()}

    roles = await _roles_by_sub()
    try:
        identities = await keycloak_admin.all_user_subs()
        have_identities = True
    except HTTPException:
        # Keycloak unreachable: degrade the FLAG, never the list (same posture as groups below).
        # Assume can_sign_in=True so a transient outage never hides real actions from an admin.
        identities, have_identities = set(), False

    out: list[UserOut] = []
    for uid, username, email, display_name, created_at, disabled_at, last_seen_at in rows:
        try:
            slugs = await _openfga_read_user_groups(uid)
        except HTTPException:
            slugs = []  # OpenFGA down — show the user without groups, don't fail the list
        groups = [GroupRef(group_id=s, display_name=group_names.get(s, s)) for s in slugs]
        out.append(
            UserOut(
                id=str(uid),
                username=username,
                email=email,
                display_name=display_name,
                created_at=created_at,
                disabled_at=disabled_at,
                active=disabled_at is None,
                last_seen_at=last_seen_at,
                role=roles.get(str(uid)),
                groups=groups,
                can_sign_in=(str(uid) in identities) if have_identities else True,
            )
        )
    return out


@router.get("/admin/users", summary="List human users with their role, groups, and status")
async def list_users(
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> dict[str, list[UserOut]]:
    return {"users": await gather_human_users(conn)}


async def _unknown_groups(conn: AsyncConnection, group_ids: list[str]) -> list[str]:
    """The group ids in `group_ids` that do NOT exist in the registry — so a caller can reject a
    create before any Keycloak side effect rather than writing a membership tuple to a phantom
    group. Empty list => all known. Shared by single-create and the bulk importer."""
    if not group_ids:
        return []
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT group_id FROM platform.groups WHERE group_id = ANY(%s)", (group_ids,)
        )
        known = {r[0] for r in await cur.fetchall()}
    return [g for g in group_ids if g not in known]


async def _provision_human(
    conn: AsyncConnection,
    username: str,
    email: str,
    display_name: str,
    role: str,
    group_ids: list[str],
    federated: bool = False,
) -> tuple[str, str | None]:
    """The four-step human-provision orchestration shared by single-create AND bulk import, so the
    two paths can never diverge: (1) Keycloak identity + one-time temp password -> (2) platform.users
    principal row (the forced NOT-NULL FK target) -> (3) realm role -> (4) OpenFGA org + group
    tuples. Returns (sub, temp_password) — temp_password is None for a federated/SSO user (no local
    credential). Failure modes are exactly create_user's: a Keycloak 409 (duplicate username/email)
    raises HTTPException(409); a grant that can't be written fails CLOSED (503). The caller MUST have
    validated `group_ids` exist (see `_unknown_groups`)."""
    # 1. Keycloak: the identity (409 if username/email taken). A LOCAL user gets a one-time temp
    #    password; a FEDERATED (SSO) user gets NONE — they authenticate only via the linked IdP, which
    #    first-broker-login links by trusted email (create_user sets emailVerified=true). The
    #    platform.users-row gate (auth.py) is the admin-provision guarantee either way.
    sub = await keycloak_admin.create_user(username, email, display_name)
    temp_password: str | None = None
    if not federated:
        temp_password = _gen_temp_password()
        await keycloak_admin.set_user_temp_password(sub, temp_password)

    # 2. platform.users principal row — the forced NOT-NULL FK target every write needs. A human
    #    carries NO kc_client_id (the V024 CHECK enforces it). ON CONFLICT keeps a retry idempotent.
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO platform.users "
            "(id, username, email, display_name, principal_type, kc_client_id) "
            "VALUES (%s, %s, %s, %s, 'human', NULL) "
            "ON CONFLICT (id) DO UPDATE SET "
            "username = EXCLUDED.username, email = EXCLUDED.email, "
            "display_name = EXCLUDED.display_name, principal_type = 'human'",
            (sub, username, email, display_name),
        )
    await conn.commit()

    # 3. Realm role: the chosen tier (POST is a no-op if already mapped).
    await keycloak_admin.assign_realm_role(sub, role)

    # 4. OpenFGA: org membership written AT CREATION (retires the manual seed-org-membership.sh) +
    #    any group memberships. Fails CLOSED (503) — a grant that can't be written must not report
    #    success. Humans only get org membership; service accounts never do (Talus 19).
    org_tuple = (f"user:{sub}", "member", _ORG_OBJECT)
    group_tuples = [(f"user:{sub}", "member", f"group:{g}") for g in group_ids]
    await _openfga_write([org_tuple, *group_tuples])
    return sub, temp_password


@router.post(
    "/admin/users",
    status_code=status.HTTP_201_CREATED,
    summary="Create a human user (temporary password shown once)",
)
async def create_user(
    payload: UserCreate,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> UserCreated:
    # Reject unknown groups up front (before any Keycloak side effect).
    unknown = await _unknown_groups(conn, payload.groups)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown group(s): {', '.join(sorted(unknown))}",
        )

    sub, temp_password = await _provision_human(
        conn, payload.username, payload.email, payload.display_name, payload.role, payload.groups,
        federated=payload.federated,
    )

    log.info(
        "user_created",
        sub=sub,
        username=payload.username,
        role=payload.role,
        groups=payload.groups,
        federated=payload.federated,
        actor=str(_admin.id),
    )
    return UserCreated(
        id=sub,
        username=payload.username,
        email=payload.email,
        display_name=payload.display_name,
        role=payload.role,
        groups=payload.groups,
        temp_password=temp_password,
    )


# --- bulk CSV import (decision-bulk-user-import.md) -----------------------------
#
# The at-scale onboarding sibling of single-create: parse a CSV, validate every row, and (on
# commit) create the valid ones, reusing _provision_human per row so the paths never diverge. The
# UI calls this twice — commit=false to preview, commit=true to create — and the server is the
# authority both times (no trust in a client-side pre-parse). Admin-gated; every row's OpenFGA write
# fails closed. No migration; reuses platform.users / Keycloak / OpenFGA.

_MAX_IMPORT_ROWS = 500

# Friendly CSV role labels -> realm role. The raw realm role is also accepted (see _resolve_role);
# a blank role defaults to Viewer (least privilege).
_ROLE_LABELS: dict[str, str] = {
    "viewer": "talus-viewer",
    "editor": settings.keycloak_data_editor_role,
    "publisher": "talus-publisher",
    "administrator": "talus-admin",
    "admin": "talus-admin",
}


def _has_ctrl(v: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in v)


def _email_ok(v: str) -> bool:
    """The same shape check as UserCreate._v_email, but returns a bool instead of raising (one row's
    bad email is a row error, not a request failure)."""
    v = v.strip()
    if not v or "@" not in v or "." not in v.split("@")[-1] or len(v) > _MAX_NAME_LEN:
        return False
    return not _has_ctrl(v)


def _resolve_role(raw: str) -> str | None:
    """A CSV `role` cell -> a realm role, or None if unrecognized. Blank -> Viewer (least
    privilege). Accepts a friendly label (case-insensitive) or the raw realm role string."""
    v = raw.strip()
    if v == "":
        return "talus-viewer"
    if v.lower() in _ROLE_LABELS:
        return _ROLE_LABELS[v.lower()]
    if v in _ROLE_SET:
        return v
    return None


class ImportRequest(BaseModel):
    csv_text: str
    # False => validate only (dry run, no writes); True => validate THEN create the valid rows.
    commit: bool = False


class ImportRowResult(BaseModel):
    line: int  # 1-based CSV data line (the header is line 0)
    username: str | None
    email: str | None
    display_name: str | None
    role: str | None
    groups: list[str]
    # validate phase: ok | skip_exists | error ;  commit phase: created | failed (| skip_exists)
    status: str
    message: str | None = None


class ImportCredential(BaseModel):
    username: str
    # The one-time temporary password (forced reset at first login). Returned ONCE in the commit
    # response for the UI's credentials-CSV download; never stored or re-retrievable by Talus.
    temp_password: str


class ImportSummary(BaseModel):
    total: int
    to_create: int  # rows that passed validation (validate phase)
    to_skip: int
    errors: int
    created: int  # rows actually provisioned (commit phase)
    failed: int


class ImportResult(BaseModel):
    committed: bool
    summary: ImportSummary
    rows: list[ImportRowResult]
    credentials: list[ImportCredential]


@router.post(
    "/admin/users/import",
    summary="Bulk-create human users from CSV (commit=false validates; commit=true creates)",
)
async def import_users(
    payload: ImportRequest,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> ImportResult:
    # --- parse + locate columns (a malformed/missing-column file is a whole-request 400) ---------
    try:
        raw_rows = list(csv.reader(io.StringIO(payload.csv_text)))
    except csv.Error as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"could not parse CSV: {exc}"
        )
    rows = [r for r in raw_rows if any(c.strip() for c in r)]  # drop blank lines
    if not rows:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="the CSV is empty")

    header = [h.strip().lower() for h in rows[0]]

    def col(*names: str) -> int | None:
        for n in names:
            if n in header:
                return header.index(n)
        return None

    i_name = col("full_name", "name", "display_name")
    i_user = col("username", "user")
    i_email = col("email", "e-mail")
    i_role = col("role")
    i_groups = col("groups", "group")
    missing = [
        label
        for label, idx in (("full_name", i_name), ("username", i_user), ("email", i_email))
        if idx is None
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"CSV is missing required column(s): {', '.join(missing)}",
        )

    body = rows[1:]
    if len(body) > _MAX_IMPORT_ROWS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"too many rows: {len(body)} (max {_MAX_IMPORT_ROWS} per import)",
        )

    # --- preload the registry + existing roster (one read each, not per-row) --------------------
    async with conn.cursor() as cur:
        await cur.execute("SELECT group_id, display_name FROM platform.groups")
        group_by_name: dict[str, list[str]] = {}
        for gid, dname in await cur.fetchall():
            group_by_name.setdefault((dname or "").strip().lower(), []).append(gid)
        await cur.execute(
            "SELECT username, lower(email) FROM platform.users WHERE principal_type = 'human'"
        )
        ex = await cur.fetchall()
    existing_usernames = {r[0] for r in ex}
    existing_emails = {r[1] for r in ex if r[1]}

    def cell(row: list[str], idx: int | None) -> str:
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    # --- validate every row ---------------------------------------------------------------------
    plan: list[dict[str, Any]] = []
    seen_users: set[str] = set()
    seen_emails: set[str] = set()
    for line, row in enumerate(body, start=1):
        username = cell(row, i_user)
        email = cell(row, i_email)
        display_name = cell(row, i_name)
        role_raw = cell(row, i_role)
        group_names = [g.strip() for g in cell(row, i_groups).split(";") if g.strip()]

        errs: list[str] = []
        if not display_name or len(display_name) > _MAX_NAME_LEN or _has_ctrl(display_name):
            errs.append("invalid full name")
        if not username:
            errs.append("username is required")
        elif len(username) > _MAX_NAME_LEN or not _USERNAME_RE.match(username):
            errs.append("invalid username")
        if not _email_ok(email):
            errs.append("invalid email")
        role_id = _resolve_role(role_raw)
        if role_id is None:
            errs.append(f"unknown role {role_raw!r}")
        group_ids: list[str] = []
        for gname in group_names:
            matches = group_by_name.get(gname.lower())
            if not matches:
                errs.append(f"unknown group {gname!r}")
            elif len(matches) > 1:
                errs.append(f"ambiguous group {gname!r}")
            else:
                group_ids.append(matches[0])
        if username and username in seen_users:
            errs.append("duplicate username in file")
        if email and email.lower() in seen_emails:
            errs.append("duplicate email in file")
        if username:
            seen_users.add(username)
        if email:
            seen_emails.add(email.lower())

        entry: dict[str, Any] = {
            "line": line,
            "username": username or None,
            "email": email or None,
            "display_name": display_name or None,
            "role": role_raw.strip() or "Viewer",
            "groups": group_names,
            "role_id": role_id,
            "group_ids": group_ids,
        }
        if errs:
            entry["status"] = "error"
            entry["message"] = "; ".join(errs)
        elif username in existing_usernames or (email and email.lower() in existing_emails):
            entry["status"] = "skip_exists"
            entry["message"] = "a user with that username or email already exists"
        else:
            entry["status"] = "ok"
            entry["message"] = None
        plan.append(entry)

    # --- commit: create only the rows that passed validation ------------------------------------
    credentials: list[ImportCredential] = []
    if payload.commit:
        for entry in plan:
            if entry["status"] != "ok":
                continue
            try:
                sub, temp_password = await _provision_human(
                    conn,
                    entry["username"],
                    entry["email"],
                    entry["display_name"],
                    entry["role_id"],
                    entry["group_ids"],
                )
                entry["status"] = "created"
                entry["message"] = None
                credentials.append(
                    ImportCredential(username=entry["username"], temp_password=temp_password)
                )
                log.info(
                    "user_created",
                    sub=sub,
                    username=entry["username"],
                    role=entry["role_id"],
                    groups=entry["group_ids"],
                    actor=str(_admin.id),
                    via="bulk_import",
                )
            except HTTPException as exc:
                # A duplicate that slipped past the pre-check (created between validate and commit)
                # is a clean skip, not a failure — keeps a re-run after a partial import tidy.
                if exc.status_code == status.HTTP_409_CONFLICT:
                    entry["status"] = "skip_exists"
                    entry["message"] = "a user with that username or email already exists"
                else:
                    entry["status"] = "failed"
                    entry["message"] = str(exc.detail)
            except Exception as exc:  # noqa: BLE001 — one row's failure must not sink the batch
                entry["status"] = "failed"
                entry["message"] = str(exc)

    def n(*statuses: str) -> int:
        return sum(1 for e in plan if e["status"] in statuses)

    summary = ImportSummary(
        total=len(plan),
        to_create=n("ok"),
        to_skip=n("skip_exists"),
        errors=n("error"),
        created=n("created"),
        failed=n("failed"),
    )
    log.info(
        "users_bulk_import",
        committed=payload.commit,
        total=summary.total,
        to_create=summary.to_create,
        created=summary.created,
        skipped=summary.to_skip,
        errors=summary.errors,
        failed=summary.failed,
        actor=str(_admin.id),
    )
    return ImportResult(
        committed=payload.commit,
        summary=summary,
        rows=[ImportRowResult(**{k: e[k] for k in ImportRowResult.model_fields}) for e in plan],
        credentials=credentials,
    )


# --- c2: offboard + edit (disable/enable, reset, role/groups, reassign) ----------


class StatusResult(BaseModel):
    id: str
    active: bool
    disabled_at: datetime | None


class PasswordReset(BaseModel):
    id: str
    temp_password: str


class RoleUpdate(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def _v_role(cls, v: str) -> str:
        if v not in _ROLE_SET:
            raise ValueError(f"role must be one of: {', '.join(_ASSIGNABLE_ROLES)}")
        return v


class GroupsUpdate(BaseModel):
    groups: list[str] = []


class OwnedContent(BaseModel):
    maps: int
    layers: int


class ReassignRequest(BaseModel):
    successor_id: str


class ReassignResult(BaseModel):
    from_user: str
    to_user: str
    moved_maps: list[str]
    moved_layers: list[str]
    # Items whose transfer errored mid-loop (each {type,id,error}); a re-run retries only these,
    # since a successful move removes the item from the leaver's owned set (idempotent).
    failed: list[dict[str, str]]


async def _require_human(conn: AsyncConnection, user_id: str) -> str:
    """400 for a malformed id, 404 if no such principal, 400 if it's a service account, 409 if the
    account was REMOVED. Returns the target's display label (for messages/audit). Every mutating
    endpoint passes through this so a user-admin action can never touch a service account, a
    phantom id, or a tombstone.

    The removed check lives HERE rather than in each endpoint (decision-user-lifecycle.md F4): a
    removed row is a record, not a principal, and every mutation is meaningless on it. Without this
    `enable_user` would clear `disabled_at` on a tombstone — leaving removed_at set and disabled_at
    NULL, an incoherent state for an account whose identity no longer exists. One gate, not six
    checks that could drift apart."""
    try:
        UUID(user_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid user id")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT principal_type, COALESCE(display_name, username, email), removed_at "
            "FROM platform.users WHERE id = %s",
            (user_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    if row[0] != "human":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="not a human user"
        )
    if row[2] is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this account was removed from the directory and cannot be changed",
        )
    return row[1]


async def _assert_not_last_admin(conn: AsyncConnection, target_sub: str) -> None:
    """Refuse to strip admin from the org's LAST enabled administrator (a disable or a demote).
    No-op unless the target CURRENTLY holds talus-admin. FAIL-SAFE (F7): the admin set is read
    strict — any Keycloak read failure raises (deny), never silently permits a lockout."""
    admins = await keycloak_admin.subs_with_realm_role("talus-admin", strict=True)
    if target_sub not in admins:
        return  # not an admin -> disabling/demoting them can't lock the org out
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id::text FROM platform.users "
            "WHERE principal_type = 'human' AND disabled_at IS NULL"
        )
        active = {r[0] for r in await cur.fetchall()}
    enabled_admins = admins & active
    if enabled_admins <= {target_sub}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cannot disable or demote the last remaining administrator",
        )


@router.post("/admin/users/{user_id}/disable", summary="Disable (offboard) a user")
async def disable_user(
    user_id: str,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> StatusResult:
    await _require_human(conn, user_id)
    await _assert_not_last_admin(conn, user_id)
    # Keycloak enabled=false is the real enforcement — do it FIRST, then mirror the column. A
    # failure after the Keycloak write leaves the user disabled (safe) with a stale-active column
    # that self-heals on a re-run (cosmetic divergence only, F2).
    # An identity-less principal returns False rather than raising (decision-user-lifecycle.md F2):
    # there is nothing to disable in Keycloak because they could never sign in, and the column
    # write below is the whole meaning of "disabled" for them. Raising here used to abort before
    # that write, so Disable was impossible on 139 of 147 rows and said "user not found" about a
    # row the admin was looking at.
    await keycloak_admin.set_user_enabled(user_id, False)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.users SET disabled_at = now() WHERE id = %s AND disabled_at IS NULL",
            (user_id,),
        )
        await cur.execute("SELECT disabled_at FROM platform.users WHERE id = %s", (user_id,))
        disabled_at = (await cur.fetchone())[0]
    # decision-sql-source-pull.md §11.2 L1: the user's SQL-source pulls go manual + stale (live runs canceled)
    # in the same transaction — a disabled person keeps no background write channel. Commits.
    await sql_pulls.deactivate_owner_pulls(conn, user_id, event="disabled", actor=_admin.id)
    await conn.commit()
    log.info("user_disabled", sub=user_id, actor=str(_admin.id))
    await admin_audit.record(
        conn, actor_id=_admin.id, action="user_disabled", target_type="user", target_id=user_id,
    )
    return StatusResult(id=user_id, active=False, disabled_at=disabled_at)


@router.post("/admin/users/{user_id}/enable", summary="Re-enable a disabled user")
async def enable_user(
    user_id: str,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> StatusResult:
    await _require_human(conn, user_id)
    await keycloak_admin.set_user_enabled(user_id, True)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.users SET disabled_at = NULL WHERE id = %s", (user_id,)
        )
    await conn.commit()
    log.info("user_enabled", sub=user_id, actor=str(_admin.id))
    await admin_audit.record(
        conn, actor_id=_admin.id, action="user_enabled", target_type="user", target_id=user_id,
    )
    return StatusResult(id=user_id, active=True, disabled_at=None)


@router.post(
    "/admin/users/{user_id}/reset-password",
    summary="Reset a user's password (new temporary password shown once)",
)
async def reset_password(
    user_id: str,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> PasswordReset:
    await _require_human(conn, user_id)
    temp_password = _gen_temp_password()
    await keycloak_admin.set_user_temp_password(user_id, temp_password)
    log.info("user_password_reset", sub=user_id, actor=str(_admin.id))
    return PasswordReset(id=user_id, temp_password=temp_password)


@router.put("/admin/users/{user_id}/role", summary="Set a user's role (single tier)")
async def update_role(
    user_id: str,
    payload: RoleUpdate,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> dict[str, str]:
    await _require_human(conn, user_id)
    # Demotion lockout: only when moving AWAY from admin (promoting/keeping admin is always safe).
    if payload.role != "talus-admin":
        await _assert_not_last_admin(conn, user_id)
    await keycloak_admin.set_user_realm_role(user_id, payload.role, _ASSIGNABLE_ROLES)
    if not sql_pulls.role_may_run_pulls(payload.role):
        # §11.2 L3: the role IS the right to run pulls — a viewer/editor's pulls go manual + stale. Promotion
        # back resumes nothing (L2). Admin ↔ publisher is not a demotion for pulls.
        await sql_pulls.deactivate_owner_pulls(conn, user_id, event="role_changed", actor=_admin.id, role=payload.role)
    log.info("user_role_changed", sub=user_id, role=payload.role, actor=str(_admin.id))
    await admin_audit.record(
        conn, actor_id=_admin.id, action="user_role_changed",
        target_type="user", target_id=user_id, detail={"role": payload.role},
    )
    return {"id": user_id, "role": payload.role}


@router.put("/admin/users/{user_id}/groups", summary="Set a user's group memberships")
async def update_groups(
    user_id: str,
    payload: GroupsUpdate,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> dict[str, Any]:
    await _require_human(conn, user_id)
    desired = set(payload.groups)
    if desired:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT group_id FROM platform.groups WHERE group_id = ANY(%s)", (list(desired),)
            )
            known = {r[0] for r in await cur.fetchall()}
        unknown = sorted(desired - known)
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown group(s): {', '.join(unknown)}",
            )
    # Reconcile against current memberships (org membership is a separate tuple, untouched). Adds
    # fail closed; an "already exists" add is idempotent success (authz._openfga_write).
    current = set(await _openfga_read_user_groups(UUID(user_id)))
    to_add = sorted(desired - current)
    to_remove = sorted(current - desired)
    if to_add:
        await _openfga_write([(f"user:{user_id}", "member", f"group:{g}") for g in to_add])
    if to_remove:
        await _openfga_delete([(f"user:{user_id}", "member", f"group:{g}") for g in to_remove])
    log.info(
        "user_groups_changed", sub=user_id, added=to_add, removed=to_remove, actor=str(_admin.id)
    )
    return {"id": user_id, "groups": sorted(desired)}


@router.get(
    "/admin/users/{user_id}/owned-content",
    summary="Count the maps + layers a user owns (for the reassign dialog)",
)
async def owned_content(
    user_id: str,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> OwnedContent:
    await _require_human(conn, user_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM platform.web_maps WHERE owner_id = %s", (user_id,)
        )
        maps = (await cur.fetchone())[0]
    layers = len(await _openfga_list_objects(UUID(user_id), "owner", "layer"))
    return OwnedContent(maps=maps, layers=layers)


class DeleteResult(BaseModel):
    id: str
    # "deleted" = the platform.users row is GONE (nothing referenced it).
    # "removed" = the Keycloak identity was deleted and the row was tombstoned, because history
    #             still points at it. The UI must say which happened — never imply an erase that
    #             did not occur (decision-user-lifecycle.md F5).
    outcome: str
    identity_deleted: bool


@router.delete(
    "/admin/users/{user_id}",
    summary="Delete a departed user (disabled + owns nothing); hard-deletes or tombstones",
)
async def delete_user(
    user_id: str,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> DeleteResult:
    """Remove a departed person from the directory.

    The gate is the familiar shape: refuse while they still OWN anything, say what they
    own, and require a Transfer first. Ownership is transferable; feature attribution and audit
    trails are NOT (reassigning `created_by` would claim a different person made that edit), which
    is why "transfer everything then hard-delete" cannot be the whole answer here.

    Two honest outcomes once they own nothing:
      * nothing references them  -> the row is hard-deleted. Gone.
      * history references them  -> the KEYCLOAK IDENTITY IS DELETED (irreversible; username
        freed) and the row is tombstoned with removed_at so it leaves the roster, KEEPING ITS NAME
        (F4 — institutional memory over erasure for a records-retention audience).

    ★ Which outcome applies is decided by ATTEMPTING the delete and catching the FK violation, not
    by enumerating referencing tables. `assets.tal_*` tables are created at RUNTIME per layer, so
    any static list would be wrong the moment someone publishes a layer. The database is the only
    thing that actually knows.
    """
    await _require_human(conn, user_id)

    # 1. Must already be disabled — the PAT model (revoke, then Delete). Removing a WORKING
    #    account must never be one click.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT disabled_at, COALESCE(display_name, username) "
            "FROM platform.users WHERE id = %s",
            (user_id,),
        )
        row = await cur.fetchone()
    # (a removed row never reaches here — _require_human above rejects it with a 409)
    if row[0] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="disable this account first, then delete it",
        )
    label = row[1]

    # 2. Never empty the admin set (same guard the disable path runs).
    await _assert_not_last_admin(conn, user_id)

    # 3. The ownership gate. Name what they own — "you must transfer their material first" is only
    #    actionable if the admin can see what "their material" is.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM platform.web_maps WHERE owner_id = %s", (user_id,)
        )
        maps = (await cur.fetchone())[0]
    layers = len(await _openfga_list_objects(UUID(user_id), "owner", "layer"))
    if maps or layers:
        owned = " and ".join(
            p for p in (
                f"{maps} map{'s' if maps != 1 else ''}" if maps else "",
                f"{layers} layer{'s' if layers != 1 else ''}" if layers else "",
            ) if p
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{label} still owns {owned}. Transfer their content to another user first, "
                "then delete the account."
            ),
        )

    # 4. The identity dies here, irreversibly. Ordered first so a failure between the two steps
    #    leaves an identity-less row still on the roster — exactly the state F2 now handles, which
    #    a re-run completes.
    identity_deleted = await keycloak_admin.delete_user(user_id)

    # 4b. §11.2 L4: a personal database credential never survives its person — the leaver's SQL-source
    #     connections go with the identity (cascading any pull, run and diff state; their layers unlocked
    #     after). Ownership of pulled layers was already forced through Transfer by the gate above, which
    #     detached those pulls. Commits.
    await sql_pulls.purge_owner_connections(conn, user_id, actor=_admin.id)

    # 5. Hard delete if the database permits it; tombstone if history does not. SAVEPOINT so the
    #    FK violation does not poison the transaction we still need for the fallback.
    outcome = "deleted"
    async with conn.cursor() as cur:
        await cur.execute("SAVEPOINT try_hard_delete")
        try:
            await cur.execute("DELETE FROM platform.users WHERE id = %s", (user_id,))
            await cur.execute("RELEASE SAVEPOINT try_hard_delete")
        except pg_errors.ForeignKeyViolation:
            await cur.execute("ROLLBACK TO SAVEPOINT try_hard_delete")
            await cur.execute(
                "UPDATE platform.users SET removed_at = now(), disabled_at = COALESCE(disabled_at, now()) "
                "WHERE id = %s",
                (user_id,),
            )
            outcome = "removed"
    await conn.commit()

    log.info(
        "user_deleted", sub=user_id, outcome=outcome,
        identity_deleted=identity_deleted, actor=str(_admin.id),
    )
    await admin_audit.record(
        conn, actor_id=_admin.id, action="user_deleted", target_type="user", target_id=user_id,
        detail={"outcome": outcome, "identityDeleted": identity_deleted, "label": label},
    )
    return DeleteResult(id=user_id, outcome=outcome, identity_deleted=identity_deleted)


@router.post(
    "/admin/users/{user_id}/reassign-content",
    summary="Reassign ALL of a user's owned content to one successor (offboarding)",
)
async def reassign_content(
    user_id: str,
    payload: ReassignRequest,
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> ReassignResult:
    await _require_human(conn, user_id)
    successor = payload.successor_id
    try:
        UUID(successor)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid successor id")
    if successor == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="cannot reassign to the same user"
        )
    # The successor must be an ACTIVE human (never a service account or a disabled user).
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT principal_type, disabled_at FROM platform.users WHERE id = %s", (successor,)
        )
        s = await cur.fetchone()
    if s is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown successor")
    if s[0] != "human":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="successor must be a human user"
        )
    if s[1] is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="successor is disabled"
        )

    # Enumerate the leaver's owned content: maps via owner_id (DB), layers via the owner tuple
    # (FGA). Ownerless system layers are not owned by anyone, so they never appear here.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id::text FROM platform.web_maps WHERE owner_id = %s", (user_id,)
        )
        map_ids = [r[0] for r in await cur.fetchall()]
    layer_ids = await _openfga_list_objects(UUID(user_id), "owner", "layer")

    moved_maps: list[str] = []
    moved_layers: list[str] = []
    failed: list[dict[str, str]] = []

    # Per-item, fail-safe (transfer_object_owner writes the new owner before dropping the old). A
    # failed item is collected and the loop continues; the op is re-runnable (a moved item drops
    # out of the leaver's owned set next time). For a map, the FGA tuple move AND the owner_id
    # update must both land — a divergence self-heals on a re-run.
    for mid in map_ids:
        try:
            await transfer_object_owner(f"web_map:{mid}", successor)
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE platform.web_maps SET owner_id = %s WHERE id = %s", (successor, mid)
                )
            await conn.commit()
            moved_maps.append(mid)
        except Exception as exc:  # noqa: BLE001 — collect + continue; re-run retries the failures
            failed.append({"type": "web_map", "id": mid, "error": str(exc)})
    for lid in layer_ids:
        try:
            await transfer_object_owner(f"layer:{lid}", successor)
            # §11.2 L4: a pulled layer is detached at transfer time — the successor gets an editable copy.
            await sql_pulls.detach_layer_pull(conn, lid, actor=_admin.id, previous_owner=user_id, new_owner=successor)
            moved_layers.append(lid)
        except Exception as exc:  # noqa: BLE001
            failed.append({"type": "layer", "id": lid, "error": str(exc)})

    log.info(
        "user_content_reassigned",
        from_user=user_id,
        to_user=successor,
        moved_maps=len(moved_maps),
        moved_layers=len(moved_layers),
        failed=len(failed),
        actor=str(_admin.id),
    )
    await admin_audit.record(
        conn, actor_id=_admin.id, action="user_content_reassigned", target_type="user",
        target_id=user_id,
        detail={"successor": successor, "movedMaps": len(moved_maps),
                "movedLayers": len(moved_layers), "failed": len(failed)},
    )
    return ReassignResult(
        from_user=user_id,
        to_user=successor,
        moved_maps=moved_maps,
        moved_layers=moved_layers,
        failed=failed,
    )
