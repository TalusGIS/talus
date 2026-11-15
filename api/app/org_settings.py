"""Talus 22 — Org Settings store access + models.

A SINGLETON org-wide policy row (platform.org_settings, V028). This module holds the Pydantic
models + the read helper so the router and any future consumer (e.g. the anon-access slice's
share-path enforcement) share one source of truth and don't import each other.

Talus 23 (Public Access) surfaces `nonadmin_can_share_public` here: the GET projection now carries it
as a curated, UI-driving capability hint (the admin toggle reads/writes it; a non-admin owner's share
dialog reads it to decide whether to offer the Public tier — so it must be visible to non-admins, hence
it lives in the all-users projection, not an admin-only one). This is an explicit named field, not a
raw-row leak — consistent with Talus 22's curated-projection stance now that the flag has a consumer.
PUT updates it with PARTIAL semantics (None = leave unchanged) so the banner save never clobbers it.
"""
from typing import Literal

from psycopg import AsyncConnection
from pydantic import BaseModel, field_validator, model_validator

from .public_sharing import public_sharing_enabled

BannerLevel = Literal["info", "warning", "critical"]

_MAX_BANNER_LEN = 500
_MAX_HOME_HEADING = 140
_MAX_HOME_INTRO = 1000
_MAX_HOME_URL = 1000
_MAX_ADVERTISED_URL = 1000


class OrgHome(BaseModel):
    """The admin-configurable home-page text (Talus 30). All optional — the client renders sensible
    built-in defaults when a field is unset (NULL). Read by the PUBLIC bootstrap (anon-safe: it's
    org marketing copy, no sensitive data)."""

    heading: str | None
    intro: str | None
    heroImageUrl: str | None


class OrgSettingsPublic(BaseModel):
    """The client-facing projection (read by every signed-in user). Banner fields + two
    capability hints that drive the share dialog's Public tier: `publicAnonAccessEnabled` (the F6
    master switch — whether this instance offers public access at all) and `nonadminCanSharePublic`
    (whether a non-admin owner may use it).

    Since V092 `publicAnonAccessEnabled` carries the RESOLVED switch —
    `COALESCE(org_settings.public_sharing_enabled, PUBLIC_ANON_ACCESS_ENABLED)`
    (decision-public-sharing-reachable.md F2). The field keeps its name and its meaning to a client
    ("does this deployment offer public access"); only the source moved, from an env var no
    administrator could reach to a setting they can. The two hints therefore still read as a pair:
    the master switch, then who may use it."""

    bannerEnabled: bool
    bannerMessage: str | None
    bannerLevel: BannerLevel
    nonadminCanSharePublic: bool
    publicAnonAccessEnabled: bool
    # Talus 26: may a non-admin OWNER transfer their own catalog items to a colleague without an
    # admin? (Admin transfer is always allowed.) Default ON. A non-admin's Manage dialog reads this
    # to decide whether to offer the Transfer control — so, like nonadminCanSharePublic, it lives in
    # the all-users projection. The server (the transfer endpoint) is the authority.
    nonadminCanTransferOwnership: bool
    # Talus 30: the admin-configurable home-page text (heading / intro / hero image), so the Admin →
    # Settings form can read + edit it. Non-sensitive (it's the public hero copy, also served by the
    # anonymous /public/config bootstrap).
    home: "OrgHome"
    # Interop I-1 frontend (V063): the externally-reachable deployment root shown in connection
    # recipes (the "Use in…" wizard) — connection plumbing every signed-in user needs, so it lives
    # in the all-users projection like the capability hints. None = unset (the wizard falls back to
    # its build-time API base).
    advertisedBaseUrl: str | None


class OrgSettingsUpdate(BaseModel):
    """Admin PUT body. An enabled banner must carry a non-blank message (mirrors the DB CHECK).

    The two governance policies are OPTIONAL (None = leave unchanged) so a banner-only save never
    clobbers them, and a policy-only save never disturbs the banner."""

    bannerEnabled: bool
    bannerMessage: str | None = None
    bannerLevel: BannerLevel = "info"
    nonadminCanSharePublic: bool | None = None
    nonadminCanTransferOwnership: bool | None = None

    @field_validator("bannerMessage")
    @classmethod
    def _trim_message(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if len(v) > _MAX_BANNER_LEN:
            raise ValueError(f"banner message must be at most {_MAX_BANNER_LEN} characters")
        return v or None  # collapse blank/whitespace to None

    @model_validator(mode="after")
    def _message_required_when_enabled(self) -> "OrgSettingsUpdate":
        if self.bannerEnabled and not self.bannerMessage:
            raise ValueError("banner message is required when the banner is enabled")
        return self


class AdvertisedBaseUrlUpdate(BaseModel):
    """Admin PUT body for the advertised base URL (Interop I-1 frontend, V063). Written DIRECTLY
    (the home-PUT pattern): blank/None CLEARS the value back to unset. Kept as its own PUT so
    neither the banner/governance nor the home save can clobber it."""

    advertisedBaseUrl: str | None = None

    @field_validator("advertisedBaseUrl")
    @classmethod
    def _v_advertised(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > _MAX_ADVERTISED_URL:
            raise ValueError(f"advertised base URL must be at most {_MAX_ADVERTISED_URL} characters")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("advertised base URL must start with http:// or https://")
        # Normalize away trailing slashes so consumers can append paths without doubling.
        v = v.rstrip("/")
        # rstrip can only have eaten slashes, but a value like "https://" collapses to junk the DB
        # CHECK would still accept — require a non-empty host after the scheme.
        if v in ("http:", "https:", "http:/", "https:/"):
            raise ValueError("advertised base URL must include a host")
        return v


class OrgHomeUpdate(BaseModel):
    """Admin PUT body for the home-page text (Talus 30). All three are sent together and written
    DIRECTLY (not COALESCE) — so a blank/None CLEARS that field (reverts to the built-in default).
    Kept separate from the banner/governance PUT so neither save clobbers the other."""

    heading: str | None = None
    intro: str | None = None
    heroImageUrl: str | None = None

    @field_validator("heading")
    @classmethod
    def _v_heading(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if len(v) > _MAX_HOME_HEADING:
            raise ValueError(f"heading must be at most {_MAX_HOME_HEADING} characters")
        return v or None

    @field_validator("intro")
    @classmethod
    def _v_intro(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if len(v) > _MAX_HOME_INTRO:
            raise ValueError(f"intro must be at most {_MAX_HOME_INTRO} characters")
        return v or None

    @field_validator("heroImageUrl")
    @classmethod
    def _v_hero_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > _MAX_HOME_URL:
            raise ValueError(f"hero image URL must be at most {_MAX_HOME_URL} characters")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("hero image URL must start with http:// or https://")
        return v


# Safe default if the singleton row is somehow absent (V028 seeds it): no banner. The shell also
# degrades to "no banner" if the read fails entirely (client-side), so a missing notice is the
# safe outcome either way.
_DEFAULT_PUBLIC = OrgSettingsPublic(
    bannerEnabled=False, bannerMessage=None, bannerLevel="info", nonadminCanSharePublic=False,
    # Placeholder only: BOTH read paths below overwrite this with the resolver's answer, so the
    # master switch has exactly one source even in the row-missing branch.
    publicAnonAccessEnabled=False,
    # The safe default mirrors the DB column default (true). This default only applies if the
    # singleton row is somehow absent — the share/transfer consumers read fail-SAFE on a real error.
    nonadminCanTransferOwnership=True,
    home=OrgHome(heading=None, intro=None, heroImageUrl=None),
    advertisedBaseUrl=None,
)


async def read_org_home(conn: AsyncConnection) -> OrgHome:
    """The home-page text from the singleton row (all None if unset/missing → app defaults)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT home_heading, home_intro, home_hero_image_url "
            "FROM platform.org_settings WHERE id = true"
        )
        row = await cur.fetchone()
    if row is None:
        return OrgHome(heading=None, intro=None, heroImageUrl=None)
    return OrgHome(heading=row[0], intro=row[1], heroImageUrl=row[2])


async def read_org_settings(conn: AsyncConnection) -> OrgSettingsPublic:
    """Read the singleton's client-facing fields. Returns the safe default (no banner, public
    sharing restricted) if the row is missing (defensive — the migration seeds exactly one row)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT banner_enabled, banner_message, banner_level, nonadmin_can_share_public, "
            "nonadmin_can_transfer_ownership, home_heading, home_intro, home_hero_image_url, "
            "advertised_base_url "
            "FROM platform.org_settings WHERE id = true"
        )
        row = await cur.fetchone()
    # The master switch comes from the one resolver, never from `settings` directly — so this
    # projection (which drives whether the share dialog even OFFERS the Public tier) can never
    # disagree with the gate that would accept or refuse the share.
    if row is None:
        return _DEFAULT_PUBLIC.model_copy(
            update={"publicAnonAccessEnabled": await public_sharing_enabled(conn)}
        )
    return OrgSettingsPublic(
        bannerEnabled=row[0], bannerMessage=row[1], bannerLevel=row[2], nonadminCanSharePublic=row[3],
        publicAnonAccessEnabled=await public_sharing_enabled(conn),
        nonadminCanTransferOwnership=row[4],
        home=OrgHome(heading=row[5], intro=row[6], heroImageUrl=row[7]),
        advertisedBaseUrl=row[8],
    )
