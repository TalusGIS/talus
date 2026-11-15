"""Field projects — .qgz intake, storage & the Import-URL door (V085, decision-field-projects.md).

A Field project is an uploaded QGIS/QField project stored as an OPAQUE artifact (THE FENCE:
Talus never opens or edits it — validation is the zip lip + a read-only catalog sniff, both in
app.field_projects) and served to QField's Import URL through a credentialed byte door.

Two trust postures in one router:

- MANAGE (Bearer only) — create is publisher/admin (the item-mint gate); replace / rename /
  delete / share / delete-protection are owner-or-admin (the rasters _load_for_manage shape).
  Create is OpenFGA-FIRST (uuid -> owner tuple -> store bytes -> row), full rollback on failure.
- SERVE (`GET /field-projects/{id}/archive`) — Bearer OR PAT Basic in the URL userinfo (the
  bench-proven `https://talus:<PAT>@host/...` crew shape). Anon -> 401 + Basic challenge on
  EVERY id (no existence signal); Bearer without view -> 403; PAT without view -> the 401
  NO_VIEW invite ("use another token" re-prompts the field client); revoked/expired PAT dies
  in auth._pat_user (401). The artifact may embed per-crew credentials, so can_view on the
  field_project is exactly "who may fetch these bytes" — no public/anon tier exists in v1 and
  the shares endpoint refuses publicAccess outright (F3). Conditional GET: strong ETag from
  content_sha256 + Last-Modified from updated_at; If-None-Match (then If-Modified-Since) -> 304
  — QField sends conditional GETs on re-import (recon-proven 2026-08-12). inv21 gates all legs.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import timezone
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from .. import field_projects as fp
from ..auth import (
    BASIC_CHALLENGE,
    BASIC_CHALLENGE_NO_VIEW,
    _validated_claims,
    get_current_user,
    get_optional_user,
)
from ..authz import (
    _has_admin_role,
    _has_publish_role,
    _openfga_check,
    _openfga_delete,
    _openfga_list_objects,
    _openfga_read_object_tuples,
    _openfga_read_user_groups,
    _openfga_write,
    get_object_shares,
    set_object_public,
    set_object_shares,
)
from ..config import settings
from ..db import get_db
from ..field_eligibility import (
    WARN_FILTERS, WARN_SKIPPED, WARN_TOKEN_DENIED, WARN_VIEW, collectable_layers,
)
from ..media import (
    MediaObjectMissing,
    MediaStoreUnavailable,
    iter_object,
    remove_object,
    write_object_from_file,
)
from ..models.user import User
from ..pat_service import TokenCapReached, mint_pat, revoke_pat
from ..qfield import GenerationError, LayerSpec, ProjectSpec, compile_project
from ..ratelimit import forget_validated_pat
from ..secrets import (
    SecretDecryptError,
    SecretKeyUnavailable,
    decrypt_secret,
    encrypt_secret,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["field-projects"])

_MEDIA_TYPE = "application/octet-stream"
_MAX_TITLE_LEN = 80
_MAX_DESCRIPTION_LEN = 2000


# ---------------------------------------------------------------------------
# Shared helpers (the rasters shapes, adapted)
# ---------------------------------------------------------------------------

def _clean_title(v: str) -> str:
    v = (v or "").strip()
    if not v:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="title must not be empty")
    if len(v) > _MAX_TITLE_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"title must be at most {_MAX_TITLE_LEN} characters",
        )
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="title must not contain control characters"
        )
    return v


def _clean_description(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if len(v) > _MAX_DESCRIPTION_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"description must be at most {_MAX_DESCRIPTION_LEN} characters",
        )
    return v or None


def _as_uuid(project_id: str) -> str:
    """Reject a non-UUID id with 404 (existence-hiding) before it reaches a uuid column or OpenFGA."""
    try:
        UUID(project_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="field project not found")
    return project_id


async def _require_publisher(
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> User:
    """403 unless the caller holds talus-publisher or talus-admin — minting an item, the same
    gate as create_raster / create_web_map."""
    if not _has_publish_role(claims):
        log.info(
            "authz_rejected", capability="field_projects:create",
            reason="missing_publish_role", user_id=str(user.id),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to create field projects")
    return user


async def _spool_body(request: Request) -> tuple[str, int]:
    """Stream the request body to a temp file under the F5 cap. Returns (path, size).
    413 over cap; 400 on an empty body; the temp file is unlinked on any failure."""
    fd, path = tempfile.mkstemp(suffix=".qgz")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > settings.field_project_max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=(
                            f"project exceeds the {settings.field_project_max_bytes // (1024 * 1024)} MB "
                            "upload limit"
                        ),
                    )
                out.write(chunk)
    except BaseException:
        _silent_unlink(path)
        raise
    if size == 0:
        _silent_unlink(path)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty request body — POST the .qgz bytes as the request body",
        )
    return path, size


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


async def _advertised_host(conn: AsyncConnection) -> str | None:
    """Host of the org's advertised base URL — feeds the foreign_datasources sniff warn. The
    server never guesses its public hostname (the multi-homed-rig lesson); unset -> no warn."""
    from ..org_settings import read_org_settings  # lazy: keep org_settings a leaf (no import cycle)
    try:
        adv = (await read_org_settings(conn)).advertisedBaseUrl
    except Exception:
        return None
    if not adv:
        return None
    return urlparse(adv).hostname


async def _validate_upload(conn: AsyncConnection, path: str) -> tuple[fp.QgzInfo, str]:
    """The F5 lip on a spooled upload: hard floor (400) + warnings + content sha256."""
    host = await _advertised_host(conn)
    try:
        info = await run_in_threadpool(fp.inspect_qgz, path, host)
    except fp.NotAFieldProject as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    sha = await run_in_threadpool(fp.sha256_file, path)
    return info, sha


async def _load_for_manage(
    conn: AsyncConnection, claims: dict[str, Any] | None, user: User, project_id: str
) -> str:
    """Authorize manage (replace/rename/share/delete): 404 if absent, else owner-OR-admin (403).
    Admin is role-only, checked FIRST so it short-circuits the OpenFGA call (rasters/web_maps)."""
    project_id = _as_uuid(project_id)
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.field_projects WHERE id = %s", (project_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="field project not found")
    if _has_admin_role(claims):
        return project_id
    if await _openfga_check(user.id, "owner", project_id, object_type="field_project"):
        return project_id
    log.info("authz_rejected", capability="field_projects:manage", reason="not_owner_not_admin",
             user_id=str(user.id), project=project_id)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to modify this field project")


async def _scope_project_token(cur, pat_id, specs) -> None:
    """The project's embedded token may WRITE only to the project's own layers (once-over 2026-11-15
    K6). Without a list, the token, which anyone the project is shared with can read out of the
    .qgz, edited and deleted on every layer its publisher may edit, recorded as the publisher's.
    The list is the same column a Talus Field device token carries (V108); the Esri door tells the
    two apart by `field_projects.embedded_pat_id` (geoservices `_edit_deny_reason`). Reads are
    unchanged: the token still reads what its owner may view, as the decision document says."""
    await cur.execute(
        "UPDATE platform.personal_access_tokens SET layer_ids = %s WHERE id = %s",
        ([s.layer_id for s in specs], pat_id),
    )


def _new_object_key(project_id: str) -> str:
    """A fresh key for each version of a project's file (once-over 2026-11-15 R2). The backup's media copy
    is append-only and skips a key it has archived, so a file replaced IN PLACE under one fixed key was
    never archived again: a restore brought back the first version ever stored while the restored row
    described the newer one. Each version now has its own key, recorded in the row's `object_key`."""
    return f"field-projects/{project_id}/project-{uuid4().hex[:12]}.qgz"


async def _remove_replaced(old_key: str | None, new_key: str, project_id: str) -> None:
    """After the row points at the new version, the live store drops the old one (the archive keeps
    every version). Best-effort: a failure leaves harmless cruft, never a row without bytes."""
    if not old_key or old_key == new_key:
        return
    try:
        await run_in_threadpool(remove_object, old_key)
    except Exception:
        log.warning("field_project_replaced_object_purge_failed", project=project_id, key=old_key)


async def _current_object_key(conn: AsyncConnection, project_id: str) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT object_key FROM platform.field_projects WHERE id = %s", (project_id,))
        row = await cur.fetchone()
    return row[0] if row else None


def _object_key(project_id: str) -> str:
    return f"field-projects/{project_id}/project.qgz"


def _detail_dict(row: tuple) -> dict[str, Any]:
    return {
        "id": str(row[0]), "title": row[1], "description": row[2],
        "sizeBytes": row[3], "contentSha256": row[4],
        "qgsTitle": row[5], "layerCount": row[6], "warnings": row[7],
        "createdAt": row[8], "updatedAt": row[9],
        "ownerId": str(row[10]) if row[10] else None,
        "deleteProtected": row[11],
        "sourceWebMapId": str(row[12]) if row[12] else None,
    }


_DETAIL_COLS = (
    "id, title, description, size_bytes, content_sha256, qgs_title, layer_count, warnings, "
    "created_at, updated_at, owner_id, delete_protected, source_web_map_id"
)


# ---------------------------------------------------------------------------
# Create / replace / rename / delete (Bearer, manage side)
# ---------------------------------------------------------------------------

@router.post("/field-projects", status_code=status.HTTP_201_CREATED,
             summary="Upload a .qgz field project (publisher only)")
async def create_field_project(
    request: Request,
    title: str,
    description: str | None = None,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(_require_publisher),
) -> dict[str, Any]:
    """Ingest: stream body -> zip lip + sniff -> owner tuple -> store -> row. The .qgz bytes are
    the request body; `title`/`description` are query params (python-multipart-free, the
    rasters-ingest pattern). A rejected upload never touches the store/FGA/DB."""
    title = _clean_title(title)
    description = _clean_description(description)
    src_path, size = await _spool_body(request)
    try:
        info, sha = await _validate_upload(conn, src_path)

        project_id = uuid4()
        object_key = _new_object_key(str(project_id))

        # OpenFGA FIRST — the owner grant. A failure raises 503 and NOTHING is stored.
        await _openfga_write([(f"user:{user.id}", "owner", f"field_project:{project_id}")])
        try:
            await run_in_threadpool(write_object_from_file, object_key, src_path, _MEDIA_TYPE)
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO platform.field_projects "
                    "(id, title, description, bucket, object_key, size_bytes, content_sha256, "
                    " qgs_title, layer_count, warnings, created_by, owner_id, updated_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING created_at, updated_at",
                    (str(project_id), title, description, settings.media_bucket, object_key,
                     size, sha, info.qgs_title, info.layer_count, Jsonb(info.warnings),
                     user.id, user.id, user.id),
                )
                ts = await cur.fetchone()
            await conn.commit()
        except Exception:
            # Roll back durable state so no half-item survives: object + owner tuple (best-effort).
            await conn.rollback()
            try:
                await run_in_threadpool(remove_object, object_key)
            except Exception:
                log.warning("field_project_cleanup_object_failed", project=str(project_id))
            try:
                await _openfga_delete(await _openfga_read_object_tuples(f"field_project:{project_id}"))
            except Exception:
                log.warning("field_project_cleanup_tuple_failed", project=str(project_id))
            raise
    finally:
        _silent_unlink(src_path)

    log.info("field_project_created", project=str(project_id), actor=str(user.id),
             bytes=size, layer_count=info.layer_count, warnings=info.warnings)
    return {
        "id": str(project_id), "title": title, "description": description,
        "sizeBytes": size, "contentSha256": sha,
        "qgsTitle": info.qgs_title, "layerCount": info.layer_count, "warnings": info.warnings,
        "createdAt": ts[0], "updatedAt": ts[1],
        "ownerId": str(user.id), "deleteProtected": False,
    }


@router.put("/field-projects/{project_id}/archive",
            summary="Replace the artifact in place (owner or admin)")
async def replace_field_project(
    request: Request,
    project_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Replace-in-place (F4): validate fully, THEN overwrite the one object key — a rejected
    replace never touches the served artifact. Row update rides uncommitted ahead of the object
    write (the attachments store() ordering) so a store failure rolls back to the old state;
    updated_at/content_sha256 roll together, so the next crew fetch misses its 304 and pulls
    the new bytes."""
    project_id = await _load_for_manage(conn, claims, user, project_id)
    src_path, size = await _spool_body(request)
    try:
        info, sha = await _validate_upload(conn, src_path)
        old_key = await _current_object_key(conn, project_id)
        object_key = _new_object_key(project_id)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE platform.field_projects SET size_bytes = %s, content_sha256 = %s, "
                "qgs_title = %s, layer_count = %s, warnings = %s, updated_by = %s, "
                "object_key = %s, updated_at = now() WHERE id = %s "
                f"RETURNING {_DETAIL_COLS}",
                (size, sha, info.qgs_title, info.layer_count, Jsonb(info.warnings),
                 user.id, object_key, project_id),
            )
            row = await cur.fetchone()
        try:
            await run_in_threadpool(write_object_from_file, object_key, src_path, _MEDIA_TYPE)
        except Exception:
            await conn.rollback()
            try:
                await run_in_threadpool(remove_object, object_key)
            except Exception:
                pass
            raise
        await conn.commit()
        await _remove_replaced(old_key, object_key, project_id)
    finally:
        _silent_unlink(src_path)
    log.info("field_project_replaced", project=project_id, actor=str(user.id),
             bytes=size, warnings=info.warnings)
    return _detail_dict(row)


class FieldProjectPatch(BaseModel):
    title: str | None = None
    description: str | None = None


@router.patch("/field-projects/{project_id}", summary="Rename / re-describe (owner or admin)")
async def patch_field_project(
    project_id: str,
    payload: FieldProjectPatch,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    project_id = await _load_for_manage(conn, claims, user, project_id)
    sets: list[str] = []
    args: list[Any] = []
    if payload.title is not None:
        sets.append("title = %s")
        args.append(_clean_title(payload.title))
    if payload.description is not None:
        sets.append("description = %s")
        args.append(_clean_description(payload.description))
    if not sets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="nothing to update")
    sets.append("updated_by = %s")
    args.append(user.id)
    # Metadata edits deliberately do NOT bump updated_at: it is the artifact's Last-Modified —
    # a rename must not make every crew's next conditional GET miss its 304 and re-pull bytes.
    async with conn.cursor() as cur:
        await cur.execute(
            f"UPDATE platform.field_projects SET {', '.join(sets)} WHERE id = %s "
            f"RETURNING {_DETAIL_COLS}",
            (*args, project_id),
        )
        row = await cur.fetchone()
    await conn.commit()
    log.info("field_project_patched", project=project_id, actor=str(user.id))
    return _detail_dict(row)


@router.delete("/field-projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete a field project (owner or admin)")
async def delete_field_project(
    project_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> None:
    project_id = await _load_for_manage(conn, claims, user, project_id)
    # The T24 padlock — ABSOLUTE, blocks owner AND admin (the deliberate two-step, V050 tone).
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT delete_protected, embedded_pat_id, object_key FROM platform.field_projects WHERE id = %s",
            (project_id,),
        )
        row = await cur.fetchone()
    if row and row[0]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="field project is delete-protected — turn off delete protection before deleting",
        )
    embedded_pat_id = row[1] if row else None
    stored_key = (row[2] if row else None) or _object_key(project_id)
    # OpenFGA first, then row + favorites (+ the embedded-PAT auto-revoke, transactional with
    # the row — no orphaned live credentials, decision-generate-field-project.md §F1), then
    # purge the object: the item leaves the catalog before its bytes go, so a purge failure
    # leaves harmless cruft, never a row without bytes.
    tuples = await _openfga_read_object_tuples(f"field_project:{project_id}")
    await _openfga_delete(tuples)
    revoked = None
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM platform.field_projects WHERE id = %s", (project_id,))
        await cur.execute(
            "DELETE FROM platform.user_favorites WHERE item_type = 'field_project' AND item_id = %s",
            (project_id,),
        )
        if embedded_pat_id is not None:
            revoked = await revoke_pat(cur, embedded_pat_id)
    await conn.commit()
    if revoked is not None:
        forget_validated_pat(revoked[1])
        log.info("pat_auto_revoked", token_id=revoked[0], project=project_id, actor=str(user.id))
    try:
        await run_in_threadpool(remove_object, stored_key)
    except Exception:
        log.warning("field_project_delete_purge_failed", project=project_id)
    log.info("field_project_deleted", project=project_id, actor=str(user.id), tuples_deleted=len(tuples))


# ---------------------------------------------------------------------------
# Generation — "Make field project" on a web map (decision-generate-field-project.md)
#
# The server compiles the web map into a .qgz (app.qfield — EMIT-only, THE FENCE), mints a
# dedicated per-project PAT (never the caller's token), self-checks its own output through
# the same intake lip every upload passes (the DOCTYPE lesson), and stores the result as an
# ordinary field-project row distinguished only by source_web_map_id / embedded_pat_id.
# ---------------------------------------------------------------------------

# Generation warning codes (F2/F3/F4 + compile-time codes from app.qfield) — same jsonb
# surface, same UI rendering as the upload-lip codes in app.field_projects.
# Lifted to app/field_eligibility.py (decision-talus-field-f1.md §4.3.3) — the generator and
# the Talus+ field module share ONE definition of "collectable"; these names stay as aliases so
# the response contract and every caller below are unchanged.
_GEN_WARN_SKIPPED = WARN_SKIPPED
_GEN_WARN_VIEW = WARN_VIEW
_GEN_WARN_FILTERS = WARN_FILTERS
_GEN_WARN_FLATTENED = "renderer_flattened"
_GEN_WARN_ICON_DROPPED = "point_icon_not_carried"
_GEN_WARN_SCOPE = "token_scope_read_only"
_GEN_WARN_TOKEN_DENIED = WARN_TOKEN_DENIED

_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


class GenerateFieldProjectBody(BaseModel):
    title: str | None = None        # default: the web map's title
    description: str | None = None


async def _advertised_base(conn: AsyncConnection) -> str | None:
    """The org's advertised base URL — the ONLY origin the generator will bake into
    datasources (the multi-homed-rig lesson: the server never guesses its public hostname).
    Unset -> the caller refuses with an actionable 409."""
    from ..org_settings import read_org_settings  # lazy: keep org_settings a leaf (no import cycle)
    try:
        adv = (await read_org_settings(conn)).advertisedBaseUrl
    except Exception:
        return None
    return adv.rstrip("/") if adv else None


async def _load_map_for_view(
    conn: AsyncConnection, user: User, map_id: str
) -> tuple[str, str, dict[str, Any]]:
    """Load (id, title, definition) with the get_web_map view gate: 404 on absent/malformed
    id, admin short-circuit, else can_view on the web_map object (403)."""
    try:
        UUID(map_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="web map not found")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, title, definition FROM platform.web_maps WHERE id = %s", (map_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="web map not found")
    if not user.is_admin and not await _openfga_check(
        user.id, "can_view", map_id, object_type="web_map"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to view this web map"
        )
    return str(row[0]), row[1], row[2] or {}


def _hex_or_default(value: Any, fallback: str = "#2563eb") -> str:
    return value if isinstance(value, str) and _HEX_COLOR_RE.fullmatch(value) else fallback


def _resolve_renderer(display_color: Any, renderer: Any) -> tuple[str, dict[str, Any], bool, bool]:
    """platform.layers.renderer (simple | uniqueValue, symbology.py vocabulary) -> the
    generator's single color + layer-wide style extras. uniqueValue flattens to its default
    (or first stop) color — F3 as endorsed; flattened=True adds the warn.

    A point ICON is likewise flattened away (decision-point-icons.md fork E): carrying it would
    mean emitting a QGIS SvgMarker, shipping the SVGs inside the project package, and getting
    QGIS's SVG path resolution right on a phone — its own slice. Colour and size still travel, so
    the layer stays recognisable in the field; icon_dropped=True raises the warn."""
    r = renderer if isinstance(renderer, dict) else {}
    style: dict[str, Any] = {}
    for src, dst in (("lineWidth", "line_width"), ("lineDash", "line_dash"),
                     ("fillOpacity", "fill_opacity"), ("outlineWidth", "outline_width")):
        if r.get(src) is not None:
            style[dst] = r[src]
    # pointSize is a px DIAMETER; QGIS marker `size` is a diameter too, but in POINT units — so this
    # is a unit conversion (1px = 0.75pt at 96dpi), not the x2 trap that storing a radius would have
    # created. A 14px web default lands at ~10.5pt, next to the generator's hand-picked 8pt baseline.
    if r.get("pointSize") is not None:
        style["point_size"] = round(r["pointSize"] * 0.75, 2)
    if r.get("outlineColor") is not None:
        style["outline_color"] = _hex_or_default(r["outlineColor"])
    flattened = False
    color = None
    if r.get("type") == "simple":
        color = (r.get("symbol") or {}).get("color")
    elif r.get("type") == "uniqueValue":
        flattened = True
        color = (r.get("default") or {}).get("color")
        if not color:
            stops = r.get("stops") or []
            color = stops[0].get("color") if stops else None
    return _hex_or_default(color or display_color), style, flattened, r.get("pointIcon") is not None


async def _walk_web_map(
    conn: AsyncConnection, user: User, definition: dict[str, Any]
) -> tuple[list[LayerSpec], list[str]]:
    """F2: visible hosted vector layers in draw order -> LayerSpecs; everything else skipped
    with a loud warning code. Field lists come from the same source the ESRI door advertises
    (_esri_fields incl. the chute field) — the artifact's datasources are that door.

    WHICH layers are collectable (visible, hosted, ready, point/line/polygon, and genuinely
    can_view by tuple with NO admin override — the 2026-08-17 security-review finding and the
    2026-08-18 A50 bench) is decided by app/field_eligibility.collectable_layers, shared with
    the Talus+ field module so the two can never disagree. This function keeps only the
    generator's half: renderer flattening, the chute, and the ESRI field list."""
    from .geoservices import _chute_ok, _esri_fields  # lazy: sibling router, no cycle at import

    layers, warnings = await collectable_layers(conn, user.id, definition)

    def warn(code: str) -> None:
        if code not in warnings:
            warnings.append(code)

    specs: list[LayerSpec] = []
    for cl in layers:
        color, style, flattened, icon_dropped = _resolve_renderer(cl.display_color, cl.renderer)
        if flattened:
            warn(_GEN_WARN_FLATTENED)
        if icon_dropped:
            warn(_GEN_WARN_ICON_DROPPED)
        chute = _chute_ok(cl.layer_id, {"attachments_enabled": cl.attachments_enabled, "editable": cl.editable})
        fields, _lookup = await _esri_fields(conn, cl.layer_id, is_anon=False, chute=chute)
        names = [f["name"] for f in fields]
        specs.append(LayerSpec(
            layer_id=cl.layer_id,
            name=cl.title,
            geom=cl.geom_type,
            fields=names,
            editable=cl.editable,
            attachments=chute and "_talus_attach" in names,
            color=color,
            **style,
        ))
    return specs, warnings


async def _compile_and_validate(
    conn: AsyncConnection, spec: ProjectSpec
) -> tuple[str, int, fp.QgzInfo, str, list[str]]:
    """Compile, spool to a temp file, and run OUR OWN intake lip on it (self-check). Returns
    (temp path, size, sniff info, sha, compile warnings). A floor failure on our own output
    is OUR bug -> 500, never a 4xx. Caller unlinks the temp path."""
    try:
        data, compile_warnings = compile_project(spec)
    except GenerationError as exc:
        log.error("field_project_generate_failed", reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="field-project generation failed — see server logs",
        )
    fd, path = tempfile.mkstemp(suffix=".qgz")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        try:
            info, sha = await _validate_upload(conn, path)
        except HTTPException as exc:
            log.error("field_project_generate_selfcheck_failed", detail=str(exc.detail))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="generated artifact failed intake validation — template drift?",
            )
    except BaseException:
        _silent_unlink(path)
        raise
    return path, len(data), info, sha, compile_warnings


def _merged_warnings(*lists: list[str]) -> list[str]:
    out: list[str] = []
    for codes in lists:
        for c in codes:
            if c not in out:
                out.append(c)
    return out


@router.post(
    "/web-maps/{map_id}/field-project",
    status_code=status.HTTP_201_CREATED,
    summary="Generate a field project from a web map (publisher only)",
)
async def generate_field_project(
    map_id: str,
    payload: GenerateFieldProjectBody,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(_require_publisher),
) -> dict[str, Any]:
    """Walk -> mint (uncommitted!) -> compile -> self-check -> FGA owner tuple -> object ->
    row -> commit. The mint rides the same transaction as the row, so ANY failure rolls the
    token back with it — no orphaned live credentials, no cleanup-revoke. 400 (no eligible
    layers) happens BEFORE the mint; the audit events fire after the commit."""
    map_id, map_title, definition = await _load_map_for_view(conn, user, map_id)
    origin = await _advertised_base(conn)
    if not origin:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="set the advertised base URL in Admin ▸ Settings first — the generated "
                   "project must carry a phone-reachable address, and the server never "
                   "guesses its public hostname",
        )
    title = _clean_title(payload.title or map_title)
    description = _clean_description(payload.description)

    specs, gen_warnings = await _walk_web_map(conn, user, definition)
    if not specs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="this web map holds no field-collectable layers (visible hosted "
                   "point/line/polygon layers) — nothing to generate",
        )
    scope = "read_write" if any(s.editable for s in specs) else "read_only"
    label = f"field-project: {title}"[:80]

    initial = definition.get("initialView") or {}
    center = initial.get("center") or [0.0, 0.0]
    zoom = initial.get("zoom") or 2.0

    project_id = uuid4()
    object_key = _new_object_key(str(project_id))
    src_path: str | None = None
    try:
        async with conn.cursor() as cur:
            try:
                token, pat_row = await mint_pat(cur, user.id, label, scope)
            except TokenCapReached as exc:
                await conn.rollback()
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
            await _scope_project_token(cur, pat_row[0], specs)
            try:
                ciphertext = encrypt_secret(token)
            except SecretKeyUnavailable as exc:
                await conn.rollback()
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

            spec = ProjectSpec(
                title=title, origin=origin, pat=token, layers=specs,
                center=(float(center[0]), float(center[1])), zoom=float(zoom),
            )
            src_path, size, info, sha, compile_warnings = await _compile_and_validate(conn, spec)
            warnings = _merged_warnings(info.warnings, gen_warnings, compile_warnings)

            # OpenFGA FIRST — the owner grant (external; fails 503 and the txn rolls back).
            await _openfga_write([(f"user:{user.id}", "owner", f"field_project:{project_id}")])
            try:
                await run_in_threadpool(write_object_from_file, object_key, src_path, _MEDIA_TYPE)
                await cur.execute(
                    "INSERT INTO platform.field_projects "
                    "(id, title, description, bucket, object_key, size_bytes, content_sha256, "
                    " qgs_title, layer_count, warnings, created_by, owner_id, updated_by, "
                    " source_web_map_id, embedded_pat_id, embedded_pat_ciphertext) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING created_at, updated_at",
                    (str(project_id), title, description, settings.media_bucket, object_key,
                     size, sha, info.qgs_title, info.layer_count, Jsonb(warnings),
                     user.id, user.id, user.id, map_id, pat_row[0], ciphertext),
                )
                ts = await cur.fetchone()
                await conn.commit()
            except Exception:
                # Roll back durable state so no half-item survives — the uncommitted mint
                # and row die with the rollback; object + tuples are cleaned best-effort.
                await conn.rollback()
                try:
                    await run_in_threadpool(remove_object, object_key)
                except Exception:
                    log.warning("field_project_cleanup_object_failed", project=str(project_id))
                try:
                    await _openfga_delete(
                        await _openfga_read_object_tuples(f"field_project:{project_id}")
                    )
                except Exception:
                    log.warning("field_project_cleanup_tuple_failed", project=str(project_id))
                raise
    finally:
        if src_path is not None:
            _silent_unlink(src_path)

    log.info(  # after-commit, matching the mint route's ordering; the token is NEVER logged
        "pat_minted", token_id=str(pat_row[0]), owner=str(user.id), scope=scope, expires=False,
    )
    log.info(
        "field_project_generated", project=str(project_id), actor=str(user.id),
        source_map=map_id, layers=len(specs), scope=scope, warnings=warnings,
    )
    return {
        "id": str(project_id), "title": title, "description": description,
        "sizeBytes": size, "contentSha256": sha,
        "qgsTitle": info.qgs_title, "layerCount": info.layer_count, "warnings": warnings,
        "createdAt": ts[0], "updatedAt": ts[1],
        "ownerId": str(user.id), "deleteProtected": False,
        "sourceWebMapId": map_id,
        "pat": {"label": label, "scope": scope},
    }


@router.post(
    "/field-projects/{project_id}/regenerate",
    summary="Recompile from the source web map (owner or admin)",
)
async def regenerate_field_project(
    project_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """F6: owner-or-admin AND can_view on the source map; recompiles from the CURRENT map +
    registry with the SAME embedded token (deliberate reuse — crews' imported copies keep
    live-layer access), through the F4 replace-in-place discipline: validate fully, then
    overwrite the one object key; ETag/Last-Modified roll together."""
    project_id = await _load_for_manage(conn, claims, user, project_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT source_web_map_id, embedded_pat_id, embedded_pat_ciphertext "
            "FROM platform.field_projects WHERE id = %s",
            (project_id,),
        )
        row = await cur.fetchone()
    source_map_id, pat_id, ciphertext = row if row else (None, None, None)
    if source_map_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this field project has no source web map (uploaded, or the map was "
                   "deleted) — regenerate is unavailable; upload a replacement instead",
        )
    map_id, map_title, definition = await _load_map_for_view(conn, user, str(source_map_id))
    origin = await _advertised_base(conn)
    if not origin:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="set the advertised base URL in Admin ▸ Settings first",
        )
    if pat_id is None or ciphertext is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the project's embedded token is unavailable — delete this item and "
                   "generate a fresh field project",
        )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT scope, revoked_at, (expires_at IS NOT NULL AND expires_at < now()) "
            "FROM platform.personal_access_tokens WHERE id = %s",
            (pat_id,),
        )
        pat_state = await cur.fetchone()
    if pat_state is None or pat_state[1] is not None or pat_state[2]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the project's embedded token is revoked — delete this item and "
                   "generate a fresh field project",
        )
    try:
        token = decrypt_secret(ciphertext)
    except (SecretKeyUnavailable, SecretDecryptError) as exc:
        # §F1b posture: never proceed without the credential, never silently re-mint.
        log.warning("field_project_regenerate_secret_unavailable", project=project_id, reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the project's embedded token cannot be recovered on this server "
                   "(encryption key changed?) — delete this item and generate a fresh field project",
        )

    # Title/description are the ITEM's (rename survives regenerate); the compile title too.
    async with conn.cursor() as cur:
        await cur.execute("SELECT title FROM platform.field_projects WHERE id = %s", (project_id,))
        title = (await cur.fetchone())[0]

    specs, gen_warnings = await _walk_web_map(conn, user, definition)
    if not specs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="the source web map no longer holds any field-collectable layers — "
                   "nothing to regenerate",
        )
    if pat_state[0] == "read_only" and any(s.editable for s in specs):
        # Reuse keeps the minted scope (F1): if the map has grown editable layers since the
        # mint, the reused token cannot edit — warn loudly rather than silently re-scope.
        gen_warnings = _merged_warnings(gen_warnings, [_GEN_WARN_SCOPE])

    initial = definition.get("initialView") or {}
    center = initial.get("center") or [0.0, 0.0]
    zoom = initial.get("zoom") or 2.0
    spec = ProjectSpec(
        title=title, origin=origin, pat=token, layers=specs,
        center=(float(center[0]), float(center[1])), zoom=float(zoom),
    )
    src_path, size, info, sha, compile_warnings = await _compile_and_validate(conn, spec)
    warnings = _merged_warnings(info.warnings, gen_warnings, compile_warnings)
    try:
        old_key = await _current_object_key(conn, project_id)
        object_key = _new_object_key(project_id)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE platform.field_projects SET size_bytes = %s, content_sha256 = %s, "
                "qgs_title = %s, layer_count = %s, warnings = %s, updated_by = %s, "
                "object_key = %s, updated_at = now() WHERE id = %s "
                f"RETURNING {_DETAIL_COLS}",
                (size, sha, info.qgs_title, info.layer_count, Jsonb(warnings),
                 user.id, object_key, project_id),
            )
            det = await cur.fetchone()
            # The reused token follows the map's CURRENT layers (and a project generated before
            # K6, whose token had no list, gains one here).
            await _scope_project_token(cur, pat_id, specs)
        try:
            await run_in_threadpool(write_object_from_file, object_key, src_path, _MEDIA_TYPE)
        except Exception:
            await conn.rollback()
            try:
                await run_in_threadpool(remove_object, object_key)
            except Exception:
                pass
            raise
        await conn.commit()
        await _remove_replaced(old_key, object_key, project_id)
    finally:
        _silent_unlink(src_path)
    log.info("field_project_regenerated", project=project_id, actor=str(user.id),
             source_map=map_id, layers=len(specs), warnings=warnings)
    return _detail_dict(det)


# ---------------------------------------------------------------------------
# Detail + catalog list (Bearer)
# ---------------------------------------------------------------------------

@router.get("/field-projects/{project_id}", summary="Field-project detail (can_view)")
async def get_field_project(
    project_id: str,
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    project_id = _as_uuid(project_id)
    if not await _openfga_check(user.id, "can_view", project_id, object_type="field_project"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to view this field project")
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_DETAIL_COLS} FROM platform.field_projects WHERE id = %s", (project_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="field project not found")
    return _detail_dict(row)


class FieldProjectSummary(BaseModel):
    id: str
    title: str
    description: str | None = None
    created_at: Any = None
    updated_at: Any = None
    ownerId: str | None = None
    ownerName: str | None = None
    sizeBytes: int = 0
    qgsTitle: str | None = None
    layerCount: int | None = None
    warnings: list[str] = []
    shareLevel: str | None = None
    favorite: bool = False
    deleteProtected: bool = False
    sourceWebMapId: str | None = None


_SUMMARY_COLS = (
    "p.id, p.title, p.description, p.created_at, p.updated_at, p.owner_id, p.size_bytes, "
    "p.qgs_title, p.layer_count, p.warnings, p.delete_protected, p.source_web_map_id, "
    "COALESCE(u.display_name, u.username, u.email) "
    "FROM platform.field_projects p LEFT JOIN platform.users u ON p.owner_id = u.id"
)


async def _favorited_ids(conn: AsyncConnection, user_id: Any) -> set[str]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT item_id FROM platform.user_favorites WHERE user_id = %s AND item_type = 'field_project'",
            (user_id,),
        )
        return {r[0] for r in await cur.fetchall()}


async def _summaries(rows: list[Any], favs: set[str]) -> list[FieldProjectSummary]:
    out: list[FieldProjectSummary] = []
    for r in rows:
        try:
            shares = await get_object_shares(f"field_project:{r[0]}")
            level = (
                "public" if shares["publicAccess"]
                else "organization" if shares["organizationOnly"]
                else "groups" if shares["groups"]
                else "owner"
            )
        except HTTPException:
            level = None
        out.append(FieldProjectSummary(
            id=str(r[0]), title=r[1], description=r[2], created_at=r[3], updated_at=r[4],
            ownerId=str(r[5]) if r[5] else None, sizeBytes=r[6],
            qgsTitle=r[7], layerCount=r[8], warnings=r[9] or [],
            deleteProtected=r[10], sourceWebMapId=str(r[11]) if r[11] else None,
            ownerName=r[12],
            shareLevel=level, favorite=str(r[0]) in favs,
        ))
    return out


@router.get("/me/field-projects", summary="The caller's field projects — owned + shared with them")
async def my_field_projects(
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, list[FieldProjectSummary]]:
    # Owned keys on owner_id (transferable, T26) — a cheap DB query, no per-row OpenFGA trip.
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_SUMMARY_COLS} WHERE p.owner_id = %s ORDER BY p.updated_at DESC", (user.id,)
        )
        owned_rows = await cur.fetchall()
    owned_ids = {str(r[0]) for r in owned_rows}

    # Shared with me: degrade to empty on an OpenFGA blip (shows LESS, never more — the
    # /me/rasters posture) rather than 503-ing the whole catalog.
    try:
        viewable = await _openfga_list_objects(user.id, "can_view", "field_project")
        shared_ids = [v for v in viewable if v not in owned_ids]
    except HTTPException:
        log.warning("field_projects_shared_bucket_degraded", user_id=str(user.id))
        shared_ids = []

    shared_rows: list[Any] = []
    if shared_ids:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_SUMMARY_COLS} WHERE p.id = ANY(%s) ORDER BY p.updated_at DESC",
                (shared_ids,),
            )
            shared_rows = await cur.fetchall()

    favs = await _favorited_ids(conn, user.id)
    return {
        "owned": await _summaries(owned_rows, favs),
        "shared": await _summaries(shared_rows, favs),
    }


# ---------------------------------------------------------------------------
# The serving door — QField's Import URL (Bearer or PAT Basic; no anon tier)
# ---------------------------------------------------------------------------

def _safe_filename(title: str) -> str:
    clean = "".join(c for c in title if c.isprintable() and c not in '"\\/').strip() or "field-project"
    return f"{clean}.qgz"


def _etag_matches(header: str, etag: str) -> bool:
    """RFC 7232 If-None-Match: a weak/strong-tolerant token-list compare (W/ + quotes stripped),
    plus the `*` wildcard."""
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate.strip('"') == etag:
            return True
    return False


@router.api_route(
    "/field-projects/{project_id}/archive",
    methods=["GET", "HEAD"],
    summary="The project artifact — QField Import URL / portal download (Bearer or access token)",
)
async def serve_field_project(
    request: Request,
    project_id: str,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> Response:
    """The Import-URL door. Deny order (inv21): anon -> 401 challenge BEFORE any existence
    signal; then malformed/absent id -> 404; Bearer without view -> 403; PAT without view ->
    the 401 NO_VIEW invite. 304 on If-None-Match/If-Modified-Since — QField re-imports send
    conditional GETs, so an unchanged artifact costs headers, not bytes."""
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="sign in with an access token to fetch this field project",
            headers={"WWW-Authenticate": BASIC_CHALLENGE},
        )
    project_id = _as_uuid(project_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT title, object_key, size_bytes, content_sha256, updated_at "
            "FROM platform.field_projects WHERE id = %s",
            (project_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="field project not found")
    if not await _openfga_check(principal.id, "can_view", project_id, object_type="field_project"):
        if getattr(request.state, "pat_scope", None) is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="this access token's account can't see this field project — "
                "sign in with a token whose account has access",
                headers={"WWW-Authenticate": BASIC_CHALLENGE_NO_VIEW},
            )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to view this field project")

    title, object_key, size, sha, updated_at = row
    etag = f'"{sha}"'
    last_modified = format_datetime(updated_at.astimezone(timezone.utc), usegmt=True)
    base_headers = {
        "ETag": etag,
        "Last-Modified": last_modified,
        # Credentialed artifact: private keeps shared caches out; short-lived so a replace
        # propagates fast, while the 304 path makes unchanged re-imports cost headers only.
        "Cache-Control": "private, max-age=300",
    }

    inm = request.headers.get("if-none-match")
    if inm is not None:
        if _etag_matches(inm, sha):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=base_headers)
    else:
        ims = request.headers.get("if-modified-since")
        if ims:
            try:
                since = parsedate_to_datetime(ims)
            except (TypeError, ValueError):
                since = None
            if since is not None and int(updated_at.timestamp()) <= int(since.timestamp()):
                return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=base_headers)

    download_headers = {
        **base_headers,
        "Content-Length": str(size),
        "Content-Disposition": f'attachment; filename="{_safe_filename(title)}"',
    }
    if request.method == "HEAD":
        return Response(status_code=status.HTTP_200_OK, headers=download_headers, media_type=_MEDIA_TYPE)
    try:
        stream = iter_object(object_key)
        first = next(stream, None)  # surface missing-object/store errors as HTTP, not mid-stream
    except MediaObjectMissing:
        log.warning("field_project_object_missing", project=project_id, key=object_key)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="field project not found")
    except MediaStoreUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store unavailable")

    def _chunks():
        if first is not None:
            yield first
        yield from stream

    log.info("field_project_served", project=project_id, actor=str(principal.id), bytes=size)
    return StreamingResponse(_chunks(), media_type=_MEDIA_TYPE, headers=download_headers)


# ---------------------------------------------------------------------------
# Sharing + delete-protection (owner or admin; the rasters shapes)
# ---------------------------------------------------------------------------

class ShareGroup(BaseModel):
    group_id: str
    display_name: str


class FieldProjectShares(BaseModel):
    organizationOnly: bool = False
    groups: list[ShareGroup] = []
    publicAccess: bool = False   # always False in v1 — kept for SharingSection shape parity


class FieldProjectShareUpdate(BaseModel):
    organizationOnly: bool = False
    groups: list[str] = []
    publicAccess: bool = False


class FieldProjectDeleteProtection(BaseModel):
    deleteProtected: bool = False


async def _shares_response(conn: AsyncConnection, project_id: str) -> FieldProjectShares:
    shares = await get_object_shares(f"field_project:{project_id}")
    slugs: list[str] = shares["groups"]
    names: dict[str, str] = {}
    if slugs:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT group_id, display_name FROM platform.groups WHERE group_id = ANY(%s)", (slugs,)
            )
            names = {r[0]: r[1] for r in await cur.fetchall()}
    return FieldProjectShares(
        organizationOnly=shares["organizationOnly"],
        groups=[ShareGroup(group_id=s, display_name=names.get(s, s)) for s in slugs],
        publicAccess=shares["publicAccess"],
    )


@router.get("/field-projects/{project_id}/shares", summary="Current share state (owner or admin)")
async def get_field_project_shares(
    project_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> FieldProjectShares:
    project_id = await _load_for_manage(conn, claims, user, project_id)
    return await _shares_response(conn, project_id)


@router.put("/field-projects/{project_id}/shares", summary="Set/replace share state (owner or admin)")
async def set_field_project_shares(
    project_id: str,
    payload: FieldProjectShareUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> FieldProjectShares:
    project_id = await _load_for_manage(conn, claims, user, project_id)
    # No public tier in v1 (F3): the artifact may embed per-crew credentials, and the serving
    # door never consults public_shared — refusing here keeps the tuple state honest too.
    if payload.publicAccess:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="field projects cannot be shared publicly — they may embed access credentials",
        )
    requested = list(dict.fromkeys(payload.groups))  # de-dupe, preserve order
    if requested:
        async with conn.cursor() as cur:
            await cur.execute("SELECT group_id FROM platform.groups WHERE group_id = ANY(%s)", (requested,))
            known = {r[0] for r in await cur.fetchall()}
        unknown = [g for g in requested if g not in known]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown group(s): {', '.join(sorted(unknown))}"
            )
        # Member-scoping (the real gate; admin may share to any group).
        if not _has_admin_role(claims):
            member_of = set(await _openfga_read_user_groups(user.id))
            forbidden = [g for g in requested if g not in member_of]
            if forbidden:
                log.info("authz_rejected", capability="field_projects:share", reason="group_not_member",
                         user_id=str(user.id), project=project_id, groups=forbidden)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"not authorized to share to group(s): {', '.join(sorted(forbidden))}",
                )
    await set_object_shares(f"field_project:{project_id}", organization_only=payload.organizationOnly,
                            groups=requested)
    # Clears any stray public tuple (never settable through this API — defense in depth).
    await set_object_public(f"field_project:{project_id}", public=False)
    log.info("field_project_shares_set", project=project_id, actor=str(user.id),
             organization_only=payload.organizationOnly, groups=requested)
    return await _shares_response(conn, project_id)


@router.get("/field-projects/{project_id}/delete-protection",
            summary="Delete-protection flag (owner or admin)")
async def get_field_project_delete_protection(
    project_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> FieldProjectDeleteProtection:
    project_id = await _load_for_manage(conn, claims, user, project_id)
    async with conn.cursor() as cur:
        await cur.execute("SELECT delete_protected FROM platform.field_projects WHERE id = %s", (project_id,))
        row = await cur.fetchone()
    return FieldProjectDeleteProtection(deleteProtected=bool(row[0]))


@router.put("/field-projects/{project_id}/delete-protection",
            summary="Set delete-protection (owner or admin)")
async def set_field_project_delete_protection(
    project_id: str,
    payload: FieldProjectDeleteProtection,
    conn: AsyncConnection = Depends(get_db),
    claims: dict[str, Any] | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> FieldProjectDeleteProtection:
    project_id = await _load_for_manage(conn, claims, user, project_id)
    # Flag flip only — does NOT bump updated_at (it is the artifact's Last-Modified; see PATCH).
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.field_projects SET delete_protected = %s WHERE id = %s",
            (payload.deleteProtected, project_id),
        )
    await conn.commit()
    log.info("field_project_delete_protection_set", project=project_id, actor=str(user.id),
             protected=payload.deleteProtected)
    return FieldProjectDeleteProtection(deleteProtected=payload.deleteProtected)
