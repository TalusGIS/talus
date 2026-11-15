"""Talus 22 — Org Settings (Admin → Settings).

GET /org-settings — any authenticated caller (the shell reads the banner). Curated projection
(banner fields only — the pre-placed public-sharing policy is not leaked). PUT /org-settings —
admin-gated; writes the singleton banner fields. The store + models live in `app.org_settings`;
the admin gate is the shared `deps._require_admin`.

GET/PUT /org-settings/public-sharing — the MASTER switch for anonymous public access
(decision-public-sharing-reachable.md). Its own endpoint, following the advertised-base-url / home
precedent on this page, for three reasons: enabling internet-facing publication deserves its own
deliberate confirmation rather than riding a banner save; it must never be clobbered by (or clobber)
an unrelated save; and it is the one setting here that warrants a governance audit row.
"""
import json
from typing import Literal

import structlog
from fastapi import APIRouter, Depends
from psycopg import AsyncConnection
from pydantic import BaseModel, Field, field_validator

from .. import admin_audit, basemap
from ..auth import get_current_user
from ..authz import _public_shared_objects
from ..db import get_db
from ..deps import _require_admin
from ..models.user import User
from ..org_settings import (
    AdvertisedBaseUrlUpdate,
    OrgHome,
    OrgHomeUpdate,
    OrgSettingsPublic,
    OrgSettingsUpdate,
    read_org_home,
    read_org_settings,
)
from ..public_sharing import public_sharing_enabled, public_sharing_source

log = structlog.get_logger(__name__)

router = APIRouter(tags=["org-settings"])


class DefaultExtentUpdate(BaseModel):
    """Admin PUT body for the org default map extent (V094 / decision-org-default-extent.md).
    A WGS84 [w, s, e, n] 4-array, range-gated by the SAME validator as the basemap fetch's bbox
    (one gate, two callers); the V094 jsonb CHECK is the shape-only DB backstop. None CLEARS back
    to unset (the coverage fit stands) — the advertised-base-url pattern. Its own PUT so a banner
    or home save can never clobber it."""

    extent: list[float] | None = Field(default=None, min_length=4, max_length=4)

    @field_validator("extent")
    @classmethod
    def _sane_wgs84(cls, v: list[float] | None) -> list[float] | None:
        return v if v is None else basemap.validate_wgs84_bbox(v)


class DefaultUnitsUpdate(BaseModel):
    """Admin PUT body for the org units default (V094 fork C). A CLOSED literal, never a free
    string — the value is handed to the client and fed into map construction (the embed scale
    bar, its only consumer today). Two values on purpose; 'nautical' is fenced (§7)."""

    units: Literal["metric", "imperial"]


class PublicSharingUpdate(BaseModel):
    """Admin PUT body for the master switch. Deliberately TWO-STATE: the admin decides on or off,
    and the write is always an explicit boolean. The NULL column state ('follow the deployment
    value') exists only so V092 doesn't un-publish an existing public deployment on upgrade; it is
    not a third choice a municipal administrator should have to reason about (F2)."""

    enabled: bool


class PublicSharingState(BaseModel):
    """The switch as the admin panel needs to render it.

    `publicItemCount` is what makes turning it OFF an informed act rather than a surprise: it is the
    number of items that would STOP BEING SERVED the moment the switch flips, because the toggle
    gates the anonymous READ path and not merely the write path (F3). None means the count could not
    be read (OpenFGA unreachable) — the panel then warns WITHOUT a number, because silently
    under-reporting public exposure would be worse than admitting the number is unavailable."""

    enabled: bool
    source: str  # 'override' (an admin decided it here) | 'default' (still follows the deployment)
    publicItemCount: int | None


async def _public_item_count() -> int | None:
    """How many items are currently shared publicly (layers + maps + rasters). Fail-SOFT to None:
    this drives a warning, and a settings page that 503s because the authorization service hiccuped
    would block an admin from CLOSING the door — the wrong way to fail for this particular read."""
    try:
        total = 0
        for object_type in ("layer", "web_map", "raster"):
            total += len(await _public_shared_objects(object_type))
        return total
    except Exception as exc:  # noqa: BLE001 — an unknown count is honest; a blocked page is not
        log.warning("public_item_count_failed", detail=str(exc))
        return None


async def _public_sharing_state(conn: AsyncConnection) -> PublicSharingState:
    return PublicSharingState(
        enabled=await public_sharing_enabled(conn),
        source=await public_sharing_source(conn),
        publicItemCount=await _public_item_count(),
    )


@router.get("/org-settings", summary="Org-wide settings (banner) — any authenticated caller")
async def get_org_settings(
    conn: AsyncConnection = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> OrgSettingsPublic:
    # Open to any authenticated user — the shell renders the banner for everyone. Only the curated
    # client-facing fields are returned (no nonadmin_can_share_public).
    return await read_org_settings(conn)


@router.put("/org-settings", summary="Update org-wide settings (admin only)")
async def update_org_settings(
    payload: OrgSettingsUpdate,
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> OrgSettingsPublic:
    # Singleton: UPDATE the one row (id = true), never INSERT a second. Pydantic has already
    # enforced "enabled ⇒ non-blank message" (mirrors the DB CHECK).
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.org_settings SET "
            "banner_enabled = %s, banner_message = %s, banner_level = %s, "
            # COALESCE: None leaves a governance policy unchanged (banner-only save), so the
            # concerns never clobber each other. nonadmin_can_share_public defaults restrictive
            # (false), nonadmin_can_transfer_ownership defaults permissive (true) — see V030.
            "nonadmin_can_share_public = COALESCE(%s, nonadmin_can_share_public), "
            "nonadmin_can_transfer_ownership = COALESCE(%s, nonadmin_can_transfer_ownership), "
            "updated_by = %s, updated_at = now() WHERE id = true",
            (payload.bannerEnabled, payload.bannerMessage, payload.bannerLevel,
             payload.nonadminCanSharePublic, payload.nonadminCanTransferOwnership, admin.id),
        )
    await conn.commit()
    log.info(
        "org_settings_updated",
        actor=str(admin.id),
        banner_enabled=payload.bannerEnabled,
        banner_level=payload.bannerLevel,
        nonadmin_can_share_public=payload.nonadminCanSharePublic,
        nonadmin_can_transfer_ownership=payload.nonadminCanTransferOwnership,
    )
    return await read_org_settings(conn)


@router.put("/org-settings/advertised-base-url", summary="Update the advertised base URL (admin only)")
async def update_advertised_base_url(
    payload: AdvertisedBaseUrlUpdate,
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> OrgSettingsPublic:
    # Direct write (the home-PUT pattern): blank/None CLEARS back to unset. Its own PUT so the
    # banner/governance and home saves never clobber it. Pydantic has normalized (trim, no
    # trailing slash) and enforced http(s) + host.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.org_settings SET "
            "advertised_base_url = %s, updated_by = %s, updated_at = now() WHERE id = true",
            (payload.advertisedBaseUrl, admin.id),
        )
    await conn.commit()
    log.info(
        "org_advertised_base_url_updated",
        actor=str(admin.id),
        advertised_base_url=payload.advertisedBaseUrl,
    )
    return await read_org_settings(conn)


@router.put("/org-settings/home", summary="Update the home-page text (admin only)")
async def update_org_home(
    payload: OrgHomeUpdate,
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> OrgHome:
    # All three fields are written DIRECTLY (a blank/None clears the field → built-in default).
    # Separate from the banner/governance PUT so the two saves never clobber each other.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.org_settings SET "
            "home_heading = %s, home_intro = %s, home_hero_image_url = %s, "
            "updated_by = %s, updated_at = now() WHERE id = true",
            (payload.heading, payload.intro, payload.heroImageUrl, admin.id),
        )
    await conn.commit()
    log.info("org_home_updated", actor=str(admin.id))
    return await read_org_home(conn)


@router.put("/org-settings/default-extent", summary="Set the org default map extent (admin only)")
async def update_default_extent(
    payload: DefaultExtentUpdate,
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> dict[str, object]:
    """Where a NEW map (/map) opens: beats the basemap coverage fit, loses to a saved map's own
    initialView (decision-org-default-extent.md fork H). Auto-seeded at basemap provision time
    when unset (fork F); this PUT is the explicit override — and None clears back to unset.
    Read side: the value rides the anonymous GET /basemap projection (fork B/B'), not here."""
    extent_json = None if payload.extent is None else json.dumps(payload.extent)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.org_settings SET "
            "default_extent = %s::jsonb, updated_by = %s, updated_at = now() WHERE id = true",
            (extent_json, admin.id),
        )
    await conn.commit()
    log.info("org_default_extent_updated", actor=str(admin.id), extent=payload.extent)
    return {"defaultExtent": payload.extent}


@router.put("/org-settings/default-units", summary="Set the org units default (admin only)")
async def update_default_units(
    payload: DefaultUnitsUpdate,
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> dict[str, object]:
    """Units for the embed scale bar — today's single consumer (the main map shows a unitless
    ratio scale; there is no measurement tool). Factory 'metric' (V094 fork C)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.org_settings SET "
            "default_units = %s, updated_by = %s, updated_at = now() WHERE id = true",
            (payload.units, admin.id),
        )
    await conn.commit()
    log.info("org_default_units_updated", actor=str(admin.id), units=payload.units)
    return {"defaultUnits": payload.units}


@router.get(
    "/org-settings/public-sharing",
    summary="The public-sharing master switch + how many items it currently serves (admin only)",
)
async def get_public_sharing(
    conn: AsyncConnection = Depends(get_db),
    _admin: User = Depends(_require_admin),
) -> PublicSharingState:
    # Admin-gated: the RESOLVED boolean is already public (/public/config tells any visitor whether
    # this deployment has a public face), but the COUNT of publicly-shared items is governance
    # information and stays behind the admin door.
    return await _public_sharing_state(conn)


@router.put(
    "/org-settings/public-sharing",
    summary="Turn public sharing on or off for this organization (admin only)",
)
async def update_public_sharing(
    payload: PublicSharingUpdate,
    conn: AsyncConnection = Depends(get_db),
    admin: User = Depends(_require_admin),
) -> PublicSharingState:
    """The switch that makes public map sharing, the public map embed, and the Open Data / DCAT-US
    feed reachable — previously an env var an administrator had no path to.

    Takes effect IMMEDIATELY, with no restart: every gate resolves the value per request
    (`public_sharing.py`). Turning it OFF therefore stops serving items that still carry a
    `public_shared` tuple, right now — which is the correct behaviour (the alternative leaves data
    exposed after an admin believes they closed it) and is why the UI states it before asking."""
    # Singleton UPDATE, and an EXPLICIT boolean: the write never restores NULL (F2 two-state).
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.org_settings SET "
            "public_sharing_enabled = %s, updated_by = %s, updated_at = now() WHERE id = true",
            (payload.enabled, admin.id),
        )
    await conn.commit()

    state = await _public_sharing_state(conn)
    log.info(
        "public_sharing_updated",
        actor=str(admin.id),
        enabled=payload.enabled,
        public_item_count=state.publicItemCount,
    )
    # Governance audit (threat 5): a compromised admin session opening the deployment to the
    # internet is the real cost of making this reachable, and the mitigation is that the act is
    # confirmed, attributed and queryable — not that the capability stays hidden from the product.
    await admin_audit.record(
        conn,
        actor_id=admin.id,
        action="public_sharing_toggled",
        target_type="org_settings",
        target_id="org",
        detail={"enabled": payload.enabled, "publicItemCount": state.publicItemCount},
    )
    return state
