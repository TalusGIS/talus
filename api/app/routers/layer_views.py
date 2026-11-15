"""Talus 38 — view-layer authoring API (the HTTP surface over app/layer_views.py).

A view layer is a sanitized lens over a source layer (filtered + column-limited), registered as
its own catalog item with its own OpenFGA `layer` object + its own sharing. This router is the
ONLY write surface that mints/edits/removes views; the SECURITY-CRITICAL DDL builder + store live
in `app/layer_views.py`. **Reads + sharing are untouched**: a view is a normal `platform.layers`
row, so it serves over the existing generic `/layers/{id}/features` + `/layers/{id}/export` (gated
on the VIEW's own object — sanitized for free) and shares via the existing T25 `/layers/{id}/shares`
dialog. There is deliberately no new read/share endpoint here.

**F-author gate:** authoring is privileged — you're deciding what leaves
the building. CREATE requires **talus-admin OR the source layer's owner** (a plain viewer of the
source may NOT re-expose it). EDIT/DELETE additionally allow the **view's own owner** (the author).
All gates fail closed (the OpenFGA `is_owner` check 503s on error, never allows).
"""
import re
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from pydantic import BaseModel, field_validator

from ..auth import _validated_claims, get_current_user
from ..authz import _has_admin_role, is_owner
from ..db import get_db, get_owner_db
from ..layer_views import (
    _UNJOINABLE_COLUMNS,
    drop_view,
    list_source_columns,
    register_view,
    update_view,
)
from ..models.user import User
from .layer_query import _columns, _props_fields, _require_registered

log = structlog.get_logger(__name__)

router = APIRouter(tags=["layer-views"])

_MAX_LABEL_LEN = 200


# --- request bodies (the same LABEL discipline publisher.LayerCreate uses) -------


class ViewFilter(BaseModel):
    """One `field:op:value` condition. Deep validation (field is a real source column; op is in the
    whitelist) happens in the DDL builder — the SAME validated grammar that filters the feature
    endpoint — so the router stays a thin pass-through and there's one source of validation truth."""

    field: str
    op: str
    value: str


def _validate_label(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("displayName must not be empty")
    if len(v) > _MAX_LABEL_LEN:
        raise ValueError(f"displayName must be at most {_MAX_LABEL_LEN} characters")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
        raise ValueError("displayName must not contain control characters")
    return v


def _validate_color(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if not re.match(r"^#[0-9a-fA-F]{6}$", v):
        raise ValueError("displayColor must be a 7-char hex color like '#1f77b4'")
    return v


class ViewJoin(BaseModel):
    """The V080 join recipe (decision-relates-and-join-views.md PR B). Thin pass-through like
    ViewFilter — ALL deep validation (alias/key grammar, membership in the live sides' column ∪
    field-key sets, the 63-char prefixed-name cap, sort direction) lives in the DDL builder
    (`layer_views._compile_join`), the one source of validation truth."""

    joinTableId: str
    layerKey: str
    tableKey: str
    alias: str
    columns: list[str]
    sortKey: str
    sortDir: str = "desc"

    def spec(self) -> dict[str, Any]:
        return {
            "layer_key": self.layerKey,
            "table_key": self.tableKey,
            "alias": self.alias,
            "columns": self.columns,
            "sort_key": self.sortKey,
            "sort_dir": self.sortDir,
        }


class ViewCreate(BaseModel):
    sourceLayerId: str
    displayName: str
    displayColor: str | None = None
    allowedColumns: list[str]
    filters: list[ViewFilter] = []
    join: ViewJoin | None = None

    @field_validator("displayName")
    @classmethod
    def _vn(cls, v: str) -> str:
        return _validate_label(v)

    @field_validator("displayColor")
    @classmethod
    def _vc(cls, v: str | None) -> str | None:
        return _validate_color(v)


class ViewUpdate(BaseModel):
    """`join` present → replace the join (Fork-6 gate re-checked against the new table);
    `clearJoin` → drop back to a plain view; NEITHER → the stored join is kept unchanged, so an
    older client editing columns/filters can never silently strip a join."""

    allowedColumns: list[str]
    filters: list[ViewFilter] = []
    join: ViewJoin | None = None
    clearJoin: bool = False


# --- F-author gates -------------------------------------------------------------


async def _assert_can_author_source(
    claims: dict[str, Any] | None, user: User, source_id: str
) -> None:
    """CREATE / introspect gate: talus-admin OR the source layer's owner. Fails closed."""
    if _has_admin_role(claims):
        return
    if await is_owner(user, source_id):
        return
    log.info(
        "authz_rejected", capability="layer-views:author", reason="not_admin_not_source_owner",
        user_id=str(user.id), source=source_id,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="not authorized to author a view of this layer",
    )


async def _assert_can_edit_view_definition(
    claims: dict[str, Any] | None, user: User, view_id: str, source_id: str
) -> None:
    """EDIT gate — the view's columns, filters and join: talus-admin OR the SOURCE layer's owner.
    The same rule as creating the view, and deliberately NOT the view's own owner:
    what a view exposes is a decision
    about the source's data. A view is created by the source's owner or an admin, but after either
    item is transferred the view's owner may be someone who cannot see the source — and this door
    replaces the column list and the filters, so admitting them let that person publish the whole
    source under the view's sharing. Fails closed."""
    if _has_admin_role(claims):
        return
    if await is_owner(user, source_id):
        return
    log.info(
        "authz_rejected", capability="layer-views:edit-definition", reason="not_admin_not_source_owner",
        user_id=str(user.id), view=view_id, source=source_id,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="only the source layer's owner or an administrator can change what this view exposes",
    )


async def _assert_can_modify_view(
    claims: dict[str, Any] | None, user: User, view_id: str, source_id: str
) -> None:
    """DELETE gate: admin OR the view's own owner OR the source layer's owner (it is their data the
    view exposes). Removing a view can only narrow what is exposed, so the view's owner keeps it;
    changing the definition is `_assert_can_edit_view_definition`. Fails closed."""
    if _has_admin_role(claims):
        return
    if await is_owner(user, view_id) or await is_owner(user, source_id):
        return
    log.info(
        "authz_rejected", capability="layer-views:modify", reason="not_admin_not_owner",
        user_id=str(user.id), view=view_id,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="not authorized to modify this view",
    )


async def _assert_can_author_join_table(
    claims: dict[str, Any] | None, user: User, table_id: str
) -> None:
    """Fork-6 gate, the table half: joining a table REPUBLISHES its columns under the view's own
    sharing, so the author must be talus-admin OR the TABLE's owner (mere can_view is not enough).
    Combined with `_assert_can_author_source` this yields admin OR owner-of-BOTH. Fails closed."""
    if _has_admin_role(claims):
        return
    if await is_owner(user, table_id):
        return
    log.info(
        "authz_rejected", capability="layer-views:join-author", reason="not_admin_not_table_owner",
        user_id=str(user.id), table=table_id,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="not authorized to join this table into a view (owner of both ends, or admin)",
    )


async def _view_source(conn: AsyncConnection, view_id: str) -> str:
    """The source layer id behind a view, or 404 if `view_id` isn't a view layer."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT source_layer_id FROM platform.layer_views WHERE view_layer_id = %s", (view_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="view not found")
    return row[0]


async def _view_stored_join(
    conn: AsyncConnection, view_id: str
) -> tuple[str | None, dict[str, Any] | None]:
    """The stored (join_table_id, join_spec) — the keep-unchanged baseline for ViewUpdate."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT join_table_id, join_spec FROM platform.layer_views WHERE view_layer_id = %s",
            (view_id,),
        )
        row = await cur.fetchone()
    return (row[0], row[1]) if row is not None else (None, None)


# --- endpoints ------------------------------------------------------------------


@router.get(
    "/layer-views/sources/{source_id}/columns",
    summary="A source layer's columns for the view allow-list picker (admin or source owner)",
)
async def source_columns(
    source_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require_registered(conn, source_id)
    await _assert_can_author_source(claims, user, source_id)
    return {"sourceLayerId": source_id, "columns": await list_source_columns(conn, source_id)}


@router.get(
    "/layer-views/join-tables/{table_id}/columns",
    summary="A candidate join table's columns + field keys for the join picker (admin or table owner)",
)
async def join_table_columns(
    table_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Everything a join may key/sort/expose on the TABLE side: real scalar columns (minus the
    unjoinable set) + ACTIVE owner-defined field keys, each with the type the joined output column
    will carry. Gated like authoring (Fork 6, table half) — the picker only opens for someone who
    could actually create the join."""
    await _require_registered(conn, table_id)
    async with conn.cursor() as cur:
        await cur.execute("SELECT geom_type FROM platform.layers WHERE layer_id = %s", (table_id,))
        row = await cur.fetchone()
    if row is None or row[0] != "none":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="join views join a standalone table (geom_type='none')",
        )
    await _assert_can_author_join_table(claims, user, table_id)
    cols = await _columns(conn, table_id)
    fields = await _props_fields(conn, table_id)
    return {
        "tableId": table_id,
        "columns": [
            {"name": n, "type": t} for n, t in cols.items() if n not in _UNJOINABLE_COLUMNS
        ],
        "fields": [
            {"key": k, "type": dt, "label": label} for k, (dt, _dom, label) in fields.items()
        ],
    }


@router.post(
    "/layer-views",
    status_code=status.HTTP_201_CREATED,
    summary="Author a sanitized view layer of a source (admin or source owner)",
)
async def create_view(
    payload: ViewCreate,
    conn: AsyncConnection = Depends(get_owner_db),  # S-1a: CREATE VIEW (runtime DDL) — owner pool
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _assert_can_author_source(claims, user, payload.sourceLayerId)
    if payload.join is not None:
        await _assert_can_author_join_table(claims, user, payload.join.joinTableId)
    try:
        return await register_view(
            conn,
            actor=user.id,
            source_id=payload.sourceLayerId,
            display_name=payload.displayName,
            display_color=payload.displayColor,
            allowed_columns=payload.allowedColumns,
            filters=[f.model_dump() for f in payload.filters],
            join_table_id=payload.join.joinTableId if payload.join else None,
            join_spec=payload.join.spec() if payload.join else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))


@router.put(
    "/layer-views/{view_id}",
    summary="Edit a view's columns/filter — DROP/CREATE the PG view (admin or the source layer's owner)",
)
async def edit_view(
    view_id: str,
    payload: ViewUpdate,
    conn: AsyncConnection = Depends(get_owner_db),  # S-1a: CREATE OR REPLACE VIEW (runtime DDL) — owner pool
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    source_id = await _view_source(conn, view_id)
    await _assert_can_edit_view_definition(claims, user, view_id, source_id)
    # Join semantics: clearJoin wins; an explicit join replaces (Fork-6 gate re-checked against
    # the NEW table); neither → the stored join carries through unchanged.
    if payload.clearJoin:
        join_table_id, join_spec = None, None
    elif payload.join is not None:
        await _assert_can_author_join_table(claims, user, payload.join.joinTableId)
        join_table_id, join_spec = payload.join.joinTableId, payload.join.spec()
    else:
        join_table_id, join_spec = await _view_stored_join(conn, view_id)
    try:
        return await update_view(
            conn,
            actor=user.id,
            view_id=view_id,
            allowed_columns=payload.allowedColumns,
            filters=[f.model_dump() for f in payload.filters],
            join_table_id=join_table_id,
            join_spec=join_spec,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))


@router.delete(
    "/layer-views/{view_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a view layer (admin, author, or source owner)",
)
async def delete_view(
    view_id: str,
    conn: AsyncConnection = Depends(get_owner_db),  # S-1a: DROP VIEW (runtime DDL) — owner pool
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> None:
    source_id = await _view_source(conn, view_id)
    await _assert_can_modify_view(claims, user, view_id, source_id)
    await drop_view(conn, actor=user.id, view_id=view_id)
