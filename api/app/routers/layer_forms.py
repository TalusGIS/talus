"""The layer form document doors (decision-talus-field-f2.md §4.2) — the `renderer` precedent, verbatim:
per-layer config on platform.layers, manage-gated write (owner or admin), view-gated read, audit
context set, updated_at bumped. The document is validated key-by-key (app/form_config.py) before it
is stored; a bad document is a 400 naming every problem.

Read surface: `GET /layers/{id}/form` answers for anyone who can VIEW the layer (the desk form, the
field app) — form config is layer metadata like the renderer. It is ALSO carried on `/me/layers` as
`formConfig`, so the reads the clients already make bring it along; this door exists for the
authoring tab's refresh and for scripts.
"""
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from ..auth import _validated_claims, get_current_user
from ..authz import view_allowed
from ..db import get_db
from ..form_config import FormConfigError, LayerFieldFacts, validate_form_config
from ..models.user import User
from .symbology import _require_manage, _require_registered, _set_audit_context

log = structlog.get_logger(__name__)

router = APIRouter(tags=["layer-form"])


async def _field_facts(conn: AsyncConnection, layer_id: str) -> dict[str, LayerFieldFacts]:
    """The layer's ACTIVE fields with type, constraints and — for coded fields — the bound domain's
    codes (active AND retired: a retired code in a rule still names a real stored value)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT f.field_key, f.data_type, f.constraints, f.domain_id, f.required, "
            "       (SELECT array_agg(dv.code) FROM platform.domain_values dv WHERE dv.domain_id = f.domain_id) "
            "FROM platform.layer_fields f WHERE f.layer_id = %s AND f.active",
            (layer_id,),
        )
        rows = await cur.fetchall()
    out: dict[str, LayerFieldFacts] = {}
    for key, dtype, constraints, domain_id, required, codes in rows:
        out[key] = LayerFieldFacts(
            data_type=dtype,
            constraints=constraints,
            domain_codes=set(codes or []) if domain_id else None,
            required=bool(required),
        )
    return out


async def _require_view(user: User, layer_id: str) -> None:
    if not await view_allowed(user, layer_id):
        log.info("authz_rejected", capability=f"{layer_id}:form:read", reason="no_view", user_id=str(user.id))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"not authorized to read {layer_id}")


@router.get("/layers/{layer_id}/form", summary="A layer's form document (view)")
async def get_layer_form(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require_registered(conn, layer_id)
    await _require_view(user, layer_id)
    async with conn.cursor() as cur:
        await cur.execute("SELECT form_config FROM platform.layers WHERE layer_id = %s", (layer_id,))
        row = await cur.fetchone()
    return {"layer_id": layer_id, "formConfig": row[0] if row else None}


@router.put("/layers/{layer_id}/form", summary="Set a layer's form document (owner or admin)")
async def set_layer_form(
    layer_id: str,
    payload: dict[str, Any],
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require_registered(conn, layer_id)
    auth_path = await _require_manage(claims, user, layer_id)
    facts = await _field_facts(conn, layer_id)
    try:
        cfg = validate_form_config(payload, facts)
    except FormConfigError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.errors)
    stored = cfg.model_dump(by_alias=True, exclude_none=True)
    async with conn.cursor() as cur:
        await _set_audit_context(cur, user.id)
        await cur.execute(
            "UPDATE platform.layers SET form_config = %(c)s, updated_at = now() WHERE layer_id = %(lid)s",
            {"c": Jsonb(stored), "lid": layer_id},
        )
    await conn.commit()
    log.info("layer_form_set", layer_id=layer_id, actor=str(user.id), auth_path=auth_path,
             groups=len(cfg.groups), rules=len(cfg.fields), templates=len(cfg.templates))
    return {"layer_id": layer_id, "formConfig": stored}


@router.delete("/layers/{layer_id}/form", status_code=status.HTTP_204_NO_CONTENT,
               summary="Clear a layer's form document back to the default form (owner or admin)")
async def clear_layer_form(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> Response:
    await _require_registered(conn, layer_id)
    auth_path = await _require_manage(claims, user, layer_id)
    async with conn.cursor() as cur:
        await _set_audit_context(cur, user.id)
        await cur.execute(
            "UPDATE platform.layers SET form_config = NULL, updated_at = now() WHERE layer_id = %(lid)s",
            {"lid": layer_id},
        )
    await conn.commit()
    log.info("layer_form_cleared", layer_id=layer_id, actor=str(user.id), auth_path=auth_path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
