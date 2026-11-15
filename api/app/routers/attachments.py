"""Feature attachment REST — the web/core door onto media.attachments (decision-attachments.md A-1).

Generic over EVERY registered layer (slots / tables / views), like layer_query.py and unlike
slot-only layer_features.py — the capability is universal, not a property of one layer kind.
`layer_id` is proven a `platform.layers` row before it names a table (the same
injection discipline as layer_query.py); the attachment authority (app.attachments) does the storage.

The gate matrix (decision §2.1), fail-closed:
  * list / download  = can_view  OR anon-when-public (a public layer's attachments download anonymously,
    a private layer's never do — anon-on-private is 404, existence-hiding, exactly like the feature reads).
  * upload / delete   = can_edit AND editing_enabled (both via edit_allowed) AND attachments_enabled (a HARD
    capability switch, NOT admin-overridable — "off means off"; default false, opt-in).
Caps: 25 MB/file → 413 · disallowed/spoofed type → 415 · 51st-per-feature → 409 · OpenFGA-down → 503.
No row_version bump (side table). created_by + a structlog line are the A-1 audit trail.
"""
import io
import re
from typing import Any
from uuid import UUID

import structlog
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from PIL import Image, ImageOps
from psycopg import AsyncConnection

from .. import attachments as att
from ..idempotency import lookup_applied_op, optional_idempotency_key, record_applied_op
from ..auth import _validated_claims, get_current_user, get_optional_user, get_current_user_rw, rw_claims
from ..authz import edit_allowed, edit_deny_sentence, public_object_allowed, view_allowed
from ..config import settings
from ..db import get_db
from ..media import MediaObjectMissing, MediaStoreUnavailable, read_object, remove_object
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["attachments"])

# Immutable bytes per attachment id → cacheable; `private` because view-gated (never a shared/proxy cache).
_CACHE_CONTROL = "private, max-age=3600"

# Thumbnails are derived from immutable bytes, so they can cache longer than the download route —
# saves the re-resize on every popup re-open (decision-attachments-a4.md §3.3).
_THUMB_CACHE_CONTROL = "private, max-age=86400"
_THUMB_MAX_PX = 256
# The largest image a thumbnail is made from, read from the header before anything is decoded
# (once-over 2026-11-15 K3). A 25 MB attachment can be a far larger image once decoded (a flat PNG
# compresses enormously), the door serves public layers anonymously, and the library's own guard
# only refuses near 179 megapixels. 40 megapixels is well above any phone or survey camera.
_THUMB_MAX_SOURCE_PIXELS = 40_000_000

# Stamped into the audit trigger's client_info (the web/core door). The GeoServices façade (A-2) passes
# its own 'geoservices-api' so the audit shadow distinguishes desktop attachment writes.
_AUDIT_CLIENT = "portal-api"


# --- guards -------------------------------------------------------------------------------------

# The two server-generated layer-id shapes — the ONLY platform.layers populations since the pilot
# built-ins retired (decision-retire-builtins.md phase 2). Same belt-and-braces as layer_query /
# symbology: shape first, registry second, and only then may layer_id name a relation.
_SLOT_RE = re.compile(r"^tal_[0-9a-f]{8}$")
_VIEW_RE = re.compile(r"^vw_[0-9a-f]{12}$")


async def _require_registered(conn: AsyncConnection, layer_id: str) -> None:
    """layer_id must be shaped like a slot or view id AND be a platform.layers row before it ever
    names a table (bound param — together the checks make it one of a fixed known-safe set).
    404 otherwise (existence-hiding, matching the registry miss)."""
    if not (_SLOT_RE.fullmatch(layer_id) or _VIEW_RE.fullmatch(layer_id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.layers WHERE layer_id = %s", (layer_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")


async def _attachments_enabled(conn: AsyncConnection, layer_id: str) -> bool:
    """platform.layers.attachments_enabled (V068). Default false; a missing row reads False (fail-closed)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT attachments_enabled FROM platform.layers WHERE layer_id = %s", (layer_id,)
        )
        row = await cur.fetchone()
    return bool(row[0]) if row is not None else False


async def _gate_read(principal: User | None, conn: AsyncConnection, layer_id: str) -> None:
    """list/download read gate: authed can_view, else anon-only-if-public. Mirrors require_view_or_public —
    authed-without-view is 403; anon-on-non-public is 404 (existence-hiding)."""
    await _require_registered(conn, layer_id)
    if principal is not None:
        if not await view_allowed(principal, layer_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"not authorized to view {layer_id}")
        return
    if not await public_object_allowed(conn, layer_id, object_type="layer"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


async def _gate_write(conn: AsyncConnection, claims: dict | None, user: User, layer_id: str) -> None:
    """upload/delete write gate: edit_allowed (editable × editing_enabled × role × can_edit, admin-overridable
    on editing_enabled) AND attachments_enabled (hard — an admin turns it on via Manage, not god-mode).
    A photo is an attribute of its feature (decision-layer-edit-capabilities.md F2): op `attributes`."""
    await _require_registered(conn, layer_id)
    allowed, reason = await edit_allowed(conn, claims, user, layer_id, "attributes")
    if not allowed:
        log.info("authz_rejected", capability=f"{layer_id}:attach", reason=reason, user_id=str(user.id))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=edit_deny_sentence(reason) or f"not authorized to edit {layer_id}")
    if not await _attachments_enabled(conn, layer_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"attachments are not enabled for {layer_id}")


def _safe_disposition(filename: str) -> str:
    """Content-Disposition value — inline so images/PDFs preview; a sanitised filename (no quotes/controls)
    for the download name. Kept ASCII-simple (RFC 6266 filename* for full unicode is a later nicety)."""
    clean = "".join(c for c in filename if c.isprintable() and c not in '"\\').strip() or "attachment"
    return f'inline; filename="{clean}"'


def _thumbnail_bytes(data: bytes) -> bytes:
    """Resize image bytes to a ≤256px JPEG (decision-attachments-a4.md §3.3). Sync — run in a threadpool.
    EXIF-transposed (phone photos arrive rotated); alpha flattened onto white (PNG/WebP → JPEG). Raises on
    anything Pillow can't decode (HEIC without a plugin, truncated bytes, decompression bombs) — the route
    turns that into 404 and the client falls back to the file chip."""
    img = Image.open(io.BytesIO(data))  # lazy: reads the header only
    w, h = img.size
    if w * h > _THUMB_MAX_SOURCE_PIXELS:
        raise ValueError(f"image is {w}x{h}, over the thumbnail source limit")  # -> 404, the file chip
    img.draft("RGB", (_THUMB_MAX_PX * 2, _THUMB_MAX_PX * 2))  # JPEG: decode at a reduced scale
    img = ImageOps.exif_transpose(img)
    img.thumbnail((_THUMB_MAX_PX, _THUMB_MAX_PX))
    if img.mode != "RGB":
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        img = flat
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


# --- routes -------------------------------------------------------------------------------------


@router.get(
    "/layers/{layer_id}/features/{feature_id}/attachments",
    summary="List a feature's attachments (metadata) — view-gated, anon-when-public",
)
async def list_attachments(
    layer_id: str,
    feature_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    await _gate_read(principal, conn, layer_id)
    # Who uploaded each file is for signed-in viewers only: an anonymous caller on a public layer
    # gets the files' metadata and never a staff name or address (inv34 leg 9).
    rows = await att.list_for_feature(
        conn, layer_id, str(feature_id), include_uploader=principal is not None,
    )
    return {"layer_id": layer_id, "feature_id": str(feature_id), "attachments": rows}


@router.get(
    "/layers/{layer_id}/has-attachments",
    summary="Whether the layer has ANY attachments — the popup strip's per-layer probe (A-4)",
)
async def has_attachments(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    """One EXISTS per layer per map session (client promise-caches it) so attachment-less layers never
    pay a per-click request when a popup opens (decision-attachments-a4.md §3.1). Gated exactly like
    list/download: view or anon-when-public; anon-on-private is 404 (existence-hiding)."""
    await _gate_read(principal, conn, layer_id)
    return {"layer_id": layer_id, "hasAttachments": await att.any_for_layer(conn, layer_id)}


# The picker door's server-enforced bounds (decision-story-maps-attachment-picker.md §4.1/§5). Bounding is a
# security property here — a listing is an enumeration — so pydantic holds the caps, never the client.
_LIST_DEFAULT_LIMIT = 50
_LIST_MAX_LIMIT = 200
_LIST_MAX_FEATURE_IDS = 50


@router.get(
    "/layers/{layer_id}/attachments",
    summary="List a layer's attachments (metadata, paged) — view-gated, AUTHENTICATED-ONLY (the story-maps picker door)",
)
async def list_layer_attachments(
    layer_id: str,
    limit: int = Query(_LIST_DEFAULT_LIMIT, ge=1, le=_LIST_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    kind: str | None = Query(None, description="Optional content-type family filter; only 'image' is accepted"),
    feature_id: list[UUID] = Query(default=[], description="Optional, repeatable: restrict to these features"),
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The ONE new core read the story-maps attachment picker needs (decision-story-maps-attachment-picker.md
    F1): which features of a layer have attachments AND what they are, in one bounded call. Gated by the SAME
    _gate_read as every sibling — but behind get_current_user, DELIBERATELY narrower than the siblings'
    anon-when-public (F2): this is an authoring affordance, a viewer never calls it, so an anonymous caller is
    401 before the registry lookup (it cannot even probe a layer id's existence here). Do not "fix" that to
    get_optional_user. Rows carry id/feature_id/filename/content_type/byte_size/created_at only (att.list_for_layer)."""
    if kind is not None and kind != "image":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="kind must be 'image' when given")
    if len(feature_id) > _LIST_MAX_FEATURE_IDS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"at most {_LIST_MAX_FEATURE_IDS} feature_id values per call")
    await _gate_read(user, conn, layer_id)
    total, rows = await att.list_for_layer(
        conn, layer_id, limit=limit, offset=offset, images_only=(kind == "image"),
        feature_ids=[str(f) for f in feature_id] or None,
    )
    return {
        "layer_id": layer_id,
        "numberMatched": total,
        "numberReturned": len(rows),
        "attachments": rows,
    }


@router.post(
    "/layers/{layer_id}/features/{feature_id}/attachments",
    status_code=status.HTTP_201_CREATED,
    summary="Upload an attachment to a feature (multipart; editor + editing_enabled + attachments_enabled)",
)
async def upload_attachment(
    layer_id: str,
    feature_id: UUID,
    file: UploadFile = File(...),
    conn: AsyncConnection = Depends(get_db),
    # Talus Field f4 (decision-talus-field-f4.md §4.2 / §4.4): Bearer, or a read_write field-sync
    # PAT scoped to this layer; and an OPTIONAL Idempotency-Key so a replayed photo upload (a drain
    # that crashed between the upload and its local mark) returns the attachment it already stored
    # instead of storing a duplicate — the V036 table, item_type 'attachment'.
    user: User = Depends(get_current_user_rw),
    claims: dict | None = Depends(rw_claims),
    op_key: str | None = Depends(optional_idempotency_key),
) -> dict[str, Any]:
    await _gate_write(conn, claims, user, layer_id)
    prior_id = await lookup_applied_op(conn, op_key, "attachment")
    if prior_id is not None:
        # The same metadata shape a fresh upload returns, with 200 (nothing was created this time).
        prior = next((r for r in await att.list_for_feature(conn, layer_id, str(feature_id)) if r["id"] == prior_id), None)
        await conn.rollback()
        if prior is not None:
            return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder(prior))
        # the key was recorded but the attachment is gone (deleted since): a fresh upload is right
    # Read at most cap+1 bytes so an oversize upload costs bounded memory, then 413 (before any storage).
    data = await file.read(settings.attachment_max_bytes + 1)
    if len(data) > settings.attachment_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"attachment too large (max {settings.attachment_max_bytes // (1024 * 1024)} MB)",
        )
    content_type = att.validate_upload(file.filename or "", file.content_type, data)  # 400/415 on reject
    if not await att.feature_exists(conn, layer_id, str(feature_id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feature not found")
    try:
        row = await att.store(
            conn, layer_id=layer_id, feature_id=str(feature_id),
            filename=file.filename or "attachment", content_type=content_type,
            data=data, user=user, client=_AUDIT_CLIENT,
        )
    except HTTPException:  # count-cap 409 (raised before insert) — nothing to keep
        await conn.rollback()
        raise
    except MediaStoreUnavailable:  # blob write failed — roll back the row so it never dangles
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="media store unreachable")
    await record_applied_op(conn, op_key, "attachment", row["id"])
    await conn.commit()
    log.info("attachment_added", layer_id=layer_id, feature_id=str(feature_id),
             attachment_id=row["id"], byte_size=row["byte_size"], content_type=content_type,
             actor=str(user.id))
    return row


@router.get(
    "/layers/{layer_id}/attachments/{attachment_id}",
    summary="Download an attachment's bytes — view-gated, anon-when-public",
)
async def download_attachment(
    layer_id: str,
    attachment_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> Response:
    await _gate_read(principal, conn, layer_id)
    row = await att.get_row(conn, layer_id, str(attachment_id))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="attachment not found")
    try:
        data = await run_in_threadpool(read_object, row["object_key"])
    except MediaObjectMissing:
        log.warning("attachment_blob_missing", object_key=row["object_key"], attachment_id=str(attachment_id))
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="attachment bytes not found")
    except MediaStoreUnavailable:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="media store unreachable")
    return Response(
        content=data,
        media_type=row["content_type"],
        headers={"Cache-Control": _CACHE_CONTROL, "Content-Disposition": _safe_disposition(row["filename"])},
    )


@router.get(
    "/layers/{layer_id}/attachments/{attachment_id}/thumbnail",
    summary="A ≤256px JPEG thumbnail of an image attachment — view-gated, anon-when-public (A-4)",
)
async def attachment_thumbnail(
    layer_id: str,
    attachment_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    principal: User | None = Depends(get_optional_user),
) -> Response:
    """On-the-fly Pillow resize for the popup strip (decision-attachments-a4.md §3.3) — no stored
    thumbnails, no schema. Image content-types only; non-images and undecodable bytes (e.g. HEIC
    without a decoder plugin) are 404 so the client falls back to the typed file chip."""
    await _gate_read(principal, conn, layer_id)
    row = await att.get_row(conn, layer_id, str(attachment_id))
    if row is None or not row["content_type"].startswith("image/"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thumbnail not found")
    try:
        data = await run_in_threadpool(read_object, row["object_key"])
    except MediaObjectMissing:
        log.warning("attachment_blob_missing", object_key=row["object_key"], attachment_id=str(attachment_id))
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="attachment bytes not found")
    except MediaStoreUnavailable:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="media store unreachable")
    try:
        thumb = await run_in_threadpool(_thumbnail_bytes, data)
    except Exception:
        log.info("attachment_thumbnail_undecodable", attachment_id=str(attachment_id),
                 content_type=row["content_type"])
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thumbnail not available")
    return Response(
        content=thumb, media_type="image/jpeg",
        headers={"Cache-Control": _THUMB_CACHE_CONTROL},
    )


@router.delete(
    "/layers/{layer_id}/attachments/{attachment_id}",
    summary="Delete an attachment (editor + editing_enabled + attachments_enabled)",
)
async def delete_attachment(
    layer_id: str,
    attachment_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _gate_write(conn, claims, user, layer_id)
    object_key = await att.delete_row(conn, layer_id, str(attachment_id), user, _AUDIT_CLIENT)
    if object_key is None:
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="attachment not found")
    await conn.commit()
    # Best-effort blob removal AFTER commit (so a rolled-back delete never orphans row from blob); the T39
    # orphan-blob job GCs anything a store hiccup leaves behind.
    try:
        await run_in_threadpool(remove_object, object_key)
    except MediaStoreUnavailable:
        log.warning("attachment_blob_orphaned", object_key=object_key, attachment_id=str(attachment_id))
    log.info("attachment_deleted", layer_id=layer_id, attachment_id=str(attachment_id), actor=str(user.id))
    return {"deleted": str(attachment_id)}
