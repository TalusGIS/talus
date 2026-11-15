"""Table↔layer relates — the authoring + listing API over `platform.layer_relates` (V078).

PR A of decision-relates-and-join-views.md: a relate is pure NAVIGATION metadata connecting an
origin (spatial) layer to a standalone table (V059, geom_type='none') on a key pair — click a
sign, see its inspections. Related ROWS are never served here: the client reads them through the
table's existing gated `/layers/{table}/features` endpoint filtered on the key, so this router
adds NO new data path.

**The security crux (fork 3, fail-closed omission):** a relate must never widen access to a
private table. `GET /layers/{id}/relates` includes a relate ONLY when the caller can view the
related table — otherwise it is OMITTED entirely (no count, no name, no "+N hidden"), the same
§3.1 rule the where-used routes follow. Even a caller who somehow learns a relate exists gains
nothing: the rows live behind the table's own OpenFGA gate.

**Authoring gate:** talus-admin OR the origin layer's OpenFGA owner (`authz.manage_allowed`, the
same key that gates fields authoring), AND the author must be able to view the related table at
create time (404-shaped when they can't — a create must not confirm a hidden table exists).

**Fences (North Star, nonspatial arc):** documented-FK only — no PG constraint is created, no
cascade, no relationship classes, no editing through a relate. Cardinality is a display label.
Key pairs are IMMUTABLE after create (rekey = delete + re-add, mirroring layer_fields' immutable
field_key) so a relate can never silently re-point.
"""
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from psycopg import AsyncConnection, errors as pg_errors
from psycopg.rows import dict_row
from pydantic import BaseModel, field_validator

from ..auth import _validated_claims, get_current_user
from ..authz import _openfga_list_objects, manage_allowed, view_allowed
from ..db import get_db
from ..models.user import User
from .layer_fields import _FIELD_KEY_RE, _validate_label
from .layer_query import _columns

log = structlog.get_logger(__name__)

router = APIRouter(tags=["layer-relates"])

_CARDINALITIES = frozenset({"1:1", "1:M"})
# Real columns that can't meaningfully key a relate: the geometry, the whole props blob, and
# bookkeeping columns. Native scalar columns (id, notes, a built-in's typed columns) stay legal.
_UNRELATABLE_COLUMNS = frozenset({"geom", "props", "row_version", "ai_provenance"})


# --- request bodies -------------------------------------------------------------


class RelateCreate(BaseModel):
    relatedTableId: str
    originFieldKey: str
    relatedFieldKey: str
    displayName: str
    cardinality: str = "1:M"
    sortOrder: int = 0

    @field_validator("originFieldKey", "relatedFieldKey")
    @classmethod
    def _vk(cls, v: str) -> str:
        if not _FIELD_KEY_RE.fullmatch(v):
            raise ValueError("field keys must be snake_case: a letter then letters/digits/_ (≤63 chars)")
        return v

    @field_validator("displayName")
    @classmethod
    def _vl(cls, v: str) -> str:
        return _validate_label(v)

    @field_validator("cardinality")
    @classmethod
    def _vc(cls, v: str) -> str:
        if v not in _CARDINALITIES:
            raise ValueError(f"cardinality must be one of {sorted(_CARDINALITIES)}")
        return v


class RelateUpdate(BaseModel):
    """Display metadata only — the ends and key pair are IMMUTABLE (delete + re-add to rekey)."""
    displayName: str | None = None
    cardinality: str | None = None
    sortOrder: int | None = None

    @field_validator("displayName")
    @classmethod
    def _vl(cls, v: str | None) -> str | None:
        return _validate_label(v) if v is not None else v

    @field_validator("cardinality")
    @classmethod
    def _vc(cls, v: str | None) -> str | None:
        if v is not None and v not in _CARDINALITIES:
            raise ValueError(f"cardinality must be one of {sorted(_CARDINALITIES)}")
        return v


# --- guards + validation helpers -------------------------------------------------


async def _layer_row(conn: AsyncConnection, layer_id: str) -> tuple[str, str | None, str] | None:
    """(display_name, geom_type, status) or None. layer_id binds as a parameter — safe for any id."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(display_name, layer_id), geom_type, status "
            "FROM platform.layers WHERE layer_id = %s",
            (layer_id,),
        )
        row = await cur.fetchone()
    return (row[0], row[1], row[2]) if row is not None else None


_SPATIAL_GEOMS = frozenset({"point", "line", "polygon"})


async def _require_origin(conn: AsyncConnection, layer_id: str) -> None:
    """The origin must be a registered, ready, SPATIAL layer (v1 direction: layer→table). The
    check is a positive geom_type membership, not `!= 'none'` — view layers register with a NULL
    geom_type and must not slip in as origins (the one-way fence). 404 unknown; 409 with reason."""
    row = await _layer_row(conn, layer_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    _name, geom_type, l_status = row
    if l_status != "ready":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="layer not ready")
    if geom_type not in _SPATIAL_GEOMS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="relates originate from a spatial layer (v1 is one-way, layer → table)",
        )


async def _assert_can_manage(claims: dict | None, user: User, layer_id: str) -> None:
    """Authoring gate: talus-admin OR the origin layer's OpenFGA owner. Fails closed."""
    if not await manage_allowed(claims, user, layer_id):
        log.info(
            "authz_rejected", capability=f"{layer_id}:relates-author",
            reason="not_admin_not_owner", user_id=str(user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"not authorized to manage relates on {layer_id}",
        )


async def _assert_can_read(claims: dict | None, user: User, layer_id: str) -> None:
    """Read gate: anyone who can view the origin layer (to render the drawer) OR admin/owner."""
    if await view_allowed(user, layer_id) or await manage_allowed(claims, user, layer_id):
        return
    log.info(
        "authz_rejected", capability=f"{layer_id}:relates-read",
        reason="no_view_access", user_id=str(user.id),
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"not authorized to view {layer_id}",
    )


async def _relatable_keys(conn: AsyncConnection, layer_id: str) -> set[str]:
    """The keys a relate may bind on one end: the item's real scalar columns (minus geometry/blob/
    bookkeeping) ∪ its ACTIVE owner-defined field keys. Both sources are server-derived — the
    requested key is checked for MEMBERSHIP here and never reaches SQL as an identifier (related
    rows are read via the existing bound-param filter grammar)."""
    cols = await _columns(conn, layer_id)
    keys = {c for c in cols if c not in _UNRELATABLE_COLUMNS}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT field_key FROM platform.layer_fields WHERE layer_id = %s AND active",
            (layer_id,),
        )
        keys.update(r[0] for r in await cur.fetchall())
    return keys


_SELECT = (
    "SELECT r.id::text AS id, r.origin_layer_id, r.origin_field_key, r.related_table_id, "
    "r.related_field_key, r.display_name, r.cardinality, r.sort_order, "
    "COALESCE(l.display_name, r.related_table_id) AS related_table_name, "
    "r.created_at, r.updated_at "
    "FROM platform.layer_relates r "
    "JOIN platform.layers l ON l.layer_id = r.related_table_id"
)


async def _fetch_relate(conn: AsyncConnection, relate_id: str) -> dict:
    """Re-read one relate in the standard (joined) response shape after a write, same transaction."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"{_SELECT} WHERE r.id = %s", (relate_id,))
        return await cur.fetchone()


def _to_camel(row: dict) -> dict:
    return {
        "id": row["id"],
        "originLayerId": row["origin_layer_id"],
        "originFieldKey": row["origin_field_key"],
        "relatedTableId": row["related_table_id"],
        "relatedTableName": row["related_table_name"],
        "relatedFieldKey": row["related_field_key"],
        "displayName": row["display_name"],
        "cardinality": row["cardinality"],
        "sortOrder": row["sort_order"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


# --- routes -----------------------------------------------------------------------


@router.get(
    "/layers/{layer_id}/relates",
    summary="List a layer's relates — fail-closed: relates to tables the caller can't view are omitted",
)
async def list_relates(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    if await _layer_row(conn, layer_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    await _assert_can_read(claims, user, layer_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"{_SELECT} WHERE r.origin_layer_id = %s ORDER BY r.sort_order, r.display_name",
            (layer_id,),
        )
        rows = await cur.fetchall()
    # THE crux (fork 3): omit every relate whose table the caller can't view — no count, no name.
    # One list-objects call (the where-used _visible_layers pattern), skipped for admins/empty.
    if rows and not user.is_admin:
        viewable = set(await _openfga_list_objects(user.id, "can_view", "layer"))
        rows = [r for r in rows if r["related_table_id"] in viewable]
    return {"layerId": layer_id, "relates": [_to_camel(r) for r in rows]}


@router.post(
    "/layers/{layer_id}/relates",
    status_code=status.HTTP_201_CREATED,
    summary="Author a relate from this layer to a table (admin or layer owner)",
)
async def create_relate(
    layer_id: str,
    payload: RelateCreate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require_origin(conn, layer_id)
    await _assert_can_manage(claims, user, layer_id)

    # The related end must be a registered, ready TABLE the author can see. An invisible table
    # 404s exactly like a nonexistent one — a create attempt must not confirm hidden items exist.
    target = await _layer_row(conn, payload.relatedTableId)
    if target is None or not (
        user.is_admin or await view_allowed(user, payload.relatedTableId)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="table not found")
    _t_name, t_geom, t_status = target
    if t_geom != "none":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="the related end of a relate must be a standalone table",
        )
    if t_status != "ready":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="table not ready")

    # Both keys must actually exist on their side (real scalar column or active field key) —
    # fail-closed 422, never a silently-dead relate.
    origin_keys = await _relatable_keys(conn, layer_id)
    if payload.originFieldKey not in origin_keys:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{payload.originFieldKey}' is not a field on this layer",
        )
    related_keys = await _relatable_keys(conn, payload.relatedTableId)
    if payload.relatedFieldKey not in related_keys:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{payload.relatedFieldKey}' is not a field on that table",
        )

    actor = str(user.id)
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO platform.layer_relates "
                "(origin_layer_id, origin_field_key, related_table_id, related_field_key, "
                " display_name, cardinality, sort_order, created_by, updated_by) "
                "VALUES (%(origin)s, %(okey)s, %(table)s, %(rkey)s, %(name)s, %(card)s, "
                "        %(sort)s, %(actor)s, %(actor)s) "
                "RETURNING id::text",
                {
                    "origin": layer_id, "okey": payload.originFieldKey,
                    "table": payload.relatedTableId, "rkey": payload.relatedFieldKey,
                    "name": payload.displayName, "card": payload.cardinality,
                    "sort": payload.sortOrder, "actor": actor,
                },
            )
            new_id = (await cur.fetchone())[0]
        row = await _fetch_relate(conn, new_id)
        await conn.commit()
    except pg_errors.UniqueViolation:
        await conn.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a relate on this key pair to that table already exists",
        )
    log.info("layer_relate_mutation", action="create", origin=layer_id,
             table=payload.relatedTableId, actor=actor)
    return _to_camel(row)


@router.patch(
    "/layers/{layer_id}/relates/{relate_id}",
    summary="Edit a relate's display name/cardinality/order (admin or layer owner)",
)
async def update_relate(
    layer_id: str,
    relate_id: UUID,
    payload: RelateUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    if await _layer_row(conn, layer_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    await _assert_can_manage(claims, user, layer_id)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no fields to update")
    # Every column here is NOT NULL — an explicit null is a client bug; 422 it, don't 500 on the DB.
    if any(v is None for v in fields.values()):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="fields cannot be null")

    sets = {"displayName": "display_name", "cardinality": "cardinality", "sortOrder": "sort_order"}
    set_clauses = [f"{sets[k]} = %({k})s" for k in fields]
    set_clauses += ["updated_by = %(actor)s", "updated_at = now()"]
    params: dict[str, Any] = {**fields, "id": str(relate_id), "origin": layer_id, "actor": str(user.id)}

    async with conn.cursor() as cur:
        await cur.execute(
            f"UPDATE platform.layer_relates SET {', '.join(set_clauses)} "
            "WHERE id = %(id)s AND origin_layer_id = %(origin)s RETURNING id::text",
            params,
        )
        hit = await cur.fetchone()
    if hit is None:
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="relate not found")
    row = await _fetch_relate(conn, hit[0])
    await conn.commit()
    log.info("layer_relate_mutation", action="update", origin=layer_id,
             relate_id=str(relate_id), actor=str(user.id))
    return _to_camel(row)


@router.delete(
    "/layers/{layer_id}/relates/{relate_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a relate (admin or layer owner) — navigation metadata only, no data is touched",
)
async def delete_relate(
    layer_id: str,
    relate_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> Response:
    if await _layer_row(conn, layer_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    await _assert_can_manage(claims, user, layer_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM platform.layer_relates WHERE id = %s AND origin_layer_id = %s",
            (str(relate_id), layer_id),
        )
        deleted = cur.rowcount
    if not deleted:
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="relate not found")
    await conn.commit()
    log.info("layer_relate_mutation", action="delete", origin=layer_id,
             relate_id=str(relate_id), actor=str(user.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
