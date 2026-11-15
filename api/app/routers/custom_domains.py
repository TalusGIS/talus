"""Reusable coded-value domains — the registry behind owner-defined `coded` fields (V046).

Slice 2a of the data-owner-independence arc (decision-coded-domains.md). A domain is a named, reusable
picklist of `code`/`label` values, owner-owned but **org-wide assignable**: any data owner may bind any
domain to their own coded field, while only the domain's **owner or an admin** may edit its values
(ESRI's governance). Editing a shared domain intentionally updates every bound field; soft-retire keeps
existing stored data valid (the V046 trigger accepts any DEFINED code, active or retired).

Resource is **`/custom-domains`** — named to steer clear of `GET /domains`, the retired built-ins'
picklist endpoint (the same collision class as the slice-1 `/fields`→`/custom-fields` fix; the name
outlives the collision so old clients can never conflate the two). Reads are org-wide (any
authenticated user, for the picker + the edit form); writes are owner-or-admin, fail-closed. Lifecycle:
a value still used by a feature can't be hard-deleted, and a domain still bound to a
field can't be deleted (also enforced by the V046 FK) — surfaced as friendly 409s, never an orphan.
"""
import re
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from psycopg import AsyncConnection, errors as pg_errors, sql
from psycopg.rows import dict_row
from pydantic import BaseModel, field_validator

from ..auth import _validated_claims, get_current_user
from ..authz import _has_admin_role
from ..db import get_db
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["custom-domains"])

_MAX_NAME = 120
_MAX_CODE = 100
_MAX_LABEL = 200
# Defense-in-depth: a layer_id read from layer_fields names a table only after this re-check, matching
# the "never trust, even proven tokens" discipline used everywhere else slot tables are named.
_SLOT_RE = re.compile(r"^tal_[0-9a-f]{8}$")


# --- validators (mirror the V046 CHECKs → friendly 422 before the DB raises) -----


def _clean(v: str, *, field: str, maxlen: int) -> str:
    v = v.strip()
    if not v:
        raise ValueError(f"{field} must not be empty")
    if len(v) > maxlen:
        raise ValueError(f"{field} must be at most {maxlen} characters")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
        raise ValueError(f"{field} must not contain control characters")
    return v


class ValueSeed(BaseModel):
    code: str
    label: str
    sortOrder: int = 0

    @field_validator("code")
    @classmethod
    def _vc(cls, v: str) -> str:
        return _clean(v, field="code", maxlen=_MAX_CODE)

    @field_validator("label")
    @classmethod
    def _vl(cls, v: str) -> str:
        return _clean(v, field="label", maxlen=_MAX_LABEL)


class DomainCreate(BaseModel):
    name: str
    description: str | None = None
    values: list[ValueSeed] = []  # optional initial values (the create-inline-from-field-designer flow)

    @field_validator("name")
    @classmethod
    def _vn(cls, v: str) -> str:
        return _clean(v, field="name", maxlen=_MAX_NAME)


class DomainUpdate(BaseModel):
    name: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _vn(cls, v: str | None) -> str | None:
        return _clean(v, field="name", maxlen=_MAX_NAME) if v is not None else v


class ValueCreate(ValueSeed):
    pass


class ValueUpdate(BaseModel):
    # code is IMMUTABLE (it is the stored value; changing it would orphan data — relabel/retire instead).
    label: str | None = None
    active: bool | None = None
    sortOrder: int | None = None

    @field_validator("label")
    @classmethod
    def _vl(cls, v: str | None) -> str | None:
        return _clean(v, field="label", maxlen=_MAX_LABEL) if v is not None else v


# --- gates -----------------------------------------------------------------------


async def _domain_owner(conn: AsyncConnection, domain_id: UUID) -> UUID:
    """The domain's owner id, or 404 if no such domain."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT owner_id FROM platform.domains WHERE id = %s", (str(domain_id),))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="domain not found")
    return row[0]


async def _assert_domain_editor(
    conn: AsyncConnection, claims: dict | None, user: User, domain_id: UUID
) -> None:
    """Edit gate: admin OR the domain's owner. Fails closed. (Resolves 404 first.)"""
    owner = await _domain_owner(conn, domain_id)
    if _has_admin_role(claims) or owner == user.id:
        return
    log.info(
        "authz_rejected", capability="custom-domains:edit",
        reason="not_admin_not_owner", user_id=str(user.id), domain=str(domain_id),
    )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to edit this domain")


def _value_row(r: dict) -> dict:
    return {
        "id": r["id"], "code": r["code"], "label": r["label"],
        "active": r["active"], "sortOrder": r["sort_order"],
    }


async def _values(conn: AsyncConnection, domain_id: str) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id::text AS id, code, label, active, sort_order "
            "FROM platform.domain_values WHERE domain_id = %s ORDER BY sort_order, code",
            (domain_id,),
        )
        return [_value_row(r) for r in await cur.fetchall()]


# --- domain endpoints ------------------------------------------------------------


@router.get("/custom-domains", summary="List all domains (org-wide; for the picker + the Domains page)")
async def list_domains(
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    is_admin = _has_admin_role(claims)
    me = str(user.id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT d.id::text AS id, d.name, d.description, d.owner_id::text AS owner_id, "
            "       COALESCE(u.display_name, u.username, u.email) AS owner_name, "
            "       (SELECT count(*) FROM platform.domain_values v WHERE v.domain_id = d.id AND v.active) AS value_count, "
            "       (SELECT count(*) FROM platform.layer_fields lf WHERE lf.domain_id = d.id) AS bound_field_count "
            "FROM platform.domains d LEFT JOIN platform.users u ON d.owner_id = u.id "
            "ORDER BY d.name"
        )
        rows = await cur.fetchall()
    return {
        "domains": [
            {
                "id": r["id"], "name": r["name"], "description": r["description"],
                "ownerId": r["owner_id"], "ownerName": r["owner_name"],
                "valueCount": r["value_count"], "boundFieldCount": r["bound_field_count"],
                "canEdit": is_admin or r["owner_id"] == me,  # server-authoritative; the API re-enforces
            }
            for r in rows
        ]
    }


@router.get(
    "/custom-domains/{domain_id}/usage",
    summary="Where a domain is used — the fields/layers bound to it (for the management page)",
)
async def domain_usage(
    domain_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT lf.layer_id, COALESCE(l.display_name, lf.layer_id) AS layer_name, "
            "       lf.field_key, lf.label AS field_label "
            "FROM platform.layer_fields lf JOIN platform.layers l ON lf.layer_id = l.layer_id "
            "WHERE lf.domain_id = %s ORDER BY layer_name, lf.field_key",
            (str(domain_id),),
        )
        rows = await cur.fetchall()
    return {
        "bindings": [
            {"layerId": r["layer_id"], "layerName": r["layer_name"],
             "fieldKey": r["field_key"], "fieldLabel": r["field_label"]}
            for r in rows
        ]
    }


@router.get("/custom-domains/{domain_id}", summary="One domain + its values (active and retired)")
async def get_domain(
    domain_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id::text AS id, name, description, owner_id::text AS owner_id "
            "FROM platform.domains WHERE id = %s",
            (str(domain_id),),
        )
        d = await cur.fetchone()
    if d is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="domain not found")
    return {
        "id": d["id"], "name": d["name"], "description": d["description"], "ownerId": d["owner_id"],
        "values": await _values(conn, str(domain_id)),
    }


@router.post("/custom-domains", status_code=status.HTTP_201_CREATED, summary="Create a domain (creator = owner)")
async def create_domain(
    payload: DomainCreate,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    actor = str(user.id)
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO platform.domains (owner_id, name, description, created_by, updated_by) "
                "VALUES (%(actor)s, %(name)s, %(desc)s, %(actor)s, %(actor)s) RETURNING id::text AS id",
                {"actor": actor, "name": payload.name, "desc": payload.description},
            )
            did = (await cur.fetchone())["id"]
            for i, v in enumerate(payload.values):
                await cur.execute(
                    "INSERT INTO platform.domain_values (domain_id, code, label, sort_order, created_by, updated_by) "
                    "VALUES (%(d)s, %(code)s, %(label)s, %(sort)s, %(actor)s, %(actor)s)",
                    {"d": did, "code": v.code, "label": v.label, "sort": v.sortOrder or (i * 10), "actor": actor},
                )
        await conn.commit()
    except pg_errors.UniqueViolation:
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a domain with that name already exists, or a duplicate code")
    log.info("custom_domain_mutation", action="create", domain_id=did, actor=actor)
    return await get_domain(UUID(did), conn, user)


@router.patch("/custom-domains/{domain_id}", summary="Edit a domain's name/description (owner or admin)")
async def update_domain(
    domain_id: UUID,
    payload: DomainUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _assert_domain_editor(conn, claims, user, domain_id)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no fields to update")
    sets = {"name": "name", "description": "description"}
    clauses = [f"{col} = %({attr})s" for attr, col in sets.items() if attr in fields]
    params: dict[str, Any] = {"id": str(domain_id), "actor": str(user.id)}
    for attr in sets:
        if attr in fields:
            params[attr] = fields[attr]
    clauses += ["updated_by = %(actor)s", "updated_at = now()"]
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE platform.domains SET {', '.join(clauses)} WHERE id = %(id)s", params
            )
        await conn.commit()
    except pg_errors.UniqueViolation:
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a domain with that name already exists")
    log.info("custom_domain_mutation", action="update", domain_id=str(domain_id), actor=str(user.id))
    return await get_domain(domain_id, conn, user)


@router.delete(
    "/custom-domains/{domain_id}", status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a domain — refused if still bound to a field (owner or admin)",
)
async def delete_domain(
    domain_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> Response:
    await _assert_domain_editor(conn, claims, user, domain_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM platform.layer_fields WHERE domain_id = %s LIMIT 1", (str(domain_id),)
        )
        bound = await cur.fetchone() is not None
    if bound:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="domain is bound to one or more fields; unbind them before deleting",
        )
    try:
        async with conn.cursor() as cur:  # CASCADE removes its values
            await cur.execute("DELETE FROM platform.domains WHERE id = %s", (str(domain_id),))
        await conn.commit()
    except pg_errors.ForeignKeyViolation:  # raced: a field bound it between the check and the delete
        await conn.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="domain is bound to one or more fields; unbind them before deleting",
        )
    log.info("custom_domain_mutation", action="delete", domain_id=str(domain_id), actor=str(user.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- value sub-resource ----------------------------------------------------------


@router.post(
    "/custom-domains/{domain_id}/values", status_code=status.HTTP_201_CREATED,
    summary="Add a value to a domain (owner or admin)",
)
async def add_value(
    domain_id: UUID,
    payload: ValueCreate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _assert_domain_editor(conn, claims, user, domain_id)
    actor = str(user.id)
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO platform.domain_values (domain_id, code, label, sort_order, created_by, updated_by) "
                "VALUES (%(d)s, %(code)s, %(label)s, %(sort)s, %(actor)s, %(actor)s) "
                "RETURNING id::text AS id, code, label, active, sort_order",
                {"d": str(domain_id), "code": payload.code, "label": payload.label,
                 "sort": payload.sortOrder, "actor": actor},
            )
            row = await cur.fetchone()
        await conn.commit()
    except pg_errors.UniqueViolation:
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"code '{payload.code}' already exists in this domain")
    log.info("domain_value_mutation", action="create", domain_id=str(domain_id), code=payload.code, actor=actor)
    return _value_row(row)


@router.patch(
    "/custom-domains/{domain_id}/values/{value_id}",
    summary="Edit a value's label/active/order — code is immutable (owner or admin)",
)
async def update_value(
    domain_id: UUID,
    value_id: UUID,
    payload: ValueUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _assert_domain_editor(conn, claims, user, domain_id)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no fields to update")
    cols = {"label": "label", "active": "active", "sortOrder": "sort_order"}
    clauses = [f"{col} = %({attr})s" for attr, col in cols.items() if attr in fields]
    params: dict[str, Any] = {"id": str(value_id), "d": str(domain_id), "actor": str(user.id)}
    for attr in cols:
        if attr in fields:
            params[attr] = fields[attr]
    clauses += ["updated_by = %(actor)s", "updated_at = now()"]
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"UPDATE platform.domain_values SET {', '.join(clauses)} "
            f"WHERE id = %(id)s AND domain_id = %(d)s "
            "RETURNING id::text AS id, code, label, active, sort_order",
            params,
        )
        row = await cur.fetchone()
    if row is None:
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="value not found")
    await conn.commit()
    log.info("domain_value_mutation", action="update", domain_id=str(domain_id), value_id=str(value_id), actor=str(user.id))
    return _value_row(row)


@router.delete(
    "/custom-domains/{domain_id}/values/{value_id}", status_code=status.HTTP_204_NO_CONTENT,
    summary="Hard-delete a value — refused if a feature still uses it (owner or admin)",
)
async def delete_value(
    domain_id: UUID,
    value_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> Response:
    await _assert_domain_editor(conn, claims, user, domain_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT code FROM platform.domain_values WHERE id = %s AND domain_id = %s",
            (str(value_id), str(domain_id)),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="value not found")
    code = row[0]

    # Refuse if any feature on any field bound to this domain still carries the code (retire instead).
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT layer_id, field_key FROM platform.layer_fields WHERE domain_id = %s AND data_type = 'coded'",
            (str(domain_id),),
        )
        binds = await cur.fetchall()
    for layer_id, field_key in binds:
        if not _SLOT_RE.fullmatch(layer_id):
            continue  # only slot tables carry props/coded fields; never name a non-slot table
        async with conn.cursor() as cur:
            await cur.execute(
                sql.SQL("SELECT 1 FROM {tbl} WHERE props ->> %(fk)s = %(code)s LIMIT 1").format(
                    tbl=sql.Identifier("assets", layer_id)
                ),
                {"fk": field_key, "code": code},
            )
            if await cur.fetchone() is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"value '{code}' is in use by features; retire it (set active=false) instead of deleting",
                )

    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM platform.domain_values WHERE id = %s AND domain_id = %s",
                          (str(value_id), str(domain_id)))
    await conn.commit()
    log.info("domain_value_mutation", action="delete", domain_id=str(domain_id), code=code, actor=str(user.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
