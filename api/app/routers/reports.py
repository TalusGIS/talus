"""Talus 29 — Admin → Reports (operational reporting, admin-gated).

Read-only operational reports an admin reads in-portal AND downloads as CSV:
  - GET /admin/reports/content  — every layer + web map (the AGO Item-report analog) + a sharing
    breakdown summary (the governance lens: owner-only / organization / groups / PUBLIC).
  - GET /admin/reports/members  — every human (the AGO Member-report analog); reuses the exact
    Users-tab gather (`users.gather_human_users`) so the two can never drift.

Counts + tables only — NO charts, NO dashboard builder, NO BI over the GIS data (decision-admin-reports
§1 drift fence). `?format=csv` returns the same rows as a download. Every route behind _require_admin.
"""
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, Query, Response
from fastapi.concurrency import run_in_threadpool
from psycopg import AsyncConnection
from pydantic import BaseModel

from .. import keycloak_admin
from ..authz import (
    _objects_shared_with_group,
    _openfga_read_object_tuples,
    _public_shared_objects,
    get_object_shares_and_owner,
)
from ..config import settings
from ..db import get_db
from ..deps import _require_admin
from ..storage import FilesystemStorage
from ..models.user import User
from .users import gather_human_users

log = structlog.get_logger(__name__)

router = APIRouter(tags=["reports"])

Format = Literal["json", "csv"]


def _csv_response(filename: str, header: list[str], rows: list[list[Any]]) -> Response:
    """Render rows to a CSV download (Content-Disposition attachment)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _share_level(shares: dict[str, Any]) -> str:
    """Classify an item's sharing level — mirrors me._layer_share_and_owner (public > organization >
    groups > owner-only) so the report and the catalog badge agree."""
    if shares["publicAccess"]:
        return "public"
    if shares["organizationOnly"]:
        return "organization"
    if shares["groups"]:
        return "groups"
    return "owner"


# --- content inventory ----------------------------------------------------------


# A content item counts as "stale" if it hasn't been read via an API path in this many days (or ever).
_STALE_DAYS = 90


class ContentRow(BaseModel):
    itemType: str  # 'layer' | 'web_map'
    itemId: str
    title: str
    ownerId: str | None
    ownerName: str | None
    shareLevel: str | None  # None when OpenFGA couldn't be reached for this item (degraded)
    groupsShared: int
    deleteProtected: bool
    created_at: Any
    updated_at: Any
    lastAccessed: Any  # None = never accessed via an API read path since V076 (§5.4)


class ContentSummary(BaseModel):
    total: int
    byType: dict[str, int]
    byShareLevel: dict[str, int]
    ownerless: int
    staleCount: int  # never accessed OR not in the last _STALE_DAYS days


class ContentReport(BaseModel):
    summary: ContentSummary
    rows: list[ContentRow]


async def _content_rows(conn: AsyncConnection) -> list[ContentRow]:
    raw: list[tuple[str, str, str, Any, Any, bool, Any]] = []
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT layer_id, COALESCE(display_name, layer_id), created_at, updated_at, delete_protected, "
            "last_accessed_at FROM platform.layers ORDER BY COALESCE(display_name, layer_id)"
        )
        for layer_id, title, created, updated, dp, la in await cur.fetchall():
            raw.append(("layer", layer_id, title, created, updated, dp, la))
        await cur.execute(
            "SELECT id, title, created_at, updated_at, delete_protected, last_accessed_at "
            "FROM platform.web_maps ORDER BY title"
        )
        for mid, title, created, updated, dp, la in await cur.fetchall():
            raw.append(("web_map", str(mid), title, created, updated, dp, la))

    rows: list[ContentRow] = []
    owner_ids: set[str] = set()
    for item_type, item_id, title, created, updated, dp, la in raw:
        # Per-item share+owner; fail-SOFT (a 503 on one item degrades that row, never the report).
        try:
            shares, owner = await get_object_shares_and_owner(f"{item_type}:{item_id}")
            level: str | None = _share_level(shares)
            groups_n = len(shares["groups"])
        except Exception as exc:  # noqa: BLE001 — degrade this row, keep the report
            log.warning("report_share_read_failed", object=f"{item_type}:{item_id}", detail=str(exc))
            level, groups_n, owner = None, 0, None
        if owner:
            owner_ids.add(owner)
        rows.append(
            ContentRow(
                itemType=item_type, itemId=item_id, title=title, ownerId=owner, ownerName=None,
                shareLevel=level, groupsShared=groups_n, deleteProtected=bool(dp),
                created_at=created, updated_at=updated, lastAccessed=la,
            )
        )

    # Batch owner uuid -> display name (one query; no N+1).
    if owner_ids:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id::text, COALESCE(display_name, username, email) FROM platform.users "
                "WHERE id = ANY(%s)",
                (list(owner_ids),),
            )
            names = {r[0]: r[1] for r in await cur.fetchall()}
        for r in rows:
            if r.ownerId:
                r.ownerName = names.get(r.ownerId)
    return rows


def _content_summary(rows: list[ContentRow]) -> ContentSummary:
    by_type: dict[str, int] = {}
    by_level: dict[str, int] = {"owner": 0, "organization": 0, "groups": 0, "public": 0, "unknown": 0}
    ownerless = 0
    stale = 0
    stale_before = datetime.now(timezone.utc) - timedelta(days=_STALE_DAYS)
    for r in rows:
        by_type[r.itemType] = by_type.get(r.itemType, 0) + 1
        by_level[r.shareLevel or "unknown"] = by_level.get(r.shareLevel or "unknown", 0) + 1
        if r.ownerId is None:
            ownerless += 1
        if r.lastAccessed is None or r.lastAccessed < stale_before:
            stale += 1
    return ContentSummary(
        total=len(rows), byType=by_type, byShareLevel=by_level, ownerless=ownerless, staleCount=stale
    )


@router.get("/admin/reports/content", summary="Content inventory + sharing breakdown (JSON or CSV)")
async def content_report(
    format: Format = Query("json"),
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> Any:
    rows = await _content_rows(conn)
    if format == "csv":
        return _csv_response(
            "talus-content-inventory.csv",
            ["Title", "Type", "Owner", "Share Level", "Groups Shared", "Delete Protected",
             "Created", "Modified", "Last Accessed"],
            [
                [r.title, r.itemType, r.ownerName or "", r.shareLevel or "unknown",
                 r.groupsShared, "yes" if r.deleteProtected else "no",
                 r.created_at, r.updated_at, r.lastAccessed or ""]
                for r in rows
            ],
        )
    return ContentReport(summary=_content_summary(rows), rows=rows)


# --- public-content view (Talus 33 c6) ------------------------------------------
#
# The governance complement to the curated Open Data storefront: "show me EVERYTHING public" — every
# item carrying a `public_shared` tuple, not just the curated catalog (which is public ∩ curated). It
# catches over-exposure: an item made public outside the governed curate flow, or public via the
# web-map make-public cascade, or public-but-not-curated. Enumeration is fail-CLOSED (a public-
# exposure audit that silently under-reports is worse than an honest 503); per-item owner is fail-soft.


class PublicItemRow(BaseModel):
    itemType: str  # 'layer' | 'web_map'
    itemId: str
    title: str
    ownerName: str | None
    inOpenData: bool  # layer curated into the Open Data catalog (web_map always false in v1, F1)
    created_at: Any
    updated_at: Any


class PublicContentSummary(BaseModel):
    total: int
    byType: dict[str, int]
    notCurated: int  # public items NOT in Open Data — the over-exposure signal to review


class PublicContentReport(BaseModel):
    summary: PublicContentSummary
    rows: list[PublicItemRow]


async def _public_rows(conn: AsyncConnection) -> list[PublicItemRow]:
    # Authoritative "what is public" from OpenFGA (fail-CLOSED) + the curated set for the over-exposure
    # signal. A 503 here propagates — the report must not under-report public exposure.
    layer_ids = await _public_shared_objects("layer")
    map_ids = await _public_shared_objects("web_map")
    curated = set(await _objects_shared_with_group("open_data", "layer"))

    titles: dict[str, tuple[str, Any, Any]] = {}  # ref -> (title, created, updated)
    async with conn.cursor() as cur:
        if layer_ids:
            await cur.execute(
                "SELECT layer_id, COALESCE(display_name, layer_id), created_at, updated_at "
                "FROM platform.layers WHERE layer_id = ANY(%s)",
                (layer_ids,),
            )
            for lid, title, created, updated in await cur.fetchall():
                titles[f"layer:{lid}"] = (title, created, updated)
        if map_ids:
            await cur.execute(
                "SELECT id::text, title, created_at, updated_at FROM platform.web_maps WHERE id = ANY(%s)",
                (map_ids,),
            )
            for mid, title, created, updated in await cur.fetchall():
                titles[f"web_map:{mid}"] = (title, created, updated)

    rows: list[PublicItemRow] = []
    owner_ids: set[str] = set()
    staged: list[tuple[str, str, bool]] = []  # (item_type, item_id, in_open_data) + owner via read
    for item_type, ids in (("layer", layer_ids), ("web_map", map_ids)):
        for item_id in ids:
            ref = f"{item_type}:{item_id}"
            if ref not in titles:
                continue  # public tuple with no registry row (dangling) — skip
            title, created, updated = titles[ref]
            try:
                _shares, owner = await get_object_shares_and_owner(ref)
            except Exception as exc:  # noqa: BLE001 — degrade owner, keep the row
                log.warning("public_report_owner_read_failed", object=ref, detail=str(exc))
                owner = None
            if owner:
                owner_ids.add(owner)
            in_od = item_type == "layer" and item_id in curated
            rows.append(
                PublicItemRow(
                    itemType=item_type, itemId=item_id, title=title, ownerName=None,
                    inOpenData=in_od, created_at=created, updated_at=updated,
                )
            )
            staged.append((item_type, item_id, owner))  # owner uuid carried for the name join

    if owner_ids:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id::text, COALESCE(display_name, username, email) FROM platform.users "
                "WHERE id = ANY(%s)",
                (list(owner_ids),),
            )
            names = {r[0]: r[1] for r in await cur.fetchall()}
        for row, (_t, _i, owner) in zip(rows, staged):
            if owner:
                row.ownerName = names.get(owner)
    rows.sort(key=lambda r: r.title.lower())
    return rows


def _public_summary(rows: list[PublicItemRow]) -> PublicContentSummary:
    by_type: dict[str, int] = {}
    not_curated = 0
    for r in rows:
        by_type[r.itemType] = by_type.get(r.itemType, 0) + 1
        if not r.inOpenData:
            not_curated += 1
    return PublicContentSummary(total=len(rows), byType=by_type, notCurated=not_curated)


@router.get(
    "/admin/reports/public-content",
    summary="Every PUBLIC item (governance lens, broader than the Open Data catalog) — JSON or CSV",
)
async def public_content_report(
    format: Format = Query("json"),
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> Any:
    rows = await _public_rows(conn)
    if format == "csv":
        return _csv_response(
            "talus-public-content.csv",
            ["Title", "Type", "Owner", "In Open Data", "Created", "Modified"],
            [
                [r.title, r.itemType, r.ownerName or "", "yes" if r.inOpenData else "no",
                 r.created_at, r.updated_at]
                for r in rows
            ],
        )
    return PublicContentReport(summary=_public_summary(rows), rows=rows)


# --- activity report (admin-audit governance log) -------------------------------
#
# The AGO Activity-report analog (decision-admin-reports-round2 §4.3): who did what governance action
# and when, read from platform.admin_audit (V074). Governance-WIDE — the actor is whoever performed the
# action, not only admins. Newest-first, windowed (default + max limit so the report can't accidentally
# pull an unbounded history). Actor/target names resolved best-effort via batch joins (no N+1); an
# unresolved id shows raw (an append-only log outlives the rows it references).

_ACTIVITY_DEFAULT_LIMIT = 500
_ACTIVITY_MAX_LIMIT = 5000


class ActivityRow(BaseModel):
    at: Any
    actorId: str
    actorName: str | None
    actorType: str
    action: str
    targetType: str | None
    targetId: str | None
    targetName: str | None  # resolved for user/layer/web_map targets; None (raw id shown) otherwise
    detail: dict[str, Any]


class ActivitySummary(BaseModel):
    total: int
    byAction: dict[str, int]
    byActorType: dict[str, int]


class ActivityReport(BaseModel):
    summary: ActivitySummary
    rows: list[ActivityRow]


async def _activity_rows(
    conn: AsyncConnection, *, since: datetime | None, limit: int
) -> list[ActivityRow]:
    async with conn.cursor() as cur:
        if since is not None:
            await cur.execute(
                "SELECT at, actor_id::text, actor_type, action, target_type, target_id, detail "
                "FROM platform.admin_audit WHERE at >= %s ORDER BY at DESC LIMIT %s",
                (since, limit),
            )
        else:
            await cur.execute(
                "SELECT at, actor_id::text, actor_type, action, target_type, target_id, detail "
                "FROM platform.admin_audit ORDER BY at DESC LIMIT %s",
                (limit,),
            )
        raw = await cur.fetchall()

    rows = [
        ActivityRow(
            at=at, actorId=actor_id, actorName=None, actorType=actor_type, action=action,
            targetType=target_type, targetId=target_id, targetName=None, detail=detail or {},
        )
        for at, actor_id, actor_type, action, target_type, target_id, detail in raw
    ]

    # Actor names — one batch query over platform.users.
    actor_ids = {r.actorId for r in rows}
    if actor_ids:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id::text, COALESCE(display_name, username, email) FROM platform.users "
                "WHERE id = ANY(%s)",
                (list(actor_ids),),
            )
            actor_names = {r[0]: r[1] for r in await cur.fetchall()}
        for r in rows:
            r.actorName = actor_names.get(r.actorId)

    # Target names — best-effort, one batch query per resolvable type (user/layer/web_map). Other
    # target types (raster/group/external_ref/backup_settings) show the raw id.
    target_names: dict[tuple[str, str], str] = {}
    users_t = [r.targetId for r in rows if r.targetType == "user" and r.targetId]
    layers_t = [r.targetId for r in rows if r.targetType == "layer" and r.targetId]
    maps_t = [r.targetId for r in rows if r.targetType == "web_map" and r.targetId]
    async with conn.cursor() as cur:
        if users_t:
            await cur.execute(
                "SELECT id::text, COALESCE(display_name, username, email) FROM platform.users "
                "WHERE id = ANY(%s)",
                (users_t,),
            )
            for uid, name in await cur.fetchall():
                target_names[("user", uid)] = name
        if layers_t:
            await cur.execute(
                "SELECT layer_id, COALESCE(display_name, layer_id) FROM platform.layers "
                "WHERE layer_id = ANY(%s)",
                (layers_t,),
            )
            for lid, name in await cur.fetchall():
                target_names[("layer", lid)] = name
        if maps_t:
            await cur.execute(
                "SELECT id::text, title FROM platform.web_maps WHERE id = ANY(%s)",
                (maps_t,),
            )
            for mid, name in await cur.fetchall():
                target_names[("web_map", mid)] = name
    for r in rows:
        if r.targetType and r.targetId:
            r.targetName = target_names.get((r.targetType, r.targetId))
    return rows


def _activity_summary(rows: list[ActivityRow]) -> ActivitySummary:
    by_action: dict[str, int] = {}
    by_actor_type: dict[str, int] = {}
    for r in rows:
        by_action[r.action] = by_action.get(r.action, 0) + 1
        by_actor_type[r.actorType] = by_actor_type.get(r.actorType, 0) + 1
    return ActivitySummary(total=len(rows), byAction=by_action, byActorType=by_actor_type)


@router.get(
    "/admin/reports/activity",
    summary="Governance-action activity log — who did what, when (JSON or CSV)",
)
async def activity_report(
    format: Format = Query("json"),
    since: datetime | None = Query(None, description="Only actions at/after this ISO timestamp"),
    limit: int = Query(_ACTIVITY_DEFAULT_LIMIT, ge=1, le=_ACTIVITY_MAX_LIMIT),
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> Any:
    rows = await _activity_rows(conn, since=since, limit=limit)
    if format == "csv":
        return _csv_response(
            "talus-activity.csv",
            ["Time", "Actor", "Actor Type", "Action", "Target Type", "Target",
             "Target Name", "Detail"],
            [
                [r.at, r.actorName or r.actorId, r.actorType, r.action,
                 r.targetType or "", r.targetId or "", r.targetName or "",
                 json.dumps(r.detail, default=str)]
                for r in rows
            ],
        )
    return ActivityReport(summary=_activity_summary(rows), rows=rows)


# --- storage / capacity report --------------------------------------------------
#
# DB size + per-layer feature counts (reusing the T36 machinery, me._layer_feature_counts) + object-
# storage usage per source bucket. Counts + bytes only (the BI fence). Bucket sizing is a SYNC directory
# walk → run_in_threadpool; fail-SOFT per bucket (a store hiccup blanks one number, never the report).


class StorageLayerRow(BaseModel):
    layerId: str
    title: str
    featureCount: int | None  # None = registry row with no backing assets table


class StorageSummary(BaseModel):
    databaseBytes: int
    objectBuckets: dict[str, int | None]  # bucket -> total bytes (None = read failed/degraded)
    totalObjectBytes: int
    layerCount: int
    totalFeatures: int


class StorageReport(BaseModel):
    summary: StorageSummary
    rows: list[StorageLayerRow]


def _bucket_sizes() -> dict[str, int | None]:
    """Sum object bytes per source store (media + rasters) — a directory walk under the
    storage root (decision-object-store-build.md B7; keys are the bucket-name subtrees, so
    the report's shape is unchanged). A store with nothing written yet sums to 0. SYNC
    file I/O — call via run_in_threadpool."""
    from pathlib import Path

    out: dict[str, int | None] = {}
    for bucket in settings.backup_object_buckets_list:
        try:
            store = FilesystemStorage(Path(settings.storage_root) / bucket)
            out[bucket] = sum(o.size for o in store.list())
        except Exception as exc:  # noqa: BLE001 — degrade this bucket, keep the report
            log.warning("storage_bucket_size_failed", bucket=bucket, detail=str(exc))
            out[bucket] = None
    return out


@router.get(
    "/admin/reports/storage",
    summary="Storage / capacity — DB size, per-layer feature counts, object usage (JSON or CSV)",
)
async def storage_report(
    format: Format = Query("json"),
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> Any:
    from .me import _layer_feature_counts  # lazy: avoid a router import cycle

    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_database_size(current_database())")
        db_bytes = (await cur.fetchone())[0]
        await cur.execute(
            "SELECT layer_id, COALESCE(display_name, layer_id) FROM platform.layers "
            "ORDER BY COALESCE(display_name, layer_id)"
        )
        layers = await cur.fetchall()
    counts = await _layer_feature_counts(conn, {lid for lid, _ in layers})
    rows = [
        StorageLayerRow(layerId=lid, title=title, featureCount=counts.get(lid))
        for lid, title in layers
    ]
    buckets = await run_in_threadpool(_bucket_sizes)
    summary = StorageSummary(
        databaseBytes=db_bytes,
        objectBuckets=buckets,
        totalObjectBytes=sum(v for v in buckets.values() if v is not None),
        layerCount=len(rows),
        totalFeatures=sum(counts.values()),
    )
    if format == "csv":
        return _csv_response(
            "talus-storage.csv",
            ["Layer", "Layer ID", "Feature Count"],
            [[r.title, r.layerId, "" if r.featureCount is None else r.featureCount] for r in rows],
        )
    return StorageReport(summary=summary, rows=rows)


# --- groups inventory report ----------------------------------------------------
#
# Every registered group + its member count and shared-content count. Membership + content are OpenFGA
# reads (one per group + one per content-type); fail-SOFT per group (a degraded read blanks that group's
# counts and flips `degraded`, never fails the report — the usage-report precedent).

_GROUP_CONTENT_TYPES = ("layer", "web_map", "raster")


class GroupRow(BaseModel):
    groupId: str
    displayName: str
    memberCount: int | None   # None = OpenFGA membership read failed
    contentCount: int | None  # None = OpenFGA content read failed
    deleteProtected: bool
    created_at: Any


class GroupsSummary(BaseModel):
    total: int
    degraded: bool


class GroupsReport(BaseModel):
    summary: GroupsSummary
    rows: list[GroupRow]


@router.get("/admin/reports/groups", summary="Groups inventory — members + shared-content counts (JSON or CSV)")
async def groups_report(
    format: Format = Query("json"),
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> Any:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT group_id, display_name, delete_protected, created_at FROM platform.groups "
            "ORDER BY display_name"
        )
        raw = await cur.fetchall()

    rows: list[GroupRow] = []
    degraded = False
    for gid, name, dp, created in raw:
        try:
            tuples = await _openfga_read_object_tuples(f"group:{gid}")
            members: int | None = len(
                {u for u, rel, _ in tuples if rel == "member" and not u.endswith(":*")}
            )
        except Exception as exc:  # noqa: BLE001 — degrade this group's counts, keep the report
            log.warning("groups_report_member_read_failed", group=gid, detail=str(exc))
            members, degraded = None, True
        content: int | None = 0
        for ot in _GROUP_CONTENT_TYPES:
            try:
                content += len(await _objects_shared_with_group(gid, ot))
            except Exception as exc:  # noqa: BLE001
                log.warning("groups_report_content_read_failed", group=gid, type=ot, detail=str(exc))
                content, degraded = None, True
                break
        rows.append(
            GroupRow(
                groupId=gid, displayName=name, memberCount=members, contentCount=content,
                deleteProtected=bool(dp), created_at=created,
            )
        )
    if format == "csv":
        return _csv_response(
            "talus-groups.csv",
            ["Group", "Group ID", "Members", "Shared Content", "Delete Protected", "Created"],
            [
                [r.displayName, r.groupId,
                 "" if r.memberCount is None else r.memberCount,
                 "" if r.contentCount is None else r.contentCount,
                 "yes" if r.deleteProtected else "no", r.created_at]
                for r in rows
            ],
        )
    return GroupsReport(summary=GroupsSummary(total=len(rows), degraded=degraded), rows=rows)


# --- service-account inventory report -------------------------------------------
#
# Every service-account principal (platform.users principal_type='service') + its Keycloak enabled
# state, access level (read_write = holds the data-editor realm role), and last-active (last_seen_at,
# stamped for every authenticated principal incl. SAs). Keycloak/role reads are resilient ({} on failure
# => enabled shows "unknown", never a failed report).


class ServiceAccountRow(BaseModel):
    id: str
    clientId: str | None
    displayName: str
    enabled: bool | None   # None = not found in Keycloak (unknown)
    accessLevel: str       # 'read_write' | 'read_only'
    last_seen_at: Any
    created_at: Any


class ServiceAccountSummary(BaseModel):
    total: int
    enabled: int
    disabled: int


class ServiceAccountReport(BaseModel):
    summary: ServiceAccountSummary
    rows: list[ServiceAccountRow]


@router.get(
    "/admin/reports/service-accounts",
    summary="Service-account inventory — enabled state, access level, last active (JSON or CSV)",
)
async def service_accounts_report(
    format: Format = Query("json"),
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> Any:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id::text, kc_client_id, COALESCE(display_name, kc_client_id, id::text), "
            "last_seen_at, created_at FROM platform.users WHERE principal_type = 'service' "
            "ORDER BY created_at"
        )
        raw = await cur.fetchall()
    client_ids = [r[1] for r in raw if r[1]]
    enabled_map = await keycloak_admin.enabled_map(client_ids)
    rw_subs = await keycloak_admin.subs_with_realm_role(settings.keycloak_data_editor_role)
    rows = [
        ServiceAccountRow(
            id=sid, clientId=cid, displayName=name,
            enabled=enabled_map.get(cid) if cid else None,
            accessLevel="read_write" if sid in rw_subs else "read_only",
            last_seen_at=seen, created_at=created,
        )
        for sid, cid, name, seen, created in raw
    ]
    summary = ServiceAccountSummary(
        total=len(rows),
        enabled=sum(1 for r in rows if r.enabled is True),
        disabled=sum(1 for r in rows if r.enabled is False),
    )
    if format == "csv":
        return _csv_response(
            "talus-service-accounts.csv",
            ["Name", "Client ID", "Enabled", "Access", "Last Active", "Created"],
            [
                [r.displayName, r.clientId or "",
                 "yes" if r.enabled is True else ("no" if r.enabled is False else "unknown"),
                 r.accessLevel, r.last_seen_at or "", r.created_at]
                for r in rows
            ],
        )
    return ServiceAccountReport(summary=summary, rows=rows)


# --- member report --------------------------------------------------------------


class MemberSummary(BaseModel):
    total: int
    pending: int
    disabled: int
    byRole: dict[str, int]


class MemberReport(BaseModel):
    summary: MemberSummary
    rows: list[Any]  # UserOut shape (reused from the Users tab)


def _member_summary(rows: list[Any]) -> MemberSummary:
    by_role: dict[str, int] = {}
    pending = disabled = 0
    for u in rows:
        by_role[u.role or "unassigned"] = by_role.get(u.role or "unassigned", 0) + 1
        if u.last_seen_at is None:
            pending += 1
        if not u.active:
            disabled += 1
    return MemberSummary(total=len(rows), pending=pending, disabled=disabled, byRole=by_role)


@router.get("/admin/reports/members", summary="Member roster (JSON or CSV)")
async def member_report(
    format: Format = Query("json"),
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> Any:
    rows = await gather_human_users(conn)
    if format == "csv":
        return _csv_response(
            "talus-members.csv",
            ["Username", "Email", "Name", "Role", "Groups", "Status", "Last Seen", "Created"],
            [
                [u.username, u.email, u.display_name, u.role or "unassigned",
                 "; ".join(g.display_name for g in u.groups),
                 "active" if u.active else "disabled",
                 u.last_seen_at or "", u.created_at]
                for u in rows
            ],
        )
    return MemberReport(summary=_member_summary(rows), rows=rows)
