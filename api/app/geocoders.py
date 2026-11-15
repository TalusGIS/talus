"""Talus 37 — geocoder management store (admin-configured org locators).

The org's locator list, moved out of the GEOCODERS env var into platform.geocoders (V037) so the GIS
admin manages it in-portal instead of via a deploy-time .env edit + api restart. The /geocode proxy
builds its adapter list from `active_geocoder_configs()` (this module) instead of `settings.geocoders`,
behind the unchanged /geocode/* API contract. Locators stay org-wide / shared-by-default.

TOKEN LOCATORS (decision-geocoder-token-locators.md — closes the T37 §3 F2 "tokenless v1" deferral):
an arcgis locator may carry a static token, stored as app-side Fernet CIPHERTEXT (V073,
app/secrets.py) and WRITE-ONLY through the API — clients only ever see hasToken/tokenHint (last-4).
The plaintext exists in app memory alone: encrypted before the INSERT/UPDATE params, decrypted only
into the proxy's GeocoderConfig walk and the admin Test probe. A row whose ciphertext can't be
decrypted (key loss/mismatch — F1b) is SKIPPED from the proxy walk with a warning, never queried
tokenless. The ENV-UNION stays as back-compat/bootstrap (`active_geocoder_configs` = enabled DB rows
∪ token-bearing env entries not shadowed by a DB name); the UI is the primary path. The startup seed
still backfills only TOKENLESS env entries — a token-bearing env locator is re-entered in the UI
deliberately (that's the migration path off env).

This module holds the Pydantic models + the store helpers; it imports only `config` (never `geocode`),
so the orchestration layer stays decoupled from the store — the router wires store → orchestration.
"""
from typing import Literal

import structlog
from psycopg import AsyncConnection
from psycopg.types.json import Json
from pydantic import BaseModel, field_validator, model_validator

from .config import GeocoderConfig, settings
from .geocode_internal import _MAX_FIELDS, create_locator_index, drop_locator_index, validate_mapping
from .secrets import SecretDecryptError, SecretKeyUnavailable, decrypt_secret, encrypt_secret

log = structlog.get_logger(__name__)

GeocoderType = Literal["arcgis", "nominatim", "internal"]

_MAX_NAME = 120
_MAX_URL = 2000
_MAX_TOKEN = 2048  # generous for an ArcGIS token; a fence against absurd input, not a format check


def _validate_url(v: str) -> str:
    """http/https only (decision §3 F4 — SSRF posture). Admin-trusted, so scheme-restrict + note it,
    no allowlist. Rejects file:/gopher:/etc. so the proxy can't be aimed at a non-HTTP target."""
    v = v.strip()
    if not v:
        raise ValueError("url is required")
    if not (v.startswith("http://") or v.startswith("https://")):
        raise ValueError("url must start with http:// or https://")
    if len(v) > _MAX_URL:
        raise ValueError(f"url must be at most {_MAX_URL} characters")
    return v


def _validate_extent(v: list[float] | None) -> list[float] | None:
    """Optional fixed search extent [xmin, ymin, xmax, ymax] in EPSG:4326 (mirrors GeocoderConfig)."""
    if v is not None and len(v) != 4:
        raise ValueError("extent must be [xmin, ymin, xmax, ymax] (4 numbers, EPSG:4326)")
    return v


def _validate_name(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("name is required")
    if len(v) > _MAX_NAME:
        raise ValueError(f"name must be at most {_MAX_NAME} characters")
    return v


def _validate_address_fields(v: list[str] | None) -> list[str] | None:
    """Shape-check the internal mapping (decision-own-data-geocoder §3 F2): 1..N non-empty, distinct
    field names. Whether each name resolves on the layer is checked against the registry/live schema
    at create/edit/Test time (geocode_internal), not here."""
    if v is None:
        return None
    if not v or len(v) > _MAX_FIELDS:
        raise ValueError(f"addressFields must name 1..{_MAX_FIELDS} fields")
    cleaned = [f.strip() for f in v]
    if any(not f or len(f) > 63 for f in cleaned):
        raise ValueError("each address field must be a non-empty name (at most 63 characters)")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("addressFields must not repeat a field")
    return cleaned


def _validate_locality_fields(v: list[str] | None) -> list[str] | None:
    """Shape-check the locality list (decision-batch-geocoding-county-scale §F3a): 0..N non-empty,
    distinct names — empty is the norm (a single-town layer needs none). Disjointness from
    addressFields is a model-level check; existence on the layer is checked at create/edit/Test."""
    if v is None:
        return None
    if len(v) > _MAX_FIELDS:
        raise ValueError(f"localityFields must name at most {_MAX_FIELDS} fields")
    cleaned = [f.strip() for f in v]
    if any(not f or len(f) > 63 for f in cleaned):
        raise ValueError("each locality field must be a non-empty name (at most 63 characters)")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("localityFields must not repeat a field")
    return cleaned


class GeocoderRow(BaseModel):
    """One admin-managed locator (the full management projection). `url` is None ONLY for
    type=internal (Mode B), which instead carries the source layer + address-field mapping.
    The token is WRITE-ONLY (decision-geocoder-token-locators §3 F2): this projection carries
    only its presence (`hasToken`), a last-4 legibility hint, and `tokenUnavailable` — true when
    ciphertext exists but the configured TALUS_ENCRYPTION_KEY can't open it (F1b key loss/mismatch),
    so the admin screen can badge "credential unavailable"."""

    id: str
    name: str
    type: GeocoderType
    url: str | None
    outSR: int
    extent: list[float] | None
    order: int
    enabled: bool
    layerId: str | None = None
    addressFields: list[str] | None = None
    # The locality (tiebreak + label) fields — always a list; [] for external types (V107).
    localityFields: list[str] = []
    hasToken: bool = False
    tokenHint: str | None = None
    tokenUnavailable: bool = False


class GeocoderWrite(BaseModel):
    """Create / replace body. A PUT carries the full set (full replace), matching the prompt's
    {name, type, url, outSR, order, enabled} model (+ optional extent, kept per decision §3
    F-extent). Mode B: type=internal swaps `url` for `layerId` + `addressFields` — the per-type
    shape is enforced here (friendly 422) and again by the V066 CHECK (the belt)."""

    name: str
    type: GeocoderType
    url: str | None = None
    outSR: int = 4326
    extent: list[float] | None = None
    order: int = 0
    enabled: bool = True
    layerId: str | None = None
    addressFields: list[str] | None = None
    # Mode B: the locality (city / ZIP) fields — tiebreak + label, never scored (V107). Omitted or
    # null on an internal locator = none; on an external type it must be empty.
    localityFields: list[str] | None = None
    # WRITE-ONLY credential (decision-geocoder-token-locators §3 F2): `token` set/replaces the
    # stored secret; `clearToken` removes it; BOTH OMITTED on an edit = keep what's stored (a
    # write-only field can't round-trip, so a full-replace PUT deliberately does NOT null it).
    token: str | None = None
    clearToken: bool = False

    _v_name = field_validator("name")(staticmethod(_validate_name))
    _v_extent = field_validator("extent")(staticmethod(_validate_extent))
    _v_fields = field_validator("addressFields")(staticmethod(_validate_address_fields))
    _v_locality = field_validator("localityFields")(staticmethod(_validate_locality_fields))

    @model_validator(mode="after")
    def _shape_by_type(self) -> "GeocoderWrite":
        # Normalize empty-ish values to None FIRST: a client that serializes an untouched form
        # field as "" (not omitting it) must not sail past the truthiness checks and trip the V066
        # CHECK as a raw 500 — '' and NULL mean the same thing here.
        if self.url is not None and not self.url.strip():
            self.url = None
        if self.layerId is not None and not self.layerId.strip():
            self.layerId = None
        if self.token is not None:
            self.token = self.token.strip() or None
        if self.token is not None:
            if len(self.token) > _MAX_TOKEN:
                raise ValueError(f"token must be at most {_MAX_TOKEN} characters")
            if self.clearToken:
                raise ValueError("token and clearToken are mutually exclusive")
            # token= is an ArcGIS auth mode — the only one any adapter speaks (decision §3 F3).
            if self.type != "arcgis":
                raise ValueError("a token only applies to arcgis locators")
        if self.type == "internal":
            if self.url:
                raise ValueError("an internal locator has no url")
            if not self.layerId or not self.addressFields:
                raise ValueError("an internal locator requires layerId and addressFields")
            self.localityFields = self.localityFields or []
            if set(self.localityFields) & set(self.addressFields):
                raise ValueError("a field cannot be both an address field and a locality field")
        else:
            if self.layerId or self.addressFields or self.localityFields:
                raise ValueError("layerId/addressFields/localityFields only apply to internal locators")
            self.localityFields = []
            if not self.url:
                raise ValueError("url is required")
            self.url = _validate_url(self.url)
        return self


class GeocoderReorder(BaseModel):
    """New order: `ids` in the desired sequence; each row's sort_order becomes its index."""

    ids: list[str]


class GeocoderTestRequest(BaseModel):
    """Probe a locator before relying on it (decision §3 F3). Either an existing `id`, or an ad-hoc
    {type, url, outSR, extent} — or, for an internal def, {type, layerId, addressFields} — so a
    brand-new locator can be tested before it's saved."""

    id: str | None = None
    type: GeocoderType | None = None
    url: str | None = None
    outSR: int = 4326
    extent: list[float] | None = None
    layerId: str | None = None
    addressFields: list[str] | None = None
    localityFields: list[str] | None = None
    # TRANSIENT credential for an ad-hoc (unsaved) arcgis test (decision §3 F5): validate the
    # token BEFORE saving it. Used for this one probe, never stored, never logged.
    token: str | None = None
    q: str = "1 main st"  # a harmless sample query — we only care whether the service responds

    _v_extent = field_validator("extent")(staticmethod(_validate_extent))
    _v_fields = field_validator("addressFields")(staticmethod(_validate_address_fields))
    _v_locality = field_validator("localityFields")(staticmethod(_validate_locality_fields))

    @model_validator(mode="after")
    def _clean_token(self) -> "GeocoderTestRequest":
        if self.token is not None:
            self.token = self.token.strip() or None
        if self.token is not None and len(self.token) > _MAX_TOKEN:
            raise ValueError(f"token must be at most {_MAX_TOKEN} characters")
        return self


class GeocoderTestResult(BaseModel):
    ok: bool
    sampleCount: int
    error: str | None = None


def _row_to_config(
    name: str, type_: str, url: str | None, out_sr: int, extent, order: int,
    layer_id: str | None = None, address_fields: list[str] | None = None,
    token: str | None = None, locality_fields: list[str] | None = None,
) -> GeocoderConfig:
    return GeocoderConfig(
        name=name, type=type_, url=url, out_sr=out_sr, extent=extent, order=order,
        layer_id=layer_id, address_fields=address_fields, token=token,
        locality_fields=locality_fields or None,
    )


# All CRUD reads/writes select the same column set, in the same order, as GeocoderRow expects.
# token_ciphertext is consumed INSIDE _row (presence + decryptability check) and never serialized.
_ROW_COLS = (
    "id, name, type, url, out_sr, extent, sort_order, enabled, layer_id, address_fields, "
    "token_ciphertext, token_hint, locality_fields"
)


def _row(r) -> GeocoderRow:
    cipher, hint = r[10], r[11]
    unavailable = False
    if cipher is not None:
        # Cheap availability probe (Fernet decrypt is HMAC+AES, microseconds) so the admin screen
        # can badge a key-loss row (F1b) instead of the admin discovering it via a broken search.
        try:
            decrypt_secret(cipher)
        except (SecretKeyUnavailable, SecretDecryptError):
            unavailable = True
    return GeocoderRow(
        id=str(r[0]), name=r[1], type=r[2], url=r[3], outSR=r[4],
        extent=r[5], order=r[6], enabled=r[7], layerId=r[8], addressFields=r[9],
        localityFields=list(r[12] or []),
        hasToken=cipher is not None, tokenHint=hint, tokenUnavailable=unavailable,
    )


async def read_geocoders(conn: AsyncConnection) -> list[GeocoderRow]:
    """The full ordered list (enabled + disabled) for the admin management screen."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_ROW_COLS} FROM platform.geocoders ORDER BY sort_order, name"
        )
        rows = await cur.fetchall()
    return [_row(r) for r in rows]


async def create_geocoder(conn: AsyncConnection, body: "GeocoderWrite", actor_id) -> GeocoderRow:
    """Insert a locator. A duplicate name raises psycopg UniqueViolation (the router maps it to
    409). An internal locator additionally gets its trigram index minted in the SAME transaction
    (decision-own-data-geocoder §3 F5; transactional DDL, the register_view discipline) — a bad
    mapping raises MappingError (router → 422) and nothing is created."""
    # Encrypt BEFORE any SQL (plaintext never transits the SQL layer — decision §3 F1 / T4).
    # SecretKeyUnavailable propagates to the router (→ actionable 422), nothing inserted.
    cipher = encrypt_secret(body.token) if body.token else None
    hint = body.token[-4:] if body.token else None
    if body.type == "internal":
        # Both lists proven against the layer's catalog (+ disjoint) BEFORE any row exists — a bad
        # locality field is a 422 with nothing inserted, same as a bad address field.
        await validate_mapping(conn, body.layerId, body.addressFields, body.localityFields or [])
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO platform.geocoders "
            "(name, type, url, out_sr, extent, sort_order, enabled, layer_id, address_fields, "
            "token_ciphertext, token_hint, updated_by, locality_fields) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_ROW_COLS}",
            (
                body.name, body.type, body.url, body.outSR,
                Json(body.extent) if body.extent is not None else None,
                body.order, body.enabled, body.layerId,
                Json(body.addressFields) if body.addressFields is not None else None,
                cipher, hint, actor_id, Json(body.localityFields or []),
            ),
        )
        r = await cur.fetchone()
    if body.type == "internal":
        await create_locator_index(conn, str(r[0]), body.layerId, body.addressFields)
    await conn.commit()
    if body.token:
        # Audited admin action (decision §3 F6) — the EVENT, never the value.
        log.info("geocoder_token_set", actor=str(actor_id), name=body.name, replaced=False)
    return _row(r)


async def update_geocoder(
    conn: AsyncConnection, gid, body: "GeocoderWrite", actor_id
) -> GeocoderRow | None:
    """Full-replace one locator. Returns None if `gid` doesn't exist (router → 404); a name clash
    raises UniqueViolation (router → 409). The trigram index follows the MAPPING (F5), not every
    edit: it is dropped + recreated only when the internal mapping actually changed (layer or
    fields), and just dropped when the type moved away from internal. A rename / reorder / enabled
    toggle must NOT rebuild — a GIN build over a big address table inside the request transaction
    blocks the layer, and re-validating an unchanged (possibly gone-stale) mapping would stop an
    admin from DISABLING a broken locator. One transaction; a bad mapping on a real mapping change
    raises MappingError (router → 422) and the whole edit rolls back."""
    # Token semantics (decision §3 F2 — write-only fields can't round-trip, so this PUT is
    # full-replace for everything EXCEPT the token): `token` set → replace; `clearToken` → clear;
    # neither → keep what's stored. A type moving off arcgis force-clears (no other adapter speaks
    # token=). Encrypt BEFORE any SQL (§3 F1 / T4); SecretKeyUnavailable → router 422.
    if body.token:
        token_set = "token_ciphertext = %s, token_hint = %s, "
        token_params: tuple = (encrypt_secret(body.token), body.token[-4:])
    elif body.clearToken or body.type != "arcgis":
        token_set = "token_ciphertext = NULL, token_hint = NULL, "
        token_params = ()
    else:
        token_set = ""
        token_params = ()
    async with conn.cursor() as cur:
        # The pre-edit mapping, to decide whether index work is needed at all (+ had_token for F6).
        await cur.execute(
            "SELECT type, layer_id, address_fields, token_ciphertext IS NOT NULL, locality_fields "
            "FROM platform.geocoders WHERE id = %s",
            (gid,),
        )
        old = await cur.fetchone()
        if old is None:
            return None
        await cur.execute(
            "UPDATE platform.geocoders SET "
            f"{token_set}"
            "name = %s, type = %s, url = %s, out_sr = %s, extent = %s, sort_order = %s, "
            "enabled = %s, layer_id = %s, address_fields = %s, locality_fields = %s, "
            "updated_by = %s, updated_at = now() "
            f"WHERE id = %s RETURNING {_ROW_COLS}",
            token_params + (
                body.name, body.type, body.url, body.outSR,
                Json(body.extent) if body.extent is not None else None,
                body.order, body.enabled, body.layerId,
                Json(body.addressFields) if body.addressFields is not None else None,
                Json(body.localityFields or []),
                actor_id, gid,
            ),
        )
        r = await cur.fetchone()
    if r is None:
        return None
    was_internal = old[0] == "internal"
    # The INDEX follows the street mapping only (layer + address_fields); locality fields are never
    # indexed, so a locality-only edit validates but does not rebuild.
    mapping_changed = (old[1], old[2]) != (body.layerId, body.addressFields)
    locality_changed = list(old[4] or []) != list(body.localityFields or [])
    if body.type == "internal" and (mapping_changed or locality_changed):
        await validate_mapping(conn, body.layerId, body.addressFields, body.localityFields or [])
    if was_internal and (body.type != "internal" or mapping_changed):
        await drop_locator_index(conn, str(gid))
    if body.type == "internal" and (not was_internal or mapping_changed):
        await create_locator_index(conn, str(gid), body.layerId, body.addressFields)
    await conn.commit()
    # Audited admin actions (decision §3 F6) — the EVENT, never the value.
    had_token = old[3]
    if body.token:
        log.info(
            "geocoder_token_set", actor=str(actor_id), id=str(gid), name=body.name,
            replaced=bool(had_token),
        )
    elif token_set and had_token:
        log.info("geocoder_token_cleared", actor=str(actor_id), id=str(gid), name=body.name)
    return _row(r)


async def delete_geocoder(conn: AsyncConnection, gid) -> bool:
    """Remove one locator (+ its trigram index, F5 — IF EXISTS, no-op for external types).
    Returns False if `gid` didn't exist (router → 404)."""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM platform.geocoders WHERE id = %s", (gid,))
        deleted = cur.rowcount
    if deleted:
        await drop_locator_index(conn, str(gid))
    await conn.commit()
    return bool(deleted)


async def locator_names_for_layer(conn: AsyncConnection, layer_id: str) -> list[str]:
    """Internal locators referencing `layer_id` — the F6 lifecycle guard release_layer consults
    before deleting a layer (the friendly message in front of the V066 FK belt)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT name FROM platform.geocoders WHERE layer_id = %s ORDER BY name", (layer_id,)
        )
        return [r[0] for r in await cur.fetchall()]


async def reorder_geocoders(conn: AsyncConnection, ids: list, actor_id) -> list[GeocoderRow]:
    """Set each locator's sort_order to its index in `ids` (the new fallback order). Ids not present
    are simply no-ops; ids omitted keep their old order. Returns the full re-read list."""
    async with conn.cursor() as cur:
        for idx, gid in enumerate(ids):
            await cur.execute(
                "UPDATE platform.geocoders SET sort_order = %s, updated_by = %s, updated_at = now() "
                "WHERE id = %s",
                (idx, actor_id, gid),
            )
    await conn.commit()
    return await read_geocoders(conn)


async def active_geocoder_configs(conn: AsyncConnection) -> list[GeocoderConfig]:
    """The configs the /geocode proxy walks: every ENABLED DB row (stored token decrypted into the
    config, in app memory only), UNION the token-bearing env entries whose name isn't shadowed by a
    DB row (back-compat/bootstrap — the UI is the primary path). A row whose ciphertext can't be
    decrypted (F1b: TALUS_ENCRYPTION_KEY missing or wrong for this DB) is SKIPPED with one warning —
    never query a secured service without its credential, never fail the wider /geocode surface.
    geocode.py sorts these by `order` and builds adapters."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT name, type, url, out_sr, extent, sort_order, layer_id, address_fields, "
            "token_ciphertext, locality_fields "
            "FROM platform.geocoders WHERE enabled = true"
        )
        rows = await cur.fetchall()
    db_configs = []
    for r in rows:
        token = None
        if r[8] is not None:
            try:
                token = decrypt_secret(r[8])
            except (SecretKeyUnavailable, SecretDecryptError) as exc:
                log.warning("geocoder_token_unavailable", name=r[0], error=str(exc))
                continue  # F1b: skipped, legibly — the admin screen badges this row
        db_configs.append(
            _row_to_config(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], token=token,
                           locality_fields=r[9])
        )
    db_names = {c.name for c in db_configs}
    env_token = [c for c in settings.geocoders if c.token and c.name not in db_names]
    return db_configs + env_token


async def stored_token(conn: AsyncConnection, gid) -> str | None:
    """Decrypted credential for the admin Test probe (decision §3 F5) — the only read-back path,
    and it stays server-side (the probe result carries status, never the value). None = no token.
    Raises SecretKeyUnavailable / SecretDecryptError for the router to map to a legible result."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT token_ciphertext FROM platform.geocoders WHERE id = %s", (gid,)
        )
        row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return decrypt_secret(row[0])


async def seed_geocoders_from_env_if_unseeded(conn: AsyncConnection) -> int:
    """One-time env GEOCODERS → platform.geocoders backfill (decision §3 F5). Runs at startup
    (Flyway SQL can't read the env var). Guarded by org_settings.geocoders_seeded so it's genuinely
    one-time: the marker is set true EVEN IF env was empty, so an admin who later deletes every
    locator gets an empty list that STAYS empty (env never resurrects it). Only TOKENLESS entries are
    backfilled — token-bearing ones stay env-only via the proxy union. Returns the count inserted."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT geocoders_seeded FROM platform.org_settings WHERE id = true")
        row = await cur.fetchone()
        if row is None or row[0]:
            return 0  # already seeded, or no singleton (defensive — V028 seeds exactly one row)
        seeded = 0
        for cfg in sorted(settings.geocoders, key=lambda c: c.order):
            if cfg.token:
                continue  # token-bearing: stays env-only (F2), not copied into the DB
            if cfg.type == "internal":
                # Internal locators are DB-configured only (decision-own-data-geocoder §4) — an env
                # entry has no layer mapping, and inserting it url-less would trip the V066 shape
                # CHECK and abort the whole seed. Skip it, defensively.
                continue
            await cur.execute(
                "INSERT INTO platform.geocoders (name, type, url, out_sr, extent, sort_order, enabled) "
                "VALUES (%s, %s, %s, %s, %s, %s, true) ON CONFLICT (name) DO NOTHING",
                (
                    cfg.name, cfg.type, cfg.url, cfg.out_sr,
                    Json(cfg.extent) if cfg.extent is not None else None, cfg.order,
                ),
            )
            seeded += cur.rowcount or 0
        await cur.execute("UPDATE platform.org_settings SET geocoders_seeded = true WHERE id = true")
    await conn.commit()
    return seeded
