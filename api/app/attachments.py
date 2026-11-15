"""Feature attachment authority (decision-attachments.md A-1) — the ONE place attachment bytes are
validated, stored, listed, served, and deleted. Every door funnels through here: the web/core `/features`
routes (A-1, routers/attachments.py), the GeoServices `addAttachment` façade (A-2), and the QGIS
write-chute (A-3). So the caps + the content-type allowlist + the magic-byte fence + the `media.attachments`
side table stay a SINGLE source of truth. Attachment add/delete NEVER bumps a feature's row_version — an
attachment is not a feature edit (side table; Esri-consistent).

The fences (decision §4):
  * per-file byte cap (settings.attachment_max_bytes → 413) — enforced by the router as it reads the upload
  * per-feature count cap (settings.attachment_count_cap → 409) — enforced in `store`
  * content-type allowlist + filename-extension match + inline magic-byte signature (→ 415) — `validate_upload`
The magic-byte check is what makes "never executables" REAL rather than header-theater: an ELF / Mach-O / PE
/ script can never match an allowlisted signature, so even a spoofed Content-Type on a renamed file fails.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from fastapi.concurrency import run_in_threadpool
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .audit_context import set_audit_context
from .config import settings
from .media import write_object
from .models.user import User

# Every attachment blob lives under this key prefix in the media bucket. It is ALSO the orphan-sweep
# charter: maintenance.cleanup_orphaned_media considers ONLY keys under this prefix, because the
# bucket hosts other tenants (basemaps/, field-projects/) whose registries that job does not read —
# sweeping them was the field-projects security-review finding (decision-field-projects.md §5).
OBJECT_KEY_PREFIX = "attachments/"

# --- the allowlist (decision §4.4: safe core only — images + PDF + MS Office + txt/CSV). ----------------
# MIME → the filename extensions that legitimately carry it. The declared Content-Type must be a key here
# AND the filename's extension must be in its set (declared type and extension must agree). NEVER zip, GIS
# sidecars, or executables — add later only on demonstrated demand.
ALLOWLIST: dict[str, frozenset[str]] = {
    "image/jpeg": frozenset({"jpg", "jpeg"}),
    "image/png": frozenset({"png"}),
    "image/gif": frozenset({"gif"}),
    "image/webp": frozenset({"webp"}),
    "image/tiff": frozenset({"tif", "tiff"}),
    "image/heic": frozenset({"heic"}),
    "image/heif": frozenset({"heif"}),
    "image/bmp": frozenset({"bmp"}),
    "application/pdf": frozenset({"pdf"}),
    "application/msword": frozenset({"doc"}),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": frozenset({"docx"}),
    "application/vnd.ms-excel": frozenset({"xls"}),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": frozenset({"xlsx"}),
    "application/vnd.ms-powerpoint": frozenset({"ppt"}),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": frozenset({"pptx"}),
    "text/plain": frozenset({"txt"}),
    "text/csv": frozenset({"csv"}),
}

# MIME → a "magic family" whose leading bytes we can recognise. Several MIMEs share a family (all OOXML docs
# are ZIP containers; all legacy Office docs are OLE compound files; jpeg/png/... are their own).
_FAMILY: dict[str, str] = {
    "image/jpeg": "jpeg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
    "image/tiff": "tiff", "image/heic": "heic", "image/heif": "heic", "image/bmp": "bmp",
    "application/pdf": "pdf",
    "application/msword": "ole",
    "application/vnd.ms-excel": "ole",
    "application/vnd.ms-powerpoint": "ole",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "ooxml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "ooxml",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "ooxml",
    "text/plain": "text", "text/csv": "text",
}

# family → the byte prefixes that identify it. webp / heic / text are handled specially in _magic_ok
# (a fixed prefix isn't enough: webp is RIFF….WEBP, heic is an ftyp box, text is "no NUL byte").
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "jpeg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "gif": (b"GIF87a", b"GIF89a"),
    "tiff": (b"II*\x00", b"MM\x00*"),
    "bmp": (b"BM",),
    "pdf": (b"%PDF",),
    "ooxml": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),  # ZIP local-file / empty / spanned headers
    "ole": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),           # OLE2 compound-file (legacy .doc/.xls/.ppt)
}


# extension → MIME, derived from the allowlist. The QGIS write-chute door's type authority: the
# rig proved QgsWebDavExternalStorage sends NO Content-Type header at all, so the filename
# extension in the chute URL is the only honest declaration available (decision §9 rig note).
_EXT_TO_MIME: dict[str, str] = {
    ext: mime for mime, exts in ALLOWLIST.items() for ext in exts
}


def content_type_for_filename(filename: str) -> str | None:
    """The allowlisted MIME for this filename's extension, or None (⇒ 415 at the door).
    validate_upload still runs after this — the magic-byte fence is never skipped."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXT_TO_MIME.get(ext)


def _magic_ok(head: bytes, family: str) -> bool:
    """Does `head` (the file's leading bytes) match the expected `family`? The load-bearing fence: an
    executable's header matches NO allowlisted family, so a renamed/­spoofed upload is caught here."""
    if family == "webp":
        return len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP"
    if family == "heic":
        # HEIC/HEIF is an ISO-BMFF file: bytes 4..8 are the 'ftyp' box tag. Brand-tolerant on purpose
        # (heic/heix/heim/mif1/msf1/… all valid); the MIME allowlist already excluded non-image ftyp brands.
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if family == "text":
        # Real text/CSV has no NUL bytes; ELF/Mach-O/PE/DLL carry NULs in their first bytes → rejected.
        return b"\x00" not in head
    return any(head.startswith(sig) for sig in _SIGNATURES.get(family, ()))


def validate_upload(filename: str, content_type: str | None, data: bytes) -> str:
    """Enforce the allowlist (§4.4): declared Content-Type ∈ allowlist, filename extension matches that
    type, and the magic bytes agree. Returns the normalised content_type to store. Raises 400 (empty) or
    415 (disallowed / mismatched / spoofed). Every door validates through here, so the fence can't drift."""
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty file")
    ct = (content_type or "").split(";")[0].strip().lower()  # drop any "; charset=…" parameter
    allowed_exts = ALLOWLIST.get(ct)
    if allowed_exts is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"content type '{ct or 'unknown'}' is not allowed",
        )
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in allowed_exts:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"file extension '.{ext or ''}' does not match content type '{ct}'",
        )
    if not _magic_ok(data[:64], _FAMILY[ct]):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="file contents do not match the declared type (rejected as a possible disguised file)",
        )
    return ct


# --- the media.attachments authority (every door reads/writes THIS table). ------------------------------


async def count_for_feature(conn: AsyncConnection, layer_id: str, feature_id: str) -> int:
    """How many attachments this feature already has — the per-feature count-cap input."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM media.attachments WHERE layer_id = %s AND feature_id = %s::uuid",
            (layer_id, feature_id),
        )
        return int((await cur.fetchone())[0])


async def any_for_layer(conn: AsyncConnection, layer_id: str) -> bool:
    """Does this layer have ANY attachments? The A-4 popup probe (decision-attachments-a4.md §3.1):
    the web map asks once per layer per session so attachment-less layers never pay a per-click
    list request when a popup opens."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT EXISTS (SELECT 1 FROM media.attachments WHERE layer_id = %s)", (layer_id,)
        )
        return bool((await cur.fetchone())[0])


async def list_for_layer(
    conn: AsyncConnection,
    layer_id: str,
    *,
    limit: int,
    offset: int,
    images_only: bool = False,
    feature_ids: list[str] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """The layer-wide, paged attachment listing — the story-maps picker door
    (decision-story-maps-attachment-picker.md §4.1). Returns (numberMatched, rows).

    Rows carry a STRICT SUBSET of `list_for_feature`'s shape: id, feature_id, filename, content_type,
    byte_size, created_at — never object_key (a bypass handle), never created_by / created_by_name
    (identity), never capture provenance. `layer_id` is the path's registry-proven id and binds EVERY
    predicate, so a `feature_ids` filter can never reach across layers (it is ANDed under it).

    Order is a TOTAL order (offset paging never splits or repeats a group): newest feature-group
    first (max created_at per feature), then newest attachment first within the group.
    """
    where = ["a.layer_id = %(lid)s"]
    params: dict[str, Any] = {"lid": layer_id, "lim": limit, "off": offset}
    if images_only:
        where.append("a.content_type LIKE %(kind)s")
        params["kind"] = "image/%"
    if feature_ids:
        where.append("a.feature_id = ANY(%(fids)s::uuid[])")
        params["fids"] = list(feature_ids)
    where_sql = " AND ".join(where)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT count(*) AS n FROM media.attachments a WHERE {where_sql}", params)
        total = int((await cur.fetchone())["n"])
        await cur.execute(
            "SELECT a.id::text AS id, a.feature_id::text AS feature_id, a.filename, a.content_type, "
            "       a.byte_size, a.created_at "
            f"FROM media.attachments a WHERE {where_sql} "
            "ORDER BY max(a.created_at) OVER (PARTITION BY a.feature_id) DESC, a.feature_id, "
            "         a.created_at DESC, a.id DESC "
            "LIMIT %(lim)s OFFSET %(off)s",
            params,
        )
        rows = list(await cur.fetchall())
    return total, rows


async def feature_exists(conn: AsyncConnection, layer_id: str, feature_id: str) -> bool:
    """Is `feature_id` a real row in assets.<layer_id>? Call ONLY after the caller proved `layer_id` is a
    registered platform.layers row (so `sql.Identifier` names one of a fixed known-safe set, never raw
    input). Works for slot + table + view layers uniformly (all `assets.<layer_id>`)."""
    q = sql.SQL("SELECT 1 FROM {tbl} WHERE id = %s::uuid").format(tbl=sql.Identifier("assets", layer_id))
    async with conn.cursor() as cur:
        await cur.execute(q, (feature_id,))
        return await cur.fetchone() is not None


async def store(
    conn: AsyncConnection,
    *,
    layer_id: str,
    feature_id: str,
    filename: str,
    content_type: str,
    data: bytes,
    user: User,
    client: str,
    captured_at: Any = None,
    captured_geom_wkt: str | None = None,
    device_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert the row + write the blob for ONE attachment. `content_type` must already be validated
    (validate_upload). Enforces the per-feature count cap (409). Sets the audit-context GUCs so the
    media.attachments trigger stamps the actor (`user`) + `client` (e.g. 'portal-api' / 'geoservices-api').
    Does NOT commit — the caller controls the transaction. Ordering (decision §2.4): count-check → INSERT
    (uncommitted) → write blob → caller commits. A blob-store failure raises before returning so the caller
    rolls back the row (a committed row ALWAYS has its blob); a crash after the blob write but before commit
    orphans only the blob, which the T39 job GCs."""
    cap = settings.attachment_count_cap
    if await count_for_feature(conn, layer_id, feature_id) >= cap:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"feature already has the maximum {cap} attachments",
        )
    att_id = uuid4()
    object_key = f"{OBJECT_KEY_PREFIX}{layer_id}/{feature_id}/{att_id}"
    geom_expr = (
        sql.SQL("ST_SetSRID(ST_GeomFromText(%(geom)s), 4326)")
        if captured_geom_wkt is not None else sql.SQL("NULL")
    )
    query = sql.SQL(
        "INSERT INTO media.attachments "
        "  (id, layer_id, feature_id, object_key, filename, content_type, byte_size, created_by, "
        "   captured_at, captured_geom, device_metadata) "
        "VALUES (%(id)s, %(layer_id)s, %(feature_id)s::uuid, %(object_key)s, %(filename)s, "
        "        %(content_type)s, %(byte_size)s, %(created_by)s, %(captured_at)s, {geom}, %(device)s) "
        "RETURNING id::text AS id, esri_id, filename, content_type, byte_size, created_at"
    ).format(geom=geom_expr)
    async with conn.cursor(row_factory=dict_row) as cur:
        await set_audit_context(cur, user, client)  # trigger stamps changed_by/actor_type/client_info
        await cur.execute(
            query,
            {
                "id": str(att_id), "layer_id": layer_id, "feature_id": feature_id,
                "object_key": object_key, "filename": filename, "content_type": content_type,
                "byte_size": len(data), "created_by": str(user.id), "captured_at": captured_at,
                "geom": captured_geom_wkt,
                "device": Jsonb(device_metadata) if device_metadata is not None else None,
            },
        )
        row = await cur.fetchone()
    # Blob AFTER the (uncommitted) insert: a store failure here rolls back the row cleanly (no orphan).
    await run_in_threadpool(write_object, object_key, data, content_type)
    return row


_LIST_FOR_FEATURE_COLS = (
    "a.id::text AS id, a.esri_id, a.filename, a.content_type, a.byte_size, a.created_at, a.captured_at"
)


async def list_for_feature(
    conn: AsyncConnection, layer_id: str, feature_id: str, *, include_uploader: bool = False,
) -> list[dict[str, Any]]:
    """The attachmentInfos-shaped metadata list for one feature (creation order). The client downloads each
    via GET /layers/{layer_id}/attachments/{id}.

    ★ `include_uploader` adds `created_by_name` — the uploader's display name, or username, or EMAIL
    ADDRESS when no display name is set. It is OFF unless the caller asks, and the one caller that
    asks (the per-feature REST door) asks only for a signed-in principal. That door also answers an
    anonymous caller on a public layer, and the anonymous surface never carries editor identity
    (decision-public-access.md F2; rider 2026-11-06 — it did, until then). Off by default so a new
    caller that forgets the question gets no name rather than leaking one; when off the users table
    is not joined at all."""
    if include_uploader:
        query = (
            f"SELECT {_LIST_FOR_FEATURE_COLS}, "
            "       COALESCE(u.display_name, u.username, u.email) AS created_by_name "
            "FROM media.attachments a LEFT JOIN platform.users u ON a.created_by = u.id "
        )
    else:
        query = f"SELECT {_LIST_FOR_FEATURE_COLS} FROM media.attachments a "
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            query + "WHERE a.layer_id = %s AND a.feature_id = %s::uuid ORDER BY a.created_at, a.id",
            (layer_id, feature_id),
        )
        return list(await cur.fetchall())


async def get_row(conn: AsyncConnection, layer_id: str, attachment_id: str) -> dict[str, Any] | None:
    """The bytes-serving row for one attachment (object_key + content_type + filename), scoped to layer_id
    so a mismatched layer in the URL can't reach another layer's attachment. None ⇒ 404."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT object_key, content_type, filename, byte_size, feature_id::text AS feature_id "
            "FROM media.attachments WHERE layer_id = %s AND id = %s::uuid",
            (layer_id, attachment_id),
        )
        return await cur.fetchone()


async def get_by_filename(
    conn: AsyncConnection, layer_id: str, feature_id: str, filename: str
) -> dict[str, Any] | None:
    """The NEWEST attachment with this filename on this feature — the QGIS chute GET resolves
    feature-addressed URLs (…/attachments/<oid>/<filename>) through here. Re-uploading the same
    filename makes the newer row win (the URL always serves the latest bytes). None ⇒ 404."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT object_key, content_type, filename, byte_size FROM media.attachments "
            "WHERE layer_id = %s AND feature_id = %s::uuid AND filename = %s "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (layer_id, feature_id, filename),
        )
        return await cur.fetchone()


async def get_by_esri_id(
    conn: AsyncConnection, layer_id: str, feature_id: str, esri_id: int
) -> dict[str, Any] | None:
    """The (layer, feature, esri_id)-scoped fetch — how the A-2 GeoServices endpoints resolve the
    integer `attachmentId` the Esri wire carries (download/update/delete). esri_id is globally
    unique (V069), but the scope stays load-bearing: an id fished from another feature or layer
    resolves to nothing here (cross-talk hygiene). None ⇒ 404 / per-result not-found."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id::text AS id, esri_id, object_key, content_type, filename, byte_size "
            "FROM media.attachments "
            "WHERE layer_id = %s AND feature_id = %s::uuid AND esri_id = %s",
            (layer_id, feature_id, esri_id),
        )
        return await cur.fetchone()


async def update_attachment(
    conn: AsyncConnection,
    *,
    layer_id: str,
    feature_id: str,
    esri_id: int,
    filename: str,
    content_type: str,
    data: bytes,
    user: User,
    client: str,
) -> dict[str, Any] | None:
    """Replace an attachment's bytes IN PLACE, keeping `esri_id` stable (Esri updateAttachment —
    Pro expects the id to survive; decision §11.6). `content_type` must already be validated.
    Ordering keeps store()'s invariant (a committed row ALWAYS has its blob): UPDATE the row to a
    fresh object_key (uncommitted, audit GUCs stamped) → write the NEW blob → caller commits →
    caller removes the returned `old_object_key` AFTER commit (a crash between blob write and
    commit orphans only the new blob — T39 GCs it; a blob-store failure raises so the caller
    rolls back to the old row+blob, both intact). Does NOT bump row_version (side table).
    None ⇒ no such attachment on this feature (per-result not-found)."""
    new_key = f"{OBJECT_KEY_PREFIX}{layer_id}/{feature_id}/{uuid4()}"
    async with conn.cursor(row_factory=dict_row) as cur:
        await set_audit_context(cur, user, client)  # the UPDATE lands in audit.attachments_audit
        await cur.execute(
            "UPDATE media.attachments a "
            "SET object_key = %s, filename = %s, content_type = %s, byte_size = %s "
            "FROM (SELECT id, object_key FROM media.attachments "
            "      WHERE layer_id = %s AND feature_id = %s::uuid AND esri_id = %s) old "
            "WHERE a.id = old.id "
            "RETURNING a.id::text AS id, a.esri_id, a.filename, a.content_type, a.byte_size, "
            "          old.object_key AS old_object_key",
            (new_key, filename, content_type, len(data), layer_id, feature_id, esri_id),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    # Blob AFTER the (uncommitted) update, exactly like store(): a write failure here raises and
    # the caller rolls back — the row reverts to the old object_key, whose blob still exists.
    await run_in_threadpool(write_object, new_key, data, content_type)
    return row


async def latest_for_features(
    conn: AsyncConnection, layer_id: str, feature_ids: list[str]
) -> dict[str, str]:
    """{feature uuid → newest attachment's filename} for one page of features, in ONE query — the
    chute echo field (_talus_attach) is computed per query page, never per feature (no N+1)."""
    if not feature_ids:
        return {}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT DISTINCT ON (feature_id) feature_id::text, filename FROM media.attachments "
            "WHERE layer_id = %s AND feature_id = ANY(%s::uuid[]) "
            "ORDER BY feature_id, created_at DESC, id DESC",
            (layer_id, feature_ids),
        )
        return {fid: fname for fid, fname in await cur.fetchall()}


async def delete_by_esri_id(
    conn: AsyncConnection, layer_id: str, feature_id: str, esri_id: int, user: User, client: str
) -> str | None:
    """delete_row's esri-handle twin (the A-2 deleteAttachments loop): ONE scoped DELETE resolves
    and removes the row, returning its object_key for the caller's post-commit blob removal.
    Same audit GUCs (a durable, attributable DELETE shadow row), same no-commit contract, same
    row_version silence. None ⇒ no such attachment on this feature (per-result not-found)."""
    async with conn.cursor() as cur:
        await set_audit_context(cur, user, client)
        await cur.execute(
            "DELETE FROM media.attachments "
            "WHERE layer_id = %s AND feature_id = %s::uuid AND esri_id = %s RETURNING object_key",
            (layer_id, feature_id, esri_id),
        )
        row = await cur.fetchone()
    return row[0] if row is not None else None


async def delete_for_feature(
    conn: AsyncConnection, layer_id: str, feature_id: str, user: User, client: str
) -> list[str]:
    """Delete ALL of one feature's attachment rows — the feature-delete cascade
    (decision-feature-delete-attachments.md; the §2.4 half of decision-attachments.md that was
    specified but never built). Returns their object_keys for the caller's post-commit blob removal.
    Empty list ⇒ the feature had none, which is NOT an error (most features have no attachments).

    Two contracts the callers depend on, both load-bearing:

      * SCOPED (layer_id, feature_id) — NEVER feature_id alone. The same uuid can legitimately exist
        in two different layers (a preserve-GlobalID Append can put it there deliberately), so an
        unscoped delete would destroy an unrelated layer's attachments. This is the single most
        important line in this function.
      * Call it AFTER the feature row's own DELETE, in the same transaction. Attachments-first would
        fire a cover_photo SET NULL (V079) against a still-present row, bumping its
        row_version (V035) so the feature DELETE then matches zero rows — a spurious 409 with the
        attachments already gone.

    Same as delete_row/delete_by_esri_id otherwise: sets the audit GUCs so every removed row leaves a
    durable attributable DELETE shadow (audit.attachments_audit, V068:102-104); does NOT commit; does
    NOT touch row_version (an attachment is not a feature edit).

    Bounded by settings.attachment_count_cap (50/feature), so one statement and one post-commit blob
    loop is always the right shape — no batching."""
    async with conn.cursor() as cur:
        await set_audit_context(cur, user, client)
        await cur.execute(
            "DELETE FROM media.attachments "
            "WHERE layer_id = %s AND feature_id = %s::uuid RETURNING object_key",
            (layer_id, feature_id),
        )
        return [r[0] for r in await cur.fetchall()]


async def delete_row(
    conn: AsyncConnection, layer_id: str, attachment_id: str, user: User, client: str
) -> str | None:
    """Delete ONE attachment's row (scoped to layer_id), returning its object_key for the caller to remove
    from the blob store AFTER commit (so a rolled-back delete never orphans the DB row from its blob). Sets
    the audit-context GUCs so the trigger records a durable DELETE row (actor + before_row) — a deletion is
    forensically attributable. None ⇒ nothing deleted (404). Does NOT commit; does NOT touch row_version."""
    async with conn.cursor() as cur:
        await set_audit_context(cur, user, client)
        await cur.execute(
            "DELETE FROM media.attachments WHERE layer_id = %s AND id = %s::uuid RETURNING object_key",
            (layer_id, attachment_id),
        )
        row = await cur.fetchone()
    return row[0] if row is not None else None
