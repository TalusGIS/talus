"""Owner-defined layer fields — the authoring API over `platform.layer_fields` (V044).

c2 of the data-owner-independence arc (decision-owner-defined-fields.md). Lets the OWNER of a
claimed slot layer define their own typed attribute fields (text/number/date/boolean) from the
portal — the values then live in the slot's `props jsonb`, enforced by the V044 generic trigger.

**Gate (F-author):** authoring (POST/PATCH/DELETE) requires **talus-admin OR the layer's OpenFGA
owner** — reusing `authz.manage_allowed` (the exact key the renderer/release endpoints enforce), so
the owner branch is the same one that gates restyling. Reads (GET) require view-or-manage so a viewer
can render the edit form while an admin/owner always sees the defs. All gates fail closed.

**Injection discipline** mirrors layer_features.py: `layer_id` from the URL NAMES A TABLE only in the
DELETE data-check, and only AFTER the slot regex + registry-ready guards prove it's one of the fixed
`tal_<8hex>` slots; the identifier is then built with `psycopg.sql.Identifier`, never an f-string.
`field_key` is validated to the SAME grammar the DB CHECK enforces (defense in depth → friendly 422
instead of a raw IntegrityError). `vw_*` view layers are excluded by the regex — a view has no
fields of its own (its schema derives from the source slot), so it 404s here.
"""
import base64
import binascii
import csv
import io
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from psycopg import AsyncConnection, errors as pg_errors, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError, field_validator, model_validator

from .. import gpkg_guard
from ..auth import _validated_claims, get_current_user
from ..authz import _has_publish_role, manage_allowed, view_allowed
from ..db import get_db
from ..layer_views import dependent_views_of_field
from ..file_ingest import (
    FAMILY_BY_GEOJSON,
    FAMILY_BY_GPKG_NAME,
    FAMILY_BY_SHPTYPE,
    INGEST_FEATURE_CAP,
    MAX_FILE_BYTES,
    find_csv_coordinates,
    open_shapefile_zip,
)
from ..models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(tags=["layer-fields"])

# Slots ONLY (same guard as layer_features): a view's schema derives from its source slot -> 404.
_SLOT_RE = re.compile(r"^tal_[0-9a-f]{8}$")
# Must match the V044 CHECK constraints exactly (friendly 422 before the DB raises).
_FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# Never shadow a real slot column, and never collide with a name the ESRI door SYNTHESIZES.
# Governing rule (decision-esri-field-name-clash.md §3.3): *reserve what the door synthesizes,
# nothing more* — when a synthesized field joins the ESRI surface, its name joins this set AND the
# `layer_fields_key_reserved` CHECK in the same slice. That rule is what keeps this list from either
# drifting stale or bloating with speculative Esri names (shape/fid/oid are NOT reserved: the door
# doesn't synthesize them, so they cannot clash, and ESRI exports carry them constantly).
# ★ V064 added a physical `objectid` column to every feature table and extended NEITHER this list nor
# the DB CHECK — so until V087 a hand-authored field could shadow it. `globalid` has no column at
# all; it is the door's GlobalID, backed by the uuid `id`. Mirror of the CHECK — keep them in lockstep.
_RESERVED_KEYS = frozenset({
    "id", "geom", "notes", "props", "row_version", "ai_provenance",
    "created_at", "created_by", "updated_at", "updated_by",
    "objectid", "globalid",
})
# The subset of reserved names that arrive as real DATA in ESRI exports, where a refusal would throw
# values away. These are RENAMED at ingest instead (§3.1 — see `_sanitize_key`). Every other reserved
# key keeps the plain "is reserved" refusal: a source column named `geom`/`id` is structure, not an
# attribute, and `notes` deliberately still maps to the built-in notes column at ingest.
_SRC_RENAMED_KEYS = frozenset({"objectid", "globalid"})
_SRC_SUFFIX = "_src"
_DATA_TYPES = frozenset({"text", "number", "date", "boolean", "coded"})
# Numeric subtypes (decision-create-wizard.md §4) — refine a `number` field for enforcement + export
# fidelity. Integer kinds enforce whole-number + bit-range in the V051 trigger; float/double are export-only.
# IMMUTABLE like data_type (set at create / import; the UI's Type dropdown is immutable).
_NUMERIC_SUBTYPES = frozenset({"short_integer", "long_integer", "float", "double"})
_MAX_LABEL_LEN = 200

# Constraint rules (slice 3, decision-constraint-rules.md). Closed enum of default kinds — 'today'
# resolves to CURRENT_DATE in the V048 trigger (date fields only). NOT a formula language.
_DEFAULT_KINDS = frozenset({"constant", "today", "now", "current_user"})  # now/current_user: V104, text only
# Which constraint keys are valid per data_type (mirrors the V048 trigger's per-type branches).
_CONSTRAINT_KEYS: dict[str, frozenset] = {
    "number": frozenset({"min", "max"}),
    "date": frozenset({"min", "max"}),
    "text": frozenset({"minLength", "maxLength", "pattern"}),
}
_MAX_PATTERN_LEN = 200
_MAX_TEXT_LEN = 100_000   # upper bound for length constraints (the trigger casts to int4)
# Reject the obvious catastrophic-backtracking shape: a group containing an unbounded quantifier that is
# itself quantified, e.g. (a+)+ / (\d*)* / (x+)*. The length cap + the DB statement_timeout are the
# remaining backstops (the trigger's `~` runs under it). Pattern is match/reject ONLY — never derivation.
_NESTED_QUANT_RE = re.compile(r"\([^()]*[+*][^()]*\)\s*[*+]")


# --- validation helpers ---------------------------------------------------------


def _validate_label(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("label must not be empty")
    if len(v) > _MAX_LABEL_LEN:
        raise ValueError(f"label must be at most {_MAX_LABEL_LEN} characters")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
        raise ValueError("label must not contain control characters")
    return v


def _validate_default(data_type: str, value: Any) -> None:
    """A default must match its field's type, so the V044 trigger never injects a value it would then
    reject. `None` means 'no default'. Raises ValueError (-> 422)."""
    if value is None:
        return
    if data_type == "text":
        if not isinstance(value, str):
            raise ValueError("default for a text field must be a string")
    elif data_type == "number":
        # bool is a subclass of int — exclude it explicitly.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("default for a number field must be numeric")
    elif data_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError("default for a boolean field must be true or false")
    elif data_type == "date":
        if not isinstance(value, str):
            raise ValueError("default for a date field must be an ISO date string")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("default for a date field must be a valid date (YYYY-MM-DD)")
    elif data_type == "coded":
        # Shape only here; membership (the default must be an active code) is checked in the
        # handler where the DB connection is available (V046 domain).
        if not isinstance(value, str):
            raise ValueError("default for a coded field must be a code (string)")


def _is_int(v: Any) -> bool:
    # bool is a subclass of int — exclude it.
    return isinstance(v, int) and not isinstance(v, bool)


def _validate_pattern(pattern: Any) -> str:
    """Guard a user-authored regex (the 'advanced' pattern + the preset values both pass here): a string,
    length-capped, no obvious catastrophic nesting, Python-compilable. WHOLE-STRING anchored. Returns the
    normalized pattern to STORE. Raises ValueError. NB: this validates the Python `re` dialect; the handler
    additionally validates the POSIX (Postgres) dialect via _assert_pg_pattern — the DB is what enforces."""
    if not isinstance(pattern, str):
        raise ValueError("pattern must be a string")
    if not pattern:
        raise ValueError("pattern must not be empty")
    if len(pattern) > _MAX_PATTERN_LEN:
        raise ValueError(f"pattern must be at most {_MAX_PATTERN_LEN} characters")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in pattern):
        raise ValueError("pattern must not contain control characters")
    if _NESTED_QUANT_RE.search(pattern):
        raise ValueError("pattern has nested quantifiers that risk catastrophic backtracking")
    # Wrap in a NON-CAPTURING group before anchoring, so an alternation like `cat|dog` becomes
    # `^(?:cat|dog)$` (whole-string match) — NOT `^cat|dog$`, which would accept 'catXYZ'. IDEMPOTENT:
    # a value already in this canonical form is left as-is, so re-saving a field never double-wraps.
    anchored = pattern if (pattern.startswith("^(?:") and pattern.endswith(")$")) else f"^(?:{pattern})$"
    try:
        re.compile(anchored)
    except re.error as e:
        raise ValueError(f"pattern is not a valid regular expression: {e}")
    return anchored


def _validate_constraints(data_type: str, constraints: Any) -> dict | None:
    """Validate + normalize the declarative `constraints` jsonb against the field's type (mirrors the V048
    trigger's per-type branches). Strips null-valued keys; returns the cleaned dict to STORE (or None when
    empty / not given). Raises ValueError (-> 422)."""
    if constraints is None:
        return None
    if not isinstance(constraints, dict):
        raise ValueError("constraints must be an object")
    allowed = _CONSTRAINT_KEYS.get(data_type, frozenset())
    clean = {k: v for k, v in constraints.items() if v is not None}
    extra = set(clean) - allowed
    if extra:
        raise ValueError(f"constraints {sorted(extra)} are not valid for a {data_type} field")
    if not clean:
        return None

    if data_type == "number":
        for k in ("min", "max"):
            if k in clean and (isinstance(clean[k], bool) or not isinstance(clean[k], (int, float))):
                raise ValueError(f"{k} must be numeric")
        if "min" in clean and "max" in clean and clean["min"] > clean["max"]:
            raise ValueError("min must be ≤ max")
    elif data_type == "date":
        for k in ("min", "max"):
            if k in clean:
                if not isinstance(clean[k], str):
                    raise ValueError(f"{k} must be an ISO date string (YYYY-MM-DD)")
                try:
                    date.fromisoformat(clean[k])
                except ValueError:
                    raise ValueError(f"{k} must be a valid date (YYYY-MM-DD)")
        if "min" in clean and "max" in clean and date.fromisoformat(clean["min"]) > date.fromisoformat(clean["max"]):
            raise ValueError("min must be on or before max")
    elif data_type == "text":
        for k in ("minLength", "maxLength"):
            if k in clean and (not _is_int(clean[k]) or clean[k] < 0):
                raise ValueError(f"{k} must be a non-negative integer")
            # bound it: the trigger casts to int4, so an absurd value would overflow to a raw error on write.
            if k in clean and clean[k] > _MAX_TEXT_LEN:
                raise ValueError(f"{k} must be at most {_MAX_TEXT_LEN}")
        if "minLength" in clean and "maxLength" in clean and clean["minLength"] > clean["maxLength"]:
            raise ValueError("minLength must be ≤ maxLength")
        if "pattern" in clean:
            clean["pattern"] = _validate_pattern(clean["pattern"])
    return clean


def _validate_default_kind(data_type: str, default_kind: str, default_value: Any) -> None:
    """The dynamic default is a CLOSED token enum. 'today' is date-only and cannot also carry a constant
    value. Raises ValueError (-> 422)."""
    if default_kind not in _DEFAULT_KINDS:
        raise ValueError(f"defaultKind must be one of {sorted(_DEFAULT_KINDS)}")
    if default_kind in ("now", "current_user"):
        # V104 (decision-talus-field-f2.md fork 2): ISO-8601 UTC timestamp / the acting user's display name —
        # text fields only, resolved by the trigger on write; a constant cannot ride alongside.
        if data_type != "text":
            raise ValueError(f"defaultKind '{default_kind}' is only valid for a text field")
        if default_value is not None:
            raise ValueError(f"a '{default_kind}' default cannot also set a constant defaultValue")
        return
    if default_kind == "today":
        if data_type != "date":
            raise ValueError("defaultKind 'today' is only valid for a date field")
        if default_value is not None:
            raise ValueError("a 'today' default cannot also set a constant defaultValue")


# --- request bodies -------------------------------------------------------------


def _validate_contingent_shape(parent_field_key: Any, contingent_values: Any) -> None:
    """Coarse shape of a contingency pair (V077): both present or both absent; the mapping is an
    object of parentCode -> list-of-string child codes. DEEP validation (parent
    exists / is coded / same layer / codes defined / no self-ref or 2-cycle) needs the DB — see
    _validate_contingency in the handlers. Raises ValueError (-> 422)."""
    if (parent_field_key is None) != (contingent_values is None):
        raise ValueError("parentFieldKey and contingentValues must be set together (or both cleared)")
    if contingent_values is None:
        return
    if not isinstance(contingent_values, dict):
        raise ValueError("contingentValues must be an object of parentCode -> [childCode, ...]")
    for pcode, codes in contingent_values.items():
        if not isinstance(pcode, str) or not pcode:
            raise ValueError("contingentValues keys must be non-empty parent codes")
        if not isinstance(codes, list) or not all(isinstance(c, str) and c for c in codes):
            raise ValueError(f"contingentValues['{pcode}'] must be a list of child codes")


class FieldCreate(BaseModel):
    fieldKey: str
    label: str
    dataType: str
    required: bool = False
    defaultValue: Any | None = None
    sortOrder: int = 0
    domainId: str | None = None  # required iff dataType == 'coded' (the bound V046 domain)
    constraints: dict[str, Any] | None = None  # range/length/pattern (slice 3); validated against dataType
    defaultKind: str = "constant"              # 'constant' | 'today' (date only) | 'now' | 'current_user' (text only; V104)
    numericSubtype: str | None = None          # short/long_integer|float|double — only for dataType='number'
    parentFieldKey: str | None = None                       # contingency (V077): the coded sibling parent
    contingentValues: dict[str, list[str]] | None = None    # parentCode -> allowed child codes

    @field_validator("fieldKey")
    @classmethod
    def _vk(cls, v: str) -> str:
        if not _FIELD_KEY_RE.fullmatch(v):
            raise ValueError("fieldKey must be snake_case: a letter then letters/digits/_ (≤63 chars)")
        if v in _RESERVED_KEYS:
            raise ValueError(f"fieldKey '{v}' is reserved")
        return v

    @field_validator("label")
    @classmethod
    def _vl(cls, v: str) -> str:
        return _validate_label(v)

    @field_validator("dataType")
    @classmethod
    def _vt(cls, v: str) -> str:
        if v not in _DATA_TYPES:
            raise ValueError(f"dataType must be one of {sorted(_DATA_TYPES)}")
        return v

    @model_validator(mode="after")
    def _vd(self) -> "FieldCreate":
        is_coded = self.dataType == "coded"
        if is_coded and not self.domainId:
            raise ValueError("a coded field requires domainId")
        if not is_coded and self.domainId:
            raise ValueError("domainId is only valid for a coded field")
        if self.domainId:
            try:
                UUID(self.domainId)
            except (ValueError, TypeError):
                raise ValueError("domainId must be a UUID")
        if self.numericSubtype is not None:
            if self.dataType != "number":
                raise ValueError("numericSubtype is only valid for a number field")
            if self.numericSubtype not in _NUMERIC_SUBTYPES:
                raise ValueError(f"numericSubtype must be one of {sorted(_NUMERIC_SUBTYPES)}")
        _validate_default(self.dataType, self.defaultValue)
        _validate_default_kind(self.dataType, self.defaultKind, self.defaultValue)
        self.constraints = _validate_constraints(self.dataType, self.constraints)  # normalize (anchor pattern, strip nulls)
        _validate_contingent_shape(self.parentFieldKey, self.contingentValues)
        if self.parentFieldKey is not None:
            if self.dataType != "coded":
                raise ValueError("a contingency (parentFieldKey) is only valid on a coded field")
            if self.parentFieldKey == self.fieldKey:
                raise ValueError("a field cannot depend on itself")
        return self


class FieldUpdate(BaseModel):
    """All optional — only provided keys change. fieldKey and dataType are IMMUTABLE (rekey/retype =
    retire + add) so existing `props` values can never be orphaned by a definition change."""
    label: str | None = None
    required: bool | None = None
    defaultValue: Any | None = None
    sortOrder: int | None = None
    active: bool | None = None
    # Validated against the field's EXISTING (immutable) data_type in the handler, where it's known.
    constraints: dict[str, Any] | None = None
    defaultKind: str | None = None
    # Contingency (V077): PATCH BOTH together — both values to set/replace, both nulls to clear. The
    # both-or-neither rule is enforced in the handler on the PROVIDED pair (exclude_unset semantics).
    parentFieldKey: str | None = None
    contingentValues: dict[str, list[str]] | None = None

    @field_validator("label")
    @classmethod
    def _vl(cls, v: str | None) -> str | None:
        return _validate_label(v) if v is not None else v


# --- guards ---------------------------------------------------------------------


async def _validate_slot(conn: AsyncConnection, layer_id: str) -> None:
    """`layer_id` must be a slot token (regex) registered 'ready' — BEFORE it ever names a table.
    404 (not a slot / unknown) or 409 ('layer not ready'). Binds layer_id as a parameter."""
    if not _SLOT_RE.fullmatch(layer_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    async with conn.cursor() as cur:
        await cur.execute("SELECT status FROM platform.layers WHERE layer_id = %s", (layer_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="layer not found")
    if row[0] != "ready":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="layer not ready")


async def _assert_can_manage(claims: dict | None, user: User, layer_id: str) -> None:
    """Authoring gate: talus-admin OR the layer's OpenFGA owner (authz.manage_allowed). Fails closed."""
    if not await manage_allowed(claims, user, layer_id):
        log.info(
            "authz_rejected", capability=f"{layer_id}:fields-author",
            reason="not_admin_not_owner", user_id=str(user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"not authorized to manage fields on {layer_id}",
        )


async def _assert_can_read(claims: dict | None, user: User, layer_id: str) -> None:
    """Read gate: anyone who can view the layer (to render the edit form) OR admin/owner. Fails closed."""
    if await view_allowed(user, layer_id) or await manage_allowed(claims, user, layer_id):
        return
    log.info(
        "authz_rejected", capability=f"{layer_id}:fields-read",
        reason="no_view_access", user_id=str(user.id),
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"not authorized to view {layer_id}",
    )


def _slot_table(layer_id: str) -> sql.Identifier:
    """assets.<layer_id> — ONLY after _validate_slot has passed (proven token), never raw input."""
    return sql.Identifier("assets", layer_id)


async def _validate_coded(conn: AsyncConnection, domain_id: str, default_value: Any) -> None:
    """A coded field must bind an existing domain; if it has a default, the default must be an ACTIVE
    code in that domain (so the trigger never injects a value it would reject). Raises HTTP 422."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM platform.domains WHERE id = %s", (domain_id,))
        if await cur.fetchone() is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="domainId does not reference an existing domain",
            )
    if default_value is not None:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM platform.domain_values WHERE domain_id = %s AND code = %s AND active",
                (domain_id, default_value),
            )
            if await cur.fetchone() is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"default '{default_value}' is not an active code in that domain",
                )


async def _validate_contingency(
    conn: AsyncConnection,
    layer_id: str,
    child_field_key: str,
    child_domain_id: str,
    parent_field_key: str,
    contingent_values: dict[str, list[str]],
) -> None:
    """Deep author-time validation of a contingency pair (V077, decision-contingent-values.md fork 1b).
    The parent must be an existing CODED field on the same layer (single-parent fence: coded-only keeps
    the mapping authorable as picklists); no self-reference (model-checked) and no 2-cycle (A->B, B->A).
    Mapping keys must be codes DEFINED in the parent's domain and child lists codes DEFINED in the child's
    domain — defined means active OR retired (matching the trigger's membership rule, so a mapping can
    keep a legacy pair editable). Raises HTTP 422."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT data_type, domain_id::text, parent_field_key FROM platform.layer_fields "
            "WHERE layer_id = %s AND field_key = %s",
            (layer_id, parent_field_key),
        )
        parent = await cur.fetchone()
    if parent is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"parentFieldKey '{parent_field_key}' is not a field on this layer",
        )
    p_type, p_domain, p_parent = parent
    if p_type != "coded":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"parent field '{parent_field_key}' must be a picklist (coded) field",
        )
    if p_parent == child_field_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{parent_field_key}' already depends on '{child_field_key}' — a two-field cycle is not allowed",
        )

    async def _defined_codes(domain_id: str) -> set[str]:
        async with conn.cursor() as cur:
            await cur.execute("SELECT code FROM platform.domain_values WHERE domain_id = %s", (domain_id,))
            return {r[0] for r in await cur.fetchall()}

    parent_codes = await _defined_codes(p_domain)
    child_codes = await _defined_codes(child_domain_id)
    bad_parents = sorted(set(contingent_values) - parent_codes)
    if bad_parents:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"contingentValues keys not in '{parent_field_key}'s domain: {', '.join(bad_parents)}",
        )
    bad_children = sorted({c for codes in contingent_values.values() for c in codes} - child_codes)
    if bad_children:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"contingentValues contains codes not in this field's domain: {', '.join(bad_children)}",
        )


async def _contingency_dependents(conn: AsyncConnection, layer_id: str, field_key: str) -> list[str]:
    """field_keys of ACTIVE fields whose contingency names `field_key` as parent — used to refuse
    deactivating/deleting a parent out from under its dependents (friendly 409; the V077 FK is the
    DB belt under the delete case)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT field_key FROM platform.layer_fields "
            "WHERE layer_id = %s AND parent_field_key = %s AND active ORDER BY field_key",
            (layer_id, field_key),
        )
        return [r[0] for r in await cur.fetchall()]


async def _assert_pg_pattern(conn: AsyncConnection, pattern: str) -> None:
    """The DB enforces patterns with POSIX ARE (`~`), a DIFFERENT dialect than Python `re` / JS `RegExp`.
    Validate that Postgres can parse the pattern at AUTHOR time — in a SAVEPOINT so a bad one doesn't poison
    the request transaction — otherwise a Python-valid-but-PG-invalid regex (e.g. `(?P<n>…)`) would store
    cleanly and then raise a raw error on EVERY later feature write. Raises HTTP 422."""
    async with conn.cursor() as cur:
        await cur.execute("SAVEPOINT pattern_check")
        try:
            await cur.execute("SELECT %s ~ %s", ("x", pattern))
        except pg_errors.Error:
            await cur.execute("ROLLBACK TO SAVEPOINT pattern_check")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="pattern is not a valid regular expression for the database (POSIX) — try a simpler format",
            )
        await cur.execute("RELEASE SAVEPOINT pattern_check")


_SELECT_COLS = (
    "id::text AS id, field_key, label, data_type, required, default_value, "
    "sort_order, active, domain_id::text AS domain_id, constraints, default_kind, "
    "numeric_subtype, parent_field_key, contingent_values, created_at, updated_at"
)


def _to_camel(row: dict) -> dict:
    return {
        "id": row["id"],
        "fieldKey": row["field_key"],
        "label": row["label"],
        "dataType": row["data_type"],
        "required": row["required"],
        "defaultValue": row["default_value"],
        "sortOrder": row["sort_order"],
        "active": row["active"],
        "domainId": row.get("domain_id"),
        "constraints": row.get("constraints"),
        "defaultKind": row["default_kind"],
        "numericSubtype": row.get("numeric_subtype"),
        "parentFieldKey": row.get("parent_field_key"),
        "contingentValues": row.get("contingent_values"),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


# --- routes ---------------------------------------------------------------------


@router.get(
    "/layers/{layer_id}/custom-fields",
    summary="List a slot layer's owner-defined field definitions",
)
async def list_fields(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _validate_slot(conn, layer_id)
    await _assert_can_read(claims, user, layer_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_SELECT_COLS} FROM platform.layer_fields "
            "WHERE layer_id = %s ORDER BY sort_order, field_key",
            (layer_id,),
        )
        rows = await cur.fetchall()
    return {"layerId": layer_id, "fields": [_to_camel(r) for r in rows]}


@router.post(
    "/layers/{layer_id}/custom-fields",
    status_code=status.HTTP_201_CREATED,
    summary="Define a new field on a slot layer (admin or layer owner)",
)
async def create_field(
    layer_id: str,
    payload: FieldCreate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _validate_slot(conn, layer_id)
    await _assert_can_manage(claims, user, layer_id)
    if payload.dataType == "coded":
        await _validate_coded(conn, payload.domainId, payload.defaultValue)
    if payload.constraints and payload.constraints.get("pattern"):
        await _assert_pg_pattern(conn, payload.constraints["pattern"])
    if payload.parentFieldKey is not None:
        await _validate_contingency(
            conn, layer_id, payload.fieldKey, payload.domainId,
            payload.parentFieldKey, payload.contingentValues,
        )
    actor = str(user.id)
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO platform.layer_fields "
                "(layer_id, field_key, label, data_type, required, default_value, sort_order, "
                " domain_id, constraints, default_kind, numeric_subtype, parent_field_key, "
                " contingent_values, created_by, updated_by) "
                "VALUES (%(layer)s, %(key)s, %(label)s, %(dtype)s, %(req)s, %(def)s, %(sort)s, "
                " %(domain)s, %(constr)s, %(dkind)s, %(subtype)s, %(parent)s, %(cvals)s, "
                " %(actor)s, %(actor)s) "
                f"RETURNING {_SELECT_COLS}",
                {
                    "layer": layer_id, "key": payload.fieldKey, "label": payload.label,
                    "dtype": payload.dataType, "req": payload.required,
                    "def": Jsonb(payload.defaultValue) if payload.defaultValue is not None else None,
                    "sort": payload.sortOrder, "domain": payload.domainId,
                    "constr": Jsonb(payload.constraints) if payload.constraints is not None else None,
                    "dkind": payload.defaultKind, "subtype": payload.numericSubtype,
                    "parent": payload.parentFieldKey,
                    "cvals": Jsonb(payload.contingentValues) if payload.contingentValues is not None else None,
                    "actor": actor,
                },
            )
            row = await cur.fetchone()
        await conn.commit()
    except pg_errors.UniqueViolation:
        await conn.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a field with key '{payload.fieldKey}' already exists on this layer",
        )
    log.info("layer_field_mutation", action="create", layer_id=layer_id,
             field_key=payload.fieldKey, actor=actor)
    return _to_camel(row)


_PATCH_COLS = {  # camel attr -> literal column name (column names are NEVER user-derived)
    "label": "label", "required": "required", "defaultValue": "default_value",
    "sortOrder": "sort_order", "active": "active",
    "constraints": "constraints", "defaultKind": "default_kind",
    "parentFieldKey": "parent_field_key", "contingentValues": "contingent_values",
}


@router.patch(
    "/layers/{layer_id}/custom-fields/{field_id}",
    summary="Edit a field's label/required/default/order/active (admin or layer owner)",
)
async def update_field(
    layer_id: str,
    field_id: UUID,
    payload: FieldUpdate,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _validate_slot(conn, layer_id)
    await _assert_can_manage(claims, user, layer_id)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no fields to update")

    # Validate new default/constraints against the field's EXISTING (immutable) data_type + bound domain.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT data_type, domain_id::text, default_value, default_kind, field_key "
            "FROM platform.layer_fields WHERE id = %s AND layer_id = %s",
            (str(field_id), layer_id),
        )
        existing = await cur.fetchone()
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="field not found")
    e_type, e_domain, e_default, e_kind, e_key = existing
    try:
        if "defaultValue" in fields:
            _validate_default(e_type, fields["defaultValue"])
        # The 'today' default is date-only and excludes a constant value — validate the EFFECTIVE state
        # (incoming value if patched, else the stored one), since either field may be changing alone.
        eff_kind = fields["defaultKind"] if "defaultKind" in fields else e_kind
        eff_value = fields["defaultValue"] if "defaultValue" in fields else e_default
        if "defaultKind" in fields or "defaultValue" in fields:
            _validate_default_kind(e_type, eff_kind, eff_value)
        if "constraints" in fields:
            fields["constraints"] = _validate_constraints(e_type, fields["constraints"])  # normalize
        # Contingency (V077): the pair moves together — a patch naming either key must name both
        # (two values = set/replace, two nulls = clear), so the row can never go half-configured.
        if "parentFieldKey" in fields or "contingentValues" in fields:
            if not ("parentFieldKey" in fields and "contingentValues" in fields):
                raise ValueError("patch parentFieldKey and contingentValues together (both values, or both null to clear)")
            _validate_contingent_shape(fields["parentFieldKey"], fields["contingentValues"])
            if fields["parentFieldKey"] is not None:
                if e_type != "coded":
                    raise ValueError("a contingency (parentFieldKey) is only valid on a coded field")
                if fields["parentFieldKey"] == e_key:
                    raise ValueError("a field cannot depend on itself")
    except ValueError as ex:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(ex))
    if "defaultValue" in fields and e_type == "coded":  # new default must be an active code in the domain
        await _validate_coded(conn, e_domain, fields["defaultValue"])
    if fields.get("constraints") and fields["constraints"].get("pattern"):
        await _assert_pg_pattern(conn, fields["constraints"]["pattern"])
    if fields.get("parentFieldKey") is not None:
        await _validate_contingency(
            conn, layer_id, e_key, e_domain, fields["parentFieldKey"], fields["contingentValues"],
        )
    # Deactivating a field that other fields' contingencies depend on would leave them constraining
    # against a retired parent — refuse with the dependents named (clear the dependencies first).
    if fields.get("active") is False:
        dependents = await _contingency_dependents(conn, layer_id, e_key)
        if dependents:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"field '{e_key}' is the contingency parent of: {', '.join(dependents)} — "
                       "remove those dependencies first",
            )
        # A view def that exposes/filters/joins this key keeps serving it from baked DDL while its
        # next recompile 422s on the now-inactive key — refuse the retirement with the views named
        # (decision-view-props-fields.md fork D; the same friendly-409 shape as the source-delete
        # guard in publisher.release_layer).
        views = await dependent_views_of_field(conn, layer_id, e_key)
        if views:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"field '{e_key}' is used by view layer(s): {', '.join(views)} — "
                       "edit or delete those views first",
            )

    set_clauses: list[str] = []
    params: dict[str, Any] = {"id": str(field_id), "layer": layer_id, "actor": str(user.id)}
    for attr, col in _PATCH_COLS.items():
        if attr in fields:
            val = fields[attr]
            if attr in ("defaultValue", "constraints", "contingentValues"):
                val = Jsonb(val) if val is not None else None
            set_clauses.append(f"{col} = %({attr})s")
            params[attr] = val
    set_clauses += ["updated_by = %(actor)s", "updated_at = now()"]

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"UPDATE platform.layer_fields SET {', '.join(set_clauses)} "
            f"WHERE id = %(id)s AND layer_id = %(layer)s RETURNING {_SELECT_COLS}",
            params,
        )
        row = await cur.fetchone()
    if row is None:  # field deleted between the read above and this UPDATE — 404, not a 500
        await conn.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="field not found")
    await conn.commit()
    log.info("layer_field_mutation", action="update", layer_id=layer_id,
             field_id=str(field_id), actor=str(user.id))
    return _to_camel(row)


@router.delete(
    "/layers/{layer_id}/custom-fields/{field_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Hard-delete a field def — refused if any feature has data for it (admin or layer owner)",
)
async def delete_field(
    layer_id: str,
    field_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> Response:
    await _validate_slot(conn, layer_id)
    await _assert_can_manage(claims, user, layer_id)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT field_key FROM platform.layer_fields WHERE id = %s AND layer_id = %s",
            (str(field_id), layer_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="field not found")
    field_key = row[0]

    # Refuse hard-delete while any feature row carries this key — retire (active=false) instead, so
    # existing values survive. layer_id passed _validate_slot, so _slot_table is a proven-safe id.
    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL("SELECT 1 FROM {tbl} WHERE props ? %(key)s LIMIT 1").format(tbl=_slot_table(layer_id)),
            {"key": field_key},
        )
        has_data = await cur.fetchone() is not None
    if has_data:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"field '{field_key}' has data; retire it (set active=false) instead of deleting",
        )

    # Refuse deleting a contingency parent out from under its dependents (friendly face of the V077
    # FK, which would refuse anyway). Checks ALL dependents, not just active — a retired child's
    # mapping still references this key.
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT field_key FROM platform.layer_fields "
            "WHERE layer_id = %s AND parent_field_key = %s ORDER BY field_key",
            (layer_id, field_key),
        )
        dependents = [r[0] for r in await cur.fetchall()]
    if dependents:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"field '{field_key}' is the contingency parent of: {', '.join(dependents)} — "
                   "remove those dependencies first",
        )

    # Same view-dependency refusal as the deactivate guard above (fork D) — a hard-delete is
    # strictly stronger than a retirement, so it gets at least the same fence.
    views = await dependent_views_of_field(conn, layer_id, field_key)
    if views:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"field '{field_key}' is used by view layer(s): {', '.join(views)} — "
                   "edit or delete those views first",
        )

    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM platform.layer_fields WHERE id = %s AND layer_id = %s",
            (str(field_id), layer_id),
        )
    await conn.commit()
    log.info("layer_field_mutation", action="delete", layer_id=layer_id,
             field_key=field_key, actor=str(user.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- fork 7: author-time impact preview -----------------------------------------


class ConstraintCheck(BaseModel):
    constraints: dict[str, Any]  # the PROPOSED constraints to evaluate against existing data


def _violation_cond(data_type: str, c: dict, params: dict) -> str | None:
    """Build a SQL boolean that is TRUE for an existing row whose value for this field is PRESENT and
    VIOLATES the proposed constraints `c`. Returns None if nothing to check. The field_key + bound values
    are parameters; only the fixed `props` column is referenced, so the fragment is injection-safe (it is
    wrapped with sql.SQL and never .format-ed). Type guards keep legacy non-conforming values from erroring
    a cast (the row is type-valid going forward, but we stay defensive)."""
    sub: list[str] = []
    if data_type == "number":
        guard = "jsonb_typeof(props -> %(vkey)s) = 'number'"
        if "min" in c:
            sub.append("(props ->> %(vkey)s)::numeric < %(vmin)s"); params["vmin"] = c["min"]
        if "max" in c:
            sub.append("(props ->> %(vkey)s)::numeric > %(vmax)s"); params["vmax"] = c["max"]
    elif data_type == "date":
        guard = ("jsonb_typeof(props -> %(vkey)s) = 'string' "
                 "AND (props ->> %(vkey)s) ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'")
        if "min" in c:
            sub.append("(props ->> %(vkey)s)::date < %(vmin)s::date"); params["vmin"] = c["min"]
        if "max" in c:
            sub.append("(props ->> %(vkey)s)::date > %(vmax)s::date"); params["vmax"] = c["max"]
    elif data_type == "text":
        guard = "jsonb_typeof(props -> %(vkey)s) = 'string'"
        if "minLength" in c:
            sub.append("char_length(props ->> %(vkey)s) < %(vminl)s"); params["vminl"] = c["minLength"]
        if "maxLength" in c:
            sub.append("char_length(props ->> %(vkey)s) > %(vmaxl)s"); params["vmaxl"] = c["maxLength"]
        if "pattern" in c:
            sub.append("(props ->> %(vkey)s) !~ %(vpat)s"); params["vpat"] = c["pattern"]
    else:
        return None
    if not sub:
        return None
    return f"(props ? %(vkey)s) AND ({guard}) AND (" + " OR ".join(sub) + ")"


@router.post(
    "/layers/{layer_id}/custom-fields/{field_id}/violations",
    summary="Count existing rows that would violate a proposed constraint (author-time impact preview)",
)
async def count_violations(
    layer_id: str,
    field_id: UUID,
    payload: ConstraintCheck,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Fork 7: constraints are SOFT — a new/tightened rule never retro-rejects legacy rows; it binds only on
    next write. Before saving, the author sees how many existing rows already violate the proposed rule. This
    is read-only (no mutation) and gated to admin/owner like the rest of authoring."""
    await _validate_slot(conn, layer_id)
    await _assert_can_manage(claims, user, layer_id)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT field_key, data_type FROM platform.layer_fields WHERE id = %s AND layer_id = %s",
            (str(field_id), layer_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="field not found")
    field_key, data_type = row

    try:
        cleaned = _validate_constraints(data_type, payload.constraints)  # reuse the author-time guard
    except ValueError as ex:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(ex))
    if cleaned and cleaned.get("pattern"):  # a bad POSIX regex here would error the count query (-> 500)
        await _assert_pg_pattern(conn, cleaned["pattern"])

    params: dict[str, Any] = {"vkey": field_key}
    cond = _violation_cond(data_type, cleaned, params) if cleaned else None
    async with conn.cursor() as cur:
        if cond is None:
            await cur.execute(
                sql.SQL("SELECT 0 AS violating, count(*) AS total FROM {tbl}").format(tbl=_slot_table(layer_id)),
            )
        else:
            await cur.execute(
                sql.SQL("SELECT count(*) FILTER (WHERE {cond}) AS violating, count(*) AS total FROM {tbl}").format(
                    cond=sql.SQL(cond), tbl=_slot_table(layer_id)),
                params,
            )
        violating, total = await cur.fetchone()
    return {"layerId": layer_id, "fieldId": str(field_id), "violating": violating, "total": total}


class ContingencyCheck(BaseModel):
    parentFieldKey: str
    contingentValues: dict[str, list[str]]


@router.post(
    "/layers/{layer_id}/custom-fields/{field_id}/contingency-violations",
    summary="Count existing rows whose parent/child pair a proposed contingency would forbid (impact preview)",
)
async def count_contingency_violations(
    layer_id: str,
    field_id: UUID,
    payload: ContingencyCheck,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The fork-7 pattern for contingent values (V077): the pair rule is SOFT, so existing rows survive
    until either paired value is next edited — before saving a mapping the author sees how many rows
    already hold a now-forbidden pair. A row counts only when the parent value is set AND mapped AND the
    child value is set but not in the allowed list (fork 3: empty/unmapped parent is never a violation).
    Read-only, manage-gated like the rest of authoring."""
    await _validate_slot(conn, layer_id)
    await _assert_can_manage(claims, user, layer_id)

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT field_key, data_type, domain_id::text FROM platform.layer_fields "
            "WHERE id = %s AND layer_id = %s",
            (str(field_id), layer_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="field not found")
    child_key, data_type, domain_id = row
    if data_type != "coded":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="a contingency is only valid on a coded field")
    try:
        _validate_contingent_shape(payload.parentFieldKey, payload.contingentValues)
        if payload.parentFieldKey == child_key:
            raise ValueError("a field cannot depend on itself")
    except ValueError as ex:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(ex))
    await _validate_contingency(conn, layer_id, child_key, domain_id,
                                payload.parentFieldKey, payload.contingentValues)

    # Everything user-derived is a bound parameter; the only interpolated identifier is the proven slot.
    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL(
                "SELECT count(*) FILTER (WHERE "
                "  jsonb_typeof(props -> %(pkey)s) = 'string' "
                "  AND jsonb_typeof(props -> %(ckey)s) = 'string' "
                "  AND (%(map)s::jsonb) ? (props ->> %(pkey)s) "
                "  AND jsonb_typeof((%(map)s::jsonb) -> (props ->> %(pkey)s)) = 'array' "
                "  AND NOT (((%(map)s::jsonb) -> (props ->> %(pkey)s)) ? (props ->> %(ckey)s))"
                ") AS violating, count(*) AS total FROM {tbl}"
            ).format(tbl=_slot_table(layer_id)),
            {"pkey": payload.parentFieldKey, "ckey": child_key,
             "map": Jsonb(payload.contingentValues)},
        )
        violating, total = await cur.fetchone()
    return {"layerId": layer_id, "fieldId": str(field_id), "violating": violating, "total": total}


# --- bulk schema import (decision-schema-import.md) -----------------------------
# Scaffold many fields from a CSV schema. The browser sends the raw csv_text (NOT persisted server-side —
# satisfies "no expectation to keep the file"); the server parses it (stdlib csv), maps columns -> typed
# fields, and dry-runs (commit=false) to preview per-row verdicts, then creates the valid rows (commit=true).
# REUSES FieldCreate + the slice-1/2/3 validators so the bulk path can never diverge from the single-field
# path (mirrors Talus 46's csv_text + _provision_human). CSV columns (header, case/space-insensitive):
#   field_key,label,data_type,required,default,default_today,min,max,min_length,max_length,pattern

_MAX_IMPORT_FIELDS = 200
_IMPORT_REQUIRED_COLS = ("field_key", "label", "data_type")
# Lenient type aliasing so exports from QGIS/ArcGIS/etc. import cleanly. 'coded' maps through but is FLAGGED
# (not created — fork 2). Anything unmapped -> an 'error' verdict.
_TYPE_ALIASES: dict[str, str] = {
    "text": "text", "string": "text", "str": "text", "char": "text", "varchar": "text",
    "number": "number", "int": "number", "integer": "number", "smallinteger": "number",
    "long": "number", "double": "number", "float": "number", "single": "number", "numeric": "number",
    "real": "number", "decimal": "number",
    "tinyint": "number", "smallint": "number", "mediumint": "number", "bigint": "number",  # GeoPackage ints
    "short_integer": "number", "long_integer": "number", "biginteger": "number",          # our subtype names
    "int16": "number", "int32": "number", "int64": "number",
    "date": "date", "datetime": "date",
    "boolean": "boolean", "bool": "boolean",
    "coded": "coded", "domain": "coded", "picklist": "coded", "enum": "coded",
}
# A `number` field may carry a SUBTYPE (decision-create-wizard.md §4) for enforcement + export fidelity. Maps a
# raw type name -> our subtype; None = a generic number (no subtype). Distinguishes SHORT (SMALLINT) from LONG
# (MEDIUMINT/INTEGER) so the gpkg/CSV round-trip preserves which integer width was authored.
_SUBTYPE_ALIASES: dict[str, str] = {
    "short_integer": "short_integer", "smallinteger": "short_integer", "smallint": "short_integer",
    "short": "short_integer", "tinyint": "short_integer", "int16": "short_integer",
    "long_integer": "long_integer", "integer": "long_integer", "int": "long_integer", "long": "long_integer",
    "mediumint": "long_integer", "bigint": "long_integer", "biginteger": "long_integer",
    "int32": "long_integer", "int64": "long_integer",
    "float": "float", "single": "float", "real": "float", "float4": "float",
    "double": "double", "float8": "double", "numeric": "double", "decimal": "double",
}


def _normalize_type(raw: str) -> str | None:
    return _TYPE_ALIASES.get((raw or "").strip().lower())


def _normalize_subtype(raw: str) -> str | None:
    """Raw numeric type name -> our subtype, or None for a generic 'number'. Only meaningful when the field's
    canonical type is 'number' (callers gate on that)."""
    return _SUBTYPE_ALIASES.get((raw or "").strip().lower())


def _first_validation_msg(e: ValidationError) -> str:
    msg = e.errors()[0].get("msg", "invalid field")
    return msg.replace("Value error, ", "").strip()


def _csv_bool(s: str | None) -> bool:
    return (s or "").strip().lower() in {"true", "1", "yes", "y", "t"}


def _csv_num(s: str):  # int if whole, else float; raises ValueError on a non-number (caught -> error verdict)
    s = (s or "").strip()
    try:
        return int(s)
    except ValueError:
        return float(s)


# A "draft" is one candidate field in a format-neutral shape, produced by a per-format ADAPTER (CSV / GeoPackage)
# and consumed by the shared core `_run_field_import`. Keys: field_key, label, data_type (raw, alias-able),
# required, default_value (typed), default_kind, constraints (typed or None), and optional `_error` (a verdict
# the adapter already determined, e.g. a CSV numeric-coercion failure).


def _csv_typed(row: dict[str, str], canon: str) -> tuple[dict | None, Any, str]:
    """Coerce a CSV row's typed columns by the field's canonical type -> (constraints, default_value,
    default_kind). CSV cells are strings; bad numbers raise ValueError (caught by the adapter -> error verdict)."""
    constraints: dict[str, Any] = {}
    if canon == "number":
        if (row.get("min") or "").strip():
            constraints["min"] = _csv_num(row["min"])
        if (row.get("max") or "").strip():
            constraints["max"] = _csv_num(row["max"])
    elif canon == "date":
        if (row.get("min") or "").strip():
            constraints["min"] = row["min"].strip()
        if (row.get("max") or "").strip():
            constraints["max"] = row["max"].strip()
    elif canon == "text":
        if (row.get("min_length") or "").strip():
            constraints["minLength"] = int(row["min_length"])
        if (row.get("max_length") or "").strip():
            constraints["maxLength"] = int(row["max_length"])
        if (row.get("pattern") or "").strip():
            constraints["pattern"] = row["pattern"]
    today = _csv_bool(row.get("default_today"))
    default_value: Any = None
    if not today and (row.get("default") or "").strip():
        d = row["default"].strip()
        default_value = _csv_num(d) if canon == "number" else _csv_bool(d) if canon == "boolean" else d
    return (constraints or None, default_value, "today" if today else "constant")


def _csv_to_drafts(csv_text: str) -> list[dict[str, Any]]:
    """CSV adapter: parse server-side (stdlib csv handles quoting/embedded newlines), normalize the header
    (case/space-insensitive), -> format-neutral drafts. Raises HTTPException for parse/header/size issues."""
    try:
        all_rows = list(csv.reader(io.StringIO(csv_text)))
    except csv.Error as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"could not parse CSV: {e}")
    if not all_rows:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="the CSV is empty")
    header = [h.strip().lower().replace(" ", "_") for h in all_rows[0]]
    missing = [c for c in _IMPORT_REQUIRED_COLS if c not in header]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"CSV is missing required column(s): {', '.join(missing)}",
        )
    data_rows = [dict(zip(header, r)) for r in all_rows[1:] if any((c or "").strip() for c in r)]
    drafts: list[dict[str, Any]] = []  # the _MAX_IMPORT_FIELDS cap is enforced once, in _run_field_import
    for raw in data_rows:
        d: dict[str, Any] = {
            "field_key": (raw.get("field_key") or "").strip(),
            "label": (raw.get("label") or "").strip(),
            "data_type": raw.get("data_type", ""),
            "required": _csv_bool(raw.get("required")),
            "default_value": None, "default_kind": "constant", "constraints": None,
        }
        canon = _normalize_type(d["data_type"])
        d["numeric_subtype"] = _normalize_subtype(d["data_type"]) if canon == "number" else None
        if canon and canon != "coded":
            try:
                d["constraints"], d["default_value"], d["default_kind"] = _csv_typed(raw, canon)
            except ValueError:
                d["_error"] = "min/max/length columns must be numeric"
        drafts.append(d)
    return drafts


# --- GeoPackage adapter (decision-schema-import.md §7) --------------------------------------------
# Upload cap = the shared file_ingest.MAX_FILE_BYTES (one 50 MB number across the whole file flow).
# OGC/SQLite geometry type names — skip these columns even if a malformed .gpkg failed to register them in
# gpkg_geometry_columns (a geometry column must NEVER become an attribute field).
_GPKG_GEOMETRY_TYPES = frozenset({
    "geometry", "point", "linestring", "polygon", "multipoint", "multilinestring", "multipolygon",
    "geomcollection", "geometrycollection", "curve", "surface", "curvepolygon", "compoundcurve",
    "multicurve", "multisurface", "circularstring", "polyhedralsurface", "tin", "triangle",
})


def _sqlite_has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _sanitize_key(name: str) -> str:
    """Derive a valid field_key from a SOURCE column name (machine-derived, unlike CSV where the user
    authors it): lowercase, non-[a-z0-9_] -> _, ensure it starts with a letter, cap at 63. Collisions fall out
    as the existing 'duplicate' verdict (decision-esri-field-name-clash.md §3.6 Q7 — the interactive path
    refuses VISIBLY; only V087 suffixes numerically, because a migration has no human to ask).

    ★ One door for the whole import surface. The three draft builders (GeoPackage, Esri XML,
    shapefile/GeoJSON/CSV-data) AND the bulk data loader's incoming-name -> field_key lookup
    (`layer_features._bulk_ingest`) both derive keys here, and they MUST agree: if the draft says
    `objectid_src` while the loader still looks up `objectid`, that column's VALUES vanish into
    `unmatched_columns` — a fresh silent drop inside the slice that exists to close one (§3.6 Q6).
    Keeping both sides on this function is the contract.

    ★ The rename (§3.1): a source `OBJECTID`/`GlobalID` column lands as `objectid_src`/`globalid_src`.
    Serving both `OBJECTID` and `objectid` from the ESRI door makes OGR refuse the duplicate field, so
    `QgsOfflineEditing` aborts the feature copy AND STILL REPORTS SUCCESS — an empty GeoPackage nobody
    notices until the crew is in the field. Fix the data, not the view: the door keeps serving exactly
    what the data says. §3.1's truncation rule needs no code here — both renamed names are 8 chars, so
    `<key>_src` is 12 and cannot approach the 63 cap.
    """
    k = _slugify_key(name)
    return k + _SRC_SUFFIX if k in _SRC_RENAMED_KEYS else k


def _slugify_key(name: str) -> str:
    """The pure slug half of `_sanitize_key`, WITHOUT §3.1's reserved-name rename — so `_renamed_from`
    can ask "would this source column have landed on a reserved name?" without re-deriving the rule."""
    k = re.sub(r"[^a-z0-9_]", "_", (name or "").strip().lower())
    if not k or not k[0].isalpha():
        k = "f_" + k
    return k[:63]


def _renamed_from(name: str) -> str | None:
    """The source column name IF §3.1's rename fired for it, else None. Draft builders hang this on the
    draft as `_renamed_from`; the import preview turns it into a visible `renamed` row rather than
    letting a renamed field pass as an anonymous green `ok` (§3.6 Q8 — the rename must be legible
    BEFORE commit, and legible in the summary counts, not buried among forty other rows)."""
    return name if _slugify_key(name) in _SRC_RENAMED_KEYS else None


def _geopackage_to_drafts(data: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """GeoPackage adapter: read the .gpkg (SQLite) schema fully IN MEMORY (sqlite3.deserialize — never a temp
    file) -> (format-neutral drafts, seed metadata). Single importable table (v1); skips the geometry column +
    the integer PK; reads the optional Schema extension for labels + range constraints; flags enum columns as
    coded. The metadata (geometry family / srid / feature count — decision-add-from-file.md §4.1) seeds the
    add-from-file wizard; the layer-bound schema import ignores it. Raises HTTPException for not-a-gpkg /
    multi-table / empty."""
    con = sqlite3.connect(":memory:")
    try:
        try:
            con.deserialize(data)
            con.execute("PRAGMA schema_version")  # forces a read -> raises if not a real SQLite db
        except sqlite3.DatabaseError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="not a readable GeoPackage (SQLite) file")
        gpkg_guard.guard(con, gpkg_guard.PREVIEW_BUDGET_OPS)  # read-only, untrusting, budgeted (K1)
        if not _sqlite_has_table(con, "gpkg_contents"):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="not a GeoPackage (no gpkg_contents table)")
        gpkg_guard.require_plain_table(con, "gpkg_contents")
        names = [r[0] for r in con.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type IN ('features','attributes') ORDER BY table_name")]
        if not names:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="the GeoPackage has no feature or attribute tables")
        if len(names) > 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"this GeoPackage has multiple tables ({', '.join(names)}) — v1 imports a single-table GeoPackage",
            )
        tbl = names[0]
        gpkg_guard.require_plain_table(con, tbl)  # a view / virtual table / generated column is refused (K1)
        q = '"' + tbl.replace('"', '""') + '"'  # identifier-quote the (file-derived) table name

        geomcols: set[str] = set()
        family: str | None = None
        srid: int | None = None
        if _sqlite_has_table(con, "gpkg_geometry_columns"):
            gpkg_guard.require_plain_table(con, "gpkg_geometry_columns")
            try:
                gc_rows = con.execute(
                    "SELECT column_name, geometry_type_name, srs_id FROM gpkg_geometry_columns WHERE table_name=?",
                    (tbl,)).fetchall()
            except sqlite3.OperationalError:
                # A minimal gpkg_geometry_columns (e.g. our own schema-only export) lacks the spec columns —
                # the geometry-column SET still matters for field skipping; family/srid just stay unknown.
                gc_rows = [(r[0], None, None) for r in con.execute(
                    "SELECT column_name FROM gpkg_geometry_columns WHERE table_name=?", (tbl,))]
            geomcols = {r[0] for r in gc_rows}
            if gc_rows:
                family = FAMILY_BY_GPKG_NAME.get((gc_rows[0][1] or "").lower())  # generic 'geometry' → None
                srid = int(gc_rows[0][2]) if gc_rows[0][2] is not None and int(gc_rows[0][2]) > 0 else None

        # Optional Schema extension: friendly labels + per-column constraint name.
        labels: dict[str, str] = {}
        col_constraint: dict[str, str] = {}
        if _sqlite_has_table(con, "gpkg_data_columns"):
            gpkg_guard.require_plain_table(con, "gpkg_data_columns")
            for r in con.execute(
                "SELECT column_name, title, name, constraint_name FROM gpkg_data_columns WHERE table_name=?", (tbl,)):
                if r[1] or r[2]:
                    labels[r[0]] = r[1] or r[2]
                if r[3]:
                    col_constraint[r[0]] = r[3]
        constraint_defs: dict[str, tuple] = {}
        if _sqlite_has_table(con, "gpkg_data_column_constraints"):
            gpkg_guard.require_plain_table(con, "gpkg_data_column_constraints")
            for r in con.execute(
                "SELECT constraint_name, constraint_type, min, max FROM gpkg_data_column_constraints"):
                constraint_defs.setdefault(r[0], (r[1], r[2], r[3]))  # first row per name: (type, min, max)

        drafts: list[dict[str, Any]] = []
        for info in con.execute(f"PRAGMA table_info({q})").fetchall():
            col, sqltype, notnull, pk = info[1], info[2], info[3], info[5]
            base = re.sub(r"\(.*\)", "", sqltype or "").strip().lower()  # strip TEXT(50) etc.
            if col in geomcols or base in _GPKG_GEOMETRY_TYPES:
                continue  # geometry column (registered OR detected by type name) — never an attribute field
            canon = _normalize_type(base)
            if pk and canon == "number":
                continue  # the integer primary key (fid / OID) — a system column, not an attribute
            data_type = sqltype or ""
            constraints: dict[str, Any] | None = None
            cname = col_constraint.get(col)
            if cname and cname in constraint_defs:
                ctype, cmin, cmax = constraint_defs[cname]
                if ctype == "enum":
                    data_type = "coded"  # flag for manual domain binding (fork 2); the core emits 'coded'
                elif ctype == "range" and canon == "number":
                    c: dict[str, Any] = {}
                    if cmin is not None:
                        c["min"] = cmin
                    if cmax is not None:
                        c["max"] = cmax
                    constraints = c or None
            else:
                data_type = canon or data_type  # let the core's _normalize_type decide / error
            drafts.append({
                "field_key": _sanitize_key(col),
                "_renamed_from": _renamed_from(col),
                "label": labels.get(col) or col,
                "data_type": "coded" if data_type == "coded" else (canon or sqltype or ""),
                "required": bool(notnull),
                "default_value": None, "default_kind": "constant", "constraints": constraints,
                # subtype from the ORIGINAL sqlite type (SMALLINT/MEDIUMINT/FLOAT/DOUBLE) — canon has collapsed
                # it to 'number'. None for coded / non-numeric.
                "numeric_subtype": _normalize_subtype(base) if (canon == "number" and data_type != "coded") else None,
            })
        try:
            count: int | None = con.execute(f"SELECT count(*) FROM {q}").fetchone()[0]
        except sqlite3.Error:
            count = None
        meta = {"geometry": family, "geometryCounts": ({family: count} if family and count is not None else {}),
                "featureCount": count, "detectedSrid": srid}
        return drafts, meta
    except gpkg_guard.UnsafeGeoPackage as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except sqlite3.OperationalError as exc:
        if gpkg_guard.is_budget_stop(exc):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=gpkg_guard.TOO_COSTLY)
        raise
    finally:
        con.close()


# --- Esri XML Workspace adapter (decision-schema-import.md §8) ------------------------------------
_MAX_XML_BYTES = 100 * 1024 * 1024  # a schema workspace XML can be large; cap the upload
# esriFieldType* -> our type. Anything NOT here (OID / Geometry / Blob / GlobalID / GUID / Raster / XML) is a
# system/unsupported column and is SILENTLY skipped (not an error verdict).
_ESRI_TYPE_MAP: dict[str, str] = {
    "esriFieldTypeString": "text",
    "esriFieldTypeInteger": "number", "esriFieldTypeSmallInteger": "number",
    "esriFieldTypeBigInteger": "number", "esriFieldTypeDouble": "number", "esriFieldTypeSingle": "number",
    "esriFieldTypeDate": "date",
}
# esriFieldType* -> our numeric subtype (BigInteger has no 64-bit subtype here -> long).
_ESRI_SUBTYPE_MAP: dict[str, str] = {
    "esriFieldTypeSmallInteger": "short_integer", "esriFieldTypeInteger": "long_integer",
    "esriFieldTypeBigInteger": "long_integer", "esriFieldTypeSingle": "float", "esriFieldTypeDouble": "double",
}
# geometry-derived / system field NAMES to skip (Shape_Length/Area are Double, so type-skip misses them).
_ESRI_SKIP_NAME_RE = re.compile(r"^(shape_|shape$|objectid|globalid|fid$|oid$)", re.IGNORECASE)
_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"


def _xln(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag  # strip the {namespace}


def _xtype(el: ET.Element) -> str:
    return (el.get(_XSI_TYPE) or "").split(":")[-1]  # 'esri:DEFeatureClass' -> 'DEFeatureClass'


def _xchild(el: ET.Element, name: str):
    for c in el:
        if _xln(c.tag) == name:
            return c
    return None


def _xtext(el, name: str) -> str | None:
    if el is None:
        return None
    c = _xchild(el, name)
    return (c.text or "").strip() if c is not None and c.text is not None else None


def _esri_parse(data: bytes) -> ET.Element:
    """Parse an Esri Workspace XML safely with stdlib xml.etree. REJECTS any DOCTYPE/ENTITY first (stdlib is
    otherwise vulnerable to billion-laughs / XXE) — legit Esri workspace XML has neither. Raises HTTP 422."""
    low = data.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="XML with a DOCTYPE or entities is not allowed")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"could not parse the XML: {e}")
    if _xln(root.tag) != "Workspace":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="not an Esri Workspace XML (root is not <Workspace>)")
    return root


def _esri_domain_kinds(root: ET.Element) -> dict[str, tuple]:
    """{name: ('coded', None, None) | ('range', min, max)}. Esri repeats each domain's FULL definition under
    every field that references it, so the same <Domain> name appears many times — FIRST occurrence wins
    (`setdefault`): the workspace-level <Domains> block precedes all DataElements in document order, so the
    first copy is the authoritative definition (a later field-level copy can't clobber its min/max)."""
    kinds: dict[str, tuple] = {}
    for d in root.iter():
        if _xln(d.tag) != "Domain":
            continue
        name = _xtext(d, "DomainName")
        if not name:
            continue
        if _xtype(d) == "RangeDomain":
            kinds.setdefault(name, ("range", _xtext(d, "MinValue"), _xtext(d, "MaxValue")))
        else:  # CodedValueDomain (or any other) -> coded
            kinds.setdefault(name, ("coded", None, None))
    return kinds


def _esri_tables(root: ET.Element) -> list[tuple]:
    """Importable feature classes / tables -> [(name, DataElement, field_count)]."""
    out: list[tuple] = []
    for de in root.iter():
        if _xln(de.tag) != "DataElement" or _xtype(de) not in ("DEFeatureClass", "DETable"):
            continue
        name = _xtext(de, "Name") or "?"
        n = sum(1 for f in de.iter() if _xln(f.tag) == "Field")
        out.append((name, de, n))
    return out


def _esri_table_drafts(de: ET.Element, domain_kinds: dict[str, tuple]) -> list[dict[str, Any]]:
    """One feature class/table's <Field>s -> format-neutral drafts (skips system fields; AliasName -> label;
    IsNullable=false -> required; String Length -> maxLength; a coded domain -> flagged coded; a range domain
    -> min/max)."""
    drafts: list[dict[str, Any]] = []
    for f in de.iter():
        if _xln(f.tag) != "Field":
            continue
        name = _xtext(f, "Name")
        if not name or _ESRI_SKIP_NAME_RE.match(name):
            continue
        esri_type = _xtext(f, "Type") or ""
        canon = _ESRI_TYPE_MAP.get(esri_type)
        if canon is None:
            continue  # OID / Geometry / Blob / GlobalID / ... — system or unsupported, skip silently
        data_type = canon
        subtype = _ESRI_SUBTYPE_MAP.get(esri_type)  # short/long/float/double; None for text/date
        constraints: dict[str, Any] | None = None
        dom_name = _xtext(_xchild(f, "Domain"), "DomainName")
        if dom_name and dom_name in domain_kinds:
            kind = domain_kinds[dom_name]
            if kind[0] == "coded":
                data_type = "coded"  # flag for manual binding (fork 2 / §8); the core emits 'coded'
            elif kind[0] == "range" and canon == "number":
                c: dict[str, Any] = {}
                for key, raw in (("min", kind[1]), ("max", kind[2])):
                    if raw is not None:
                        try:
                            c[key] = _csv_num(raw)
                        except ValueError:
                            pass
                constraints = c or None
        elif canon == "text":
            length = _xtext(f, "Length")
            if length and length.isdigit() and int(length) > 0:
                constraints = {"maxLength": int(length)}
        drafts.append({
            "field_key": _sanitize_key(name),
            "_renamed_from": _renamed_from(name),
            "label": _xtext(f, "AliasName") or name,
            "data_type": data_type,
            "required": (_xtext(f, "IsNullable") or "true").lower() in ("false", "0"),
            "default_value": None, "default_kind": "constant", "constraints": constraints,
            "numeric_subtype": subtype if data_type == "number" else None,  # dropped if a domain made it coded
        })
    return drafts


# --- shared import core ---------------------------------------------------------------------------


async def _run_field_import(
    conn: AsyncConnection, layer_id: str, drafts: list[dict[str, Any]], commit: bool, actor: str,
) -> dict[str, Any]:
    """The validate -> dry-run -> commit core, shared by every format adapter (CSV, GeoPackage, …) so the bulk
    path can NEVER diverge from the single-field path: each draft is run through FieldCreate + the slice-1/2/3
    validators. commit=false writes nothing; commit=true creates the 'ok' rows in one transaction."""
    # The size cap lives here (the shared core), so it protects EVERY adapter — CSV, GeoPackage, future ones.
    if len(drafts) > _MAX_IMPORT_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"too many fields in one import (max {_MAX_IMPORT_FIELDS})",
        )
    async with conn.cursor() as cur:
        await cur.execute("SELECT field_key FROM platform.layer_fields WHERE layer_id = %s", (layer_id,))
        existing = {r[0] for r in await cur.fetchall()}
        await cur.execute(
            "SELECT COALESCE(max(sort_order), 0) FROM platform.layer_fields WHERE layer_id = %s", (layer_id,))
        next_order = (await cur.fetchone())[0]

    rows: list[dict[str, Any]] = []
    valid: list[FieldCreate] = []
    seen: set[str] = set()
    for d in drafts:
        fkey = d.get("field_key", "")
        if d.get("_error"):
            rows.append({"fieldKey": fkey, "status": "error", "reason": d["_error"]})
            continue
        canon = _normalize_type(d.get("data_type", ""))
        if canon is None:
            rows.append({"fieldKey": fkey, "status": "error", "reason": f"unknown data type '{d.get('data_type', '')}'"})
            continue
        if canon == "coded":
            rows.append({"fieldKey": fkey, "status": "coded",
                         "reason": "picklist field — add it and bind a domain after import"})
            continue
        try:
            fc = FieldCreate(
                fieldKey=fkey, label=d.get("label", ""), dataType=canon, required=bool(d.get("required")),
                defaultValue=d.get("default_value"), defaultKind=d.get("default_kind", "constant"),
                constraints=d.get("constraints"), numericSubtype=d.get("numeric_subtype"),
            )
        except ValidationError as e:
            rows.append({"fieldKey": fkey, "status": "error", "reason": _first_validation_msg(e)})
            continue
        if fc.fieldKey in existing or fc.fieldKey in seen:
            rows.append({"fieldKey": fc.fieldKey, "status": "duplicate", "reason": "a field with this key already exists"})
            continue
        if fc.constraints and fc.constraints.get("pattern"):
            try:
                await _assert_pg_pattern(conn, fc.constraints["pattern"])
            except HTTPException as he:
                rows.append({"fieldKey": fc.fieldKey, "status": "error", "reason": str(he.detail)})
                continue
        _rn = d.get("_renamed_from")
        _reason = (f"renamed — '{_rn}' is reserved on the ESRI door; imported as {fc.fieldKey}"
                   if _rn else None)
        seen.add(fc.fieldKey)
        valid.append(fc)
        rows.append({"fieldKey": fc.fieldKey, "status": "renamed" if _rn else "ok", "reason": _reason})

    if commit and valid:
        order = next_order
        # 'renamed' is an ok row that carries an explanation (§3.6 Q8) — it commits like any other.
        ok_status = {r["fieldKey"]: r for r in rows if r["status"] in ("ok", "renamed")}
        async with conn.cursor() as cur:
            for fc in valid:
                order += 10
                try:
                    await cur.execute(
                        "INSERT INTO platform.layer_fields "
                        "(layer_id, field_key, label, data_type, required, default_value, sort_order, "
                        " constraints, default_kind, numeric_subtype, created_by, updated_by) "
                        "VALUES (%(layer)s, %(key)s, %(label)s, %(dtype)s, %(req)s, %(def)s, %(sort)s, "
                        " %(constr)s, %(dkind)s, %(subtype)s, %(actor)s, %(actor)s)",
                        {
                            "layer": layer_id, "key": fc.fieldKey, "label": fc.label, "dtype": fc.dataType,
                            "req": fc.required,
                            "def": Jsonb(fc.defaultValue) if fc.defaultValue is not None else None,
                            "sort": order,
                            "constr": Jsonb(fc.constraints) if fc.constraints is not None else None,
                            "dkind": fc.defaultKind, "subtype": fc.numericSubtype, "actor": actor,
                        },
                    )
                    ok_status[fc.fieldKey]["status"] = "created"
                except pg_errors.UniqueViolation:
                    await conn.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"field '{fc.fieldKey}' was created concurrently — re-run the import",
                    )
        await conn.commit()
        log.info("layer_field_mutation", action="import", layer_id=layer_id, created=len(valid), actor=actor)

    summary = {"ok": 0, "created": 0, "renamed": 0, "error": 0, "coded": 0, "duplicate": 0}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {"layerId": layer_id, "committed": bool(commit), "rows": rows, "summary": summary}


class ImportRequest(BaseModel):
    csv_text: str
    commit: bool = False  # False => dry-run preview (no writes); True => create the valid rows


@router.post(
    "/layers/{layer_id}/custom-fields/import",
    summary="Bulk-create custom fields from a CSV schema (commit=false previews; commit=true creates)",
)
async def import_fields(
    layer_id: str,
    payload: ImportRequest,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _validate_slot(conn, layer_id)
    await _assert_can_manage(claims, user, layer_id)
    drafts = _csv_to_drafts(payload.csv_text)
    return await _run_field_import(conn, layer_id, drafts, payload.commit, str(user.id))


class GeopackageImportRequest(BaseModel):
    # The .gpkg bytes, base64-encoded — keeps a JSON transport (no python-multipart dependency) for a small
    # schema file; decoded + parsed in memory, never written to disk.
    gpkg_base64: str
    commit: bool = False


@router.post(
    "/layers/{layer_id}/custom-fields/import-geopackage",
    summary="Bulk-create custom fields from a GeoPackage schema (base64 body; commit=false previews)",
)
async def import_geopackage(
    layer_id: str,
    payload: GeopackageImportRequest,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _validate_slot(conn, layer_id)
    await _assert_can_manage(claims, user, layer_id)
    try:
        data = base64.b64decode(payload.gpkg_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="gpkg_base64 is not valid base64")
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="the uploaded file is empty")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"GeoPackage too large (max {MAX_FILE_BYTES // (1024 * 1024)} MB)")
    drafts, _meta = await run_in_threadpool(_geopackage_to_drafts, data)  # no wizard to seed here; off the loop (K1)
    return await _run_field_import(conn, layer_id, drafts, commit=payload.commit, actor=str(user.id))


class EsriXmlImportRequest(BaseModel):
    xml_base64: str           # the Workspace XML bytes, base64-encoded (JSON transport, no dependency)
    table: str | None = None  # which feature class/table to import (a GDB is multi-table); see the picker flow
    commit: bool = False


@router.post(
    "/layers/{layer_id}/custom-fields/import-esri-xml",
    summary="Bulk-create custom fields from an Esri XML Workspace (base64; picks a feature class; previews/commits)",
)
async def import_esri_xml(
    layer_id: str,
    payload: EsriXmlImportRequest,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _validate_slot(conn, layer_id)
    await _assert_can_manage(claims, user, layer_id)
    try:
        data = base64.b64decode(payload.xml_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="xml_base64 is not valid base64")
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="the uploaded file is empty")
    if len(data) > _MAX_XML_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"XML too large (max {_MAX_XML_BYTES // (1024 * 1024)} MB)")
    root = _esri_parse(data)
    tables = _esri_tables(root)
    if not tables:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="no feature classes or tables in the workspace")
    if payload.table is None:
        if len(tables) > 1:  # a GDB is multi-table -> ask the caller to pick one (the picker flow)
            return {"layerId": layer_id, "needsTable": True,
                    "tables": [{"name": n, "fieldCount": c} for (n, _de, c) in tables]}
        chosen = tables[0]
    else:
        chosen = next((t for t in tables if t[0] == payload.table), None)
        if chosen is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"feature class '{payload.table}' not found; available: {', '.join(t[0] for t in tables)}",
            )
    drafts = _esri_table_drafts(chosen[1], _esri_domain_kinds(root))
    result = await _run_field_import(conn, layer_id, drafts, commit=payload.commit, actor=str(user.id))
    result["table"] = chosen[0]
    return result


# --- Add-from-file DATA-file adapters (decision-add-from-file.md §4.1) -----------------------------
# The map panel's "Add from file": the SAME file the user picked pre-fills the wizard here (drafted
# fields + geometry family + CRS + counts) and is then ingested by /features/import after Finish.
# Readers are shared with the ingest via file_ingest — one reader, both endpoints, no drift. Unlike the
# schema formats above (which parse schema DEFINITIONS), these parse DATA files: fields come from the
# dbf descriptors / a properties scan / the CSV header, so types are declared (dbf) or value-sniffed.

_SNIFF_SAMPLE = 1000  # per-column value-sniff sample size (typing only; geometry/row counts scan everything)


# A Talus long integer is 32-bit (the field validator's range). A whole number outside it must not
# type its column long_integer, or the validator refuses every such row.
_INT32_MIN, _INT32_MAX = -2147483648, 2147483647


def _new_sniff() -> dict[str, Any]:
    return {"boolean": 0, "number": 0, "date": 0, "text": 0, "n": 0, "all_int": True, "fits32": True}


def _sniff_json_value(a: dict[str, Any], v: Any) -> None:
    """Classify one GeoJSON property value into the accumulator (None values don't count).

    The number SHAPE (whole? fits 32 bits?) is tracked past the sample cap: it can only widen
    long_integer to double, never change the column's kind, and a single decimal or oversized value
    beyond the sample would otherwise be refused by the validator at insert time."""
    if v is None:
        return
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if not isinstance(v, int):
            a["all_int"] = False
        elif not _INT32_MIN <= v <= _INT32_MAX:
            a["fits32"] = False
    if a["n"] >= _SNIFF_SAMPLE:
        return
    a["n"] += 1
    if isinstance(v, bool):
        a["boolean"] += 1
    elif isinstance(v, (int, float)):
        a["number"] += 1
    elif isinstance(v, str):
        if len(v) == 10 and _is_iso_date(v):
            a["date"] += 1
        else:
            a["text"] += 1
    else:
        a["text"] += 1  # nested list/dict — no portable column type; text is the honest fallback


def _is_iso_date(s: str) -> bool:
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _sniff_csv_cell(a: dict[str, Any], s: str) -> None:
    """Classify one CSV cell (a string) into the accumulator (blank cells don't count). Numbers are checked
    FIRST so '20230101' stays a number and '0'/'1' stay numbers (not booleans); dates are strict ISO
    YYYY-MM-DD only (a datetime or a local format sniffs as text — safer than a wrong 'date')."""
    s = s.strip()
    if not s or a["n"] >= _SNIFF_SAMPLE:
        return
    a["n"] += 1
    try:
        if not _INT32_MIN <= int(s) <= _INT32_MAX:
            a["fits32"] = False
        a["number"] += 1
        return
    except ValueError:
        pass
    try:
        float(s)
        a["number"] += 1
        a["all_int"] = False
        return
    except ValueError:
        pass
    if s.lower() in {"true", "false", "yes", "no", "t", "f", "y", "n"}:
        a["boolean"] += 1
        return
    if len(s) == 10 and _is_iso_date(s):
        a["date"] += 1
        return
    a["text"] += 1


def _sniffed_type(a: dict[str, Any]) -> tuple[str, str | None]:
    """Accumulator → (data_type, numeric_subtype). One kind across every sampled value wins; mixed, or
    nothing sampled (all null/blank), falls back to text — the type a user can always widen from."""
    kinds = [k for k in ("boolean", "number", "date", "text") if a[k]]
    if a["n"] == 0 or len(kinds) != 1:
        return "text", None
    if kinds[0] == "number":
        return "number", ("long_integer" if a["all_int"] and a["fits32"] else "double")
    return kinds[0], None


def _data_draft(name: str, data_type: str, subtype: str | None, *,
                required: bool = False, constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    """One data-file column → a format-neutral draft (keys machine-derived, like the GeoPackage adapter)."""
    return {"field_key": _sanitize_key(name), "_renamed_from": _renamed_from(name),
            "label": name, "data_type": data_type, "required": required,
            "default_value": None, "default_kind": "constant", "constraints": constraints,
            "numeric_subtype": subtype}


def _shapefile_to_drafts(data: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Shapefile adapter → (drafts, seed metadata). Fields typed straight from the dbf descriptors — richer
    than value sniffing (C→text with the dbf width as maxLength, N/F→number with a width-derived subtype,
    L→boolean, D→date; memo/blob skipped silently); geometry family from the .shp header; srid from the .prj
    (None ⇒ the wizard asks the user to declare the CRS, same as the ingest's source_srid path)."""
    reader, detected = open_shapefile_zip(data)
    drafts: list[dict[str, Any]] = []
    for f in reader.fields[1:]:  # fields[0] is the pyshp DeletionFlag
        name, ftype = f[0], (f[1] or "").upper()
        size, decimal = int(f[2] or 0), int(f[3] or 0)
        if ftype == "C":
            drafts.append(_data_draft(name, "text", None,
                                      constraints={"maxLength": size} if size > 0 else None))
        elif ftype in ("N", "F"):
            sub = ("double" if (decimal > 0 or ftype == "F")
                   else "short_integer" if 0 < size <= 4 else "long_integer")
            drafts.append(_data_draft(name, "number", sub))
        elif ftype == "L":
            drafts.append(_data_draft(name, "boolean", None))
        elif ftype == "D":
            drafts.append(_data_draft(name, "date", None))
        # anything else (memo/blob) has no portable mapping — skipped silently, like Esri system fields
    family = FAMILY_BY_SHPTYPE.get(reader.shapeType)
    try:
        count: int | None = len(reader)
    except Exception:
        count = None  # no .shx and an unreadable dbf header — the ingest still counts per-feature
    meta = {"geometry": family, "geometryCounts": ({family: count} if family and count is not None else {}),
            "featureCount": count, "detectedSrid": detected}
    return drafts, meta


def _geojson_to_drafts(data: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """GeoJSON adapter → (drafts, seed metadata). Fields value-sniffed from a properties scan (first-seen key
    order, sampled per key); geometry families COUNTED across all features — the wizard preselects the
    dominant one and the ingest later skips mismatched rows per-row, as today. CRS fixed 4326 (RFC 7946)."""
    try:
        doc = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="not valid JSON")
    if isinstance(doc, dict) and doc.get("type") == "FeatureCollection":
        feats = doc.get("features") or []
    elif isinstance(doc, dict) and doc.get("type") == "Feature":
        feats = [doc]
    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="expected a GeoJSON FeatureCollection or Feature")
    if not isinstance(feats, list):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="features must be a list")
    counts: dict[str, int] = {}
    acc: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        geom = f.get("geometry")
        family = FAMILY_BY_GEOJSON.get(geom.get("type")) if isinstance(geom, dict) else None
        if family:
            counts[family] = counts.get(family, 0) + 1
        props = f.get("properties")
        if isinstance(props, dict):
            for k, v in props.items():
                if not isinstance(k, str) or not k.strip():
                    continue
                if k not in acc:
                    acc[k] = _new_sniff()
                    order.append(k)
                _sniff_json_value(acc[k], v)
    drafts = [_data_draft(k, *_sniffed_type(acc[k])) for k in order]
    dominant = max(counts, key=counts.__getitem__) if counts else None
    meta = {"geometry": dominant, "geometryCounts": counts, "featureCount": len(feats), "detectedSrid": 4326}
    return drafts, meta


def _csvdata_to_drafts(text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Data-CSV adapter → (drafts, seed metadata) — NOT the schema-CSV format above (that one parses
    field-definition rows; this parses a data table). Fields value-sniffed per column; the coordinate-column
    candidates are DETECTED (shared logic with the ingest) but every column still gets a draft — the wizard
    drops whichever pair the user confirms as x/y at Finish, so a manual override needs no second preview.
    Geometry is point by nature; srid 4326 only when the names are geographic (lon/lat), else declared."""
    if text.startswith("\ufeff"):
        text = text[1:]  # Excel/ArcGIS CSVs carry a UTF-8 BOM — without it stripped the first header is corrupt
    reader = csv.DictReader(io.StringIO(text))
    headers = [h for h in (reader.fieldnames or []) if h is not None and h.strip()]
    if not headers:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="CSV has no header row")
    xcol, ycol, detected = find_csv_coordinates(headers)
    acc = {h: _new_sniff() for h in headers}
    count = 0
    try:
        for row in reader:
            count += 1
            for h in headers:
                v = row.get(h)
                if isinstance(v, str):
                    _sniff_csv_cell(acc[h], v)
    except csv.Error as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"could not parse CSV: {e}")
    drafts = [_data_draft(h, *_sniffed_type(acc[h])) for h in headers]
    meta = {"geometry": "point", "geometryCounts": {"point": count}, "featureCount": count,
            "detectedSrid": detected, "columns": headers, "xColumn": xcol, "yColumn": ycol}
    return drafts, meta


# --- layer-less import PREVIEW (decision-create-wizard.md §5) --------------------------------------
# The create WIZARD designs a schema BEFORE the layer exists (deferred-commit). This parses a schema file with
# the SAME adapters and validates each draft with the SAME FieldCreate path as the layer-bound import — but
# with NO layer: no existing-key dedup (there's no layer yet) and no commit. It returns, per importable row,
# the camelCase field-create payload the wizard holds in memory and POSTs to /custom-fields once Finish claims
# the slot. Gated to publishers (only they create layers).


def _fc_create_payload(fc: FieldCreate) -> dict[str, Any]:
    """A validated FieldCreate -> the exact body POST /custom-fields accepts (what the wizard re-sends)."""
    return {
        "fieldKey": fc.fieldKey, "label": fc.label, "dataType": fc.dataType, "required": fc.required,
        "defaultValue": fc.defaultValue, "defaultKind": fc.defaultKind, "constraints": fc.constraints,
        "numericSubtype": fc.numericSubtype,
    }


async def _preview_validate(conn: AsyncConnection, drafts: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate format-neutral drafts WITHOUT a layer — the layer-less mirror of `_run_field_import`'s
    validation phase (dedup is batch-only; no commit). Returns per-row verdicts + the field payloads for the
    'ok' rows so the wizard can hold + re-create them at Finish."""
    if len(drafts) > _MAX_IMPORT_FIELDS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"too many fields in one import (max {_MAX_IMPORT_FIELDS})")
    rows: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in drafts:
        fkey = d.get("field_key", "")
        if d.get("_error"):
            rows.append({"fieldKey": fkey, "status": "error", "reason": d["_error"]}); continue
        canon = _normalize_type(d.get("data_type", ""))
        if canon is None:
            rows.append({"fieldKey": fkey, "status": "error", "reason": f"unknown data type '{d.get('data_type', '')}'"}); continue
        if canon == "coded":
            rows.append({"fieldKey": fkey, "status": "coded",
                         "reason": "picklist field — add it and bind a domain after the layer is created"}); continue
        try:
            fc = FieldCreate(
                fieldKey=fkey, label=d.get("label", ""), dataType=canon, required=bool(d.get("required")),
                defaultValue=d.get("default_value"), defaultKind=d.get("default_kind", "constant"),
                constraints=d.get("constraints"), numericSubtype=d.get("numeric_subtype"),
            )
        except ValidationError as e:
            rows.append({"fieldKey": fkey, "status": "error", "reason": _first_validation_msg(e)}); continue
        if fc.fieldKey in seen:
            rows.append({"fieldKey": fc.fieldKey, "status": "duplicate", "reason": "a field with this key already exists"}); continue
        if fc.constraints and fc.constraints.get("pattern"):
            try:
                await _assert_pg_pattern(conn, fc.constraints["pattern"])
            except HTTPException as he:
                rows.append({"fieldKey": fc.fieldKey, "status": "error", "reason": str(he.detail)}); continue
        _rn = d.get("_renamed_from")
        _reason = (f"renamed — '{_rn}' is reserved on the ESRI door; imported as {fc.fieldKey}"
                   if _rn else None)
        seen.add(fc.fieldKey)
        fields.append(_fc_create_payload(fc))
        rows.append({"fieldKey": fc.fieldKey, "status": "renamed" if _rn else "ok", "reason": _reason})
    summary: dict[str, int] = {"ok": 0, "renamed": 0, "error": 0, "coded": 0, "duplicate": 0}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {"rows": rows, "fields": fields, "summary": summary}


class PreviewImportRequest(BaseModel):
    # Schema formats: 'csv' (field-definition rows) | 'geopackage' | 'esri-xml'.
    # Data-file formats (add-from-file, decision-add-from-file.md): 'shapefile' | 'geojson' | 'csv-data'.
    format: str
    csv_text: str | None = None        # for format='csv' (schema rows) or 'csv-data' (a data table)
    payload_base64: str | None = None  # for format in ('geopackage','esri-xml','shapefile','geojson')
    table: str | None = None           # esri feature-class pick (a GDB is multi-table)


def _decode_b64(payload_base64: str | None, max_bytes: int, what: str) -> bytes:
    if not payload_base64:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"payload_base64 is required for a {what}")
    try:
        data = base64.b64decode(payload_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="payload_base64 is not valid base64")
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="the uploaded file is empty")
    if len(data) > max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"file too large (max {max_bytes // (1024 * 1024)} MB)")
    return data


@router.post(
    "/custom-fields/preview-import",
    summary="Parse a schema or data file into draft field definitions (no layer) — for the create wizard (publisher)",
)
async def preview_import(
    payload: PreviewImportRequest,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    if not _has_publish_role(claims):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized to create layers")
    fmt = (payload.format or "").strip().lower()
    # Data-file formats add seed metadata (geometry/srid/counts — decision-add-from-file.md §4.1) on top of
    # the field verdicts; schema formats return the verdicts alone, exactly as before.
    meta: dict[str, Any] | None = None
    if fmt == "csv":
        if payload.csv_text is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="csv_text is required for a CSV import")
        drafts = _csv_to_drafts(payload.csv_text)
    elif fmt == "geopackage":
        drafts, meta = await run_in_threadpool(
            _geopackage_to_drafts, _decode_b64(payload.payload_base64, MAX_FILE_BYTES, "GeoPackage"))
    elif fmt == "shapefile":
        drafts, meta = await run_in_threadpool(
            _shapefile_to_drafts, _decode_b64(payload.payload_base64, MAX_FILE_BYTES, "zipped Shapefile"))  # K5
    elif fmt == "geojson":
        drafts, meta = _geojson_to_drafts(_decode_b64(payload.payload_base64, MAX_FILE_BYTES, "GeoJSON file"))
    elif fmt == "csv-data":
        if payload.csv_text is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="csv_text is required for a CSV import")
        if len(payload.csv_text) > MAX_FILE_BYTES:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                detail=f"file too large (max {MAX_FILE_BYTES // (1024 * 1024)} MB)")
        drafts, meta = _csvdata_to_drafts(payload.csv_text)
    elif fmt == "esri-xml":
        root = _esri_parse(_decode_b64(payload.payload_base64, _MAX_XML_BYTES, "Workspace XML"))
        tables = _esri_tables(root)
        if not tables:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="no feature classes or tables in the workspace")
        if payload.table is None and len(tables) > 1:  # picker flow (same shape as the layer-bound endpoint)
            return {"needsTable": True, "tables": [{"name": n, "fieldCount": c} for (n, _de, c) in tables]}
        chosen = tables[0] if payload.table is None else next((t for t in tables if t[0] == payload.table), None)
        if chosen is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"feature class '{payload.table}' not found")
        drafts = _esri_table_drafts(chosen[1], _esri_domain_kinds(root))
        result = await _preview_validate(conn, drafts)
        result["table"] = chosen[0]
        return result
    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="format must be one of: csv, geopackage, esri-xml, shapefile, geojson, csv-data")
    result = await _preview_validate(conn, drafts)
    if meta is not None:
        result.update(meta)
        result["featureCap"] = INGEST_FEATURE_CAP  # so the wizard can warn before Finish, not after
    return result


# --- schema EXPORT — the mirror of import (decision-schema-import.md §9) ---------------------------
# Columns EXACTLY the import set, so an export re-imports cleanly (round-trip).
_EXPORT_COLUMNS = ["field_key", "label", "data_type", "required", "default", "default_today",
                   "min", "max", "min_length", "max_length", "pattern"]


def _field_csv_row(f: dict) -> list[str]:
    """One active field (DB row) -> a CSV row matching the import columns."""
    c = f.get("constraints") or {}
    dk = f.get("default_kind") or "constant"
    today = dk == "today"
    dv = f.get("default_value")
    if today or dv is None:
        default = ""
    elif isinstance(dv, bool):
        default = "true" if dv else "false"
    else:
        default = str(dv)

    def s(v: Any) -> str:
        return "" if v is None else str(v)

    # A number field with a subtype exports its SUBTYPE name in the data_type cell (short_integer/long_integer/
    # float/double) so the import round-trip restores it; a generic number / other type exports as-is.
    dt = f.get("numeric_subtype") or f["data_type"]
    return [
        f["field_key"], f.get("label") or "", dt,
        "true" if f.get("required") else "false",
        default, "true" if today else "",
        s(c.get("min")), s(c.get("max")), s(c.get("minLength")), s(c.get("maxLength")), s(c.get("pattern")),
    ]


@router.get(
    "/layers/{layer_id}/custom-fields/export.csv",
    summary="Export a slot layer's ACTIVE field schema as CSV (re-imports cleanly; view-or-manage)",
)
async def export_fields_csv(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> Response:
    await _validate_slot(conn, layer_id)
    await _assert_can_read(claims, user, layer_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT field_key, label, data_type, required, default_value, default_kind, constraints, numeric_subtype "
            "FROM platform.layer_fields WHERE layer_id = %s AND active ORDER BY sort_order, field_key",
            (layer_id,),
        )
        fields = await cur.fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_EXPORT_COLUMNS)
    for f in fields:
        w.writerow(_field_csv_row(f))
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{layer_id}-schema.csv"'},
    )


# --- GeoPackage EXPORT — the heavier mirror of import (decision-schema-import.md §10) --------------
# Emit a VALID empty OGC GeoPackage carrying the active schema, so it re-imports through our OWN gpkg
# importer (round-trip). Built fully in memory: sqlite3 ':memory:' -> Connection.serialize() (py3.12, the
# mirror of import's deserialize()) -> bytes. Geometry type is DERIVED from the slot's own geom column (no
# hardcoded POINT), so a point/line/polygon layer exports faithfully. The round-trip
# is intentionally lossier than CSV (the importer stays untouched): gpkg carries field_key/data_type/label/
# required + number min/max; it does NOT carry defaults, text length/pattern, or date min/max. CSV stays the
# lossless schema export; gpkg is the GIS-interop export.
_GPKG_APPLICATION_ID = 0x47504B47  # 'GPKG' — the OGC magic in the SQLite header (offset 68)
_GPKG_USER_VERSION = 10300         # GeoPackage 1.3
# our type -> GeoPackage column type (chosen so our importer reads it back unchanged).
_GPKG_COL_TYPE = {"text": "TEXT", "number": "DOUBLE", "date": "DATE", "boolean": "BOOLEAN"}
# a number SUBTYPE -> distinct gpkg/sqlite column type, so SHORT vs LONG vs FLOAT vs DOUBLE round-trips
# (SMALLINT->short, MEDIUMINT->long; the importer's _normalize_subtype maps them back).
_GPKG_SUBTYPE_COL = {"short_integer": "SMALLINT", "long_integer": "MEDIUMINT", "float": "FLOAT", "double": "DOUBLE"}
# A minimal but valid WGS84 definition for the required gpkg_spatial_ref_sys row.
_WGS84_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
)


def _quote_ident(name: str) -> str:
    """Double-quote a SQLite identifier (field_key already passed the snake_case grammar; defensive)."""
    return '"' + name.replace('"', '""') + '"'


def _build_schema_gpkg(table: str, fields: list[dict], geom_type: str, srid: int) -> bytes:
    """Build an empty single-table feature GeoPackage carrying `fields` (active defs) and return its bytes.
    Cross-checked against what `_geopackage_to_drafts` (our importer) READS, so the round-trip closes."""
    con = sqlite3.connect(":memory:")
    try:
        con.execute(f"PRAGMA application_id = {_GPKG_APPLICATION_ID}")
        con.execute(f"PRAGMA user_version = {_GPKG_USER_VERSION}")

        # Required core tables.
        con.execute(
            "CREATE TABLE gpkg_spatial_ref_sys ("
            " srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY, organization TEXT NOT NULL,"
            " organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT)"
        )
        con.executemany(
            "INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)",
            [
                ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", None),
                ("Undefined geographic SRS", 0, "NONE", 0, "undefined", None),
                ("WGS 84 geodetic", 4326, "EPSG", 4326, _WGS84_WKT, None),
            ],
        )
        con.execute(
            "CREATE TABLE gpkg_contents ("
            " table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE,"
            " description TEXT DEFAULT '', last_change DATETIME NOT NULL"
            " DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),"
            " min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER)"
        )
        con.execute(
            "INSERT INTO gpkg_contents (table_name, data_type, identifier, last_change, srs_id)"
            " VALUES (?, 'features', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)",
            (table, table, srid),
        )
        con.execute(
            "CREATE TABLE gpkg_geometry_columns ("
            " table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,"
            " srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL,"
            " CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name))"
        )
        con.execute(
            "INSERT INTO gpkg_geometry_columns VALUES (?, 'geom', ?, ?, 0, 0)",
            (table, geom_type, srid),
        )

        # Schema extension tables (labels + range constraints) — registered so QGIS treats them as valid.
        con.execute(
            "CREATE TABLE gpkg_extensions ("
            " table_name TEXT, column_name TEXT, extension_name TEXT NOT NULL, definition TEXT NOT NULL,"
            " scope TEXT NOT NULL, CONSTRAINT ge_tce UNIQUE (table_name, column_name, extension_name))"
        )
        _schema_ext = "http://www.geopackage.org/spec/#extension_schema"
        con.executemany(
            "INSERT INTO gpkg_extensions (table_name, column_name, extension_name, definition, scope)"
            " VALUES (?, NULL, 'gpkg_schema', ?, 'read-write')",
            [("gpkg_data_columns", _schema_ext), ("gpkg_data_column_constraints", _schema_ext)],
        )
        con.execute(
            "CREATE TABLE gpkg_data_columns ("
            " table_name TEXT NOT NULL, column_name TEXT NOT NULL, name TEXT, title TEXT, description TEXT,"
            " mime_type TEXT, constraint_name TEXT, CONSTRAINT pk_gdc PRIMARY KEY (table_name, column_name))"
        )
        con.execute(
            "CREATE TABLE gpkg_data_column_constraints ("
            " constraint_name TEXT NOT NULL, constraint_type TEXT NOT NULL, value TEXT,"
            " min NUMERIC, min_is_inclusive BOOLEAN, max NUMERIC, max_is_inclusive BOOLEAN, description TEXT,"
            " CONSTRAINT gdcc_ntv UNIQUE (constraint_name, constraint_type, value))"
        )

        # The feature table: fid PK + geom + one column per active field. NOT NULL carries `required`; the
        # integer PK and the geom column are skipped by the importer.
        cols = [f"fid INTEGER PRIMARY KEY AUTOINCREMENT", f"geom {geom_type}"]
        for f in fields:
            if f["data_type"] == "coded":
                continue  # no domain values to write — flag/skip, consistent with import
            ct = _GPKG_SUBTYPE_COL.get(f.get("numeric_subtype")) or _GPKG_COL_TYPE.get(f["data_type"], "TEXT")
            nn = " NOT NULL" if f.get("required") else ""
            cols.append(f"{_quote_ident(f['field_key'])} {ct}{nn}")
        con.execute(f"CREATE TABLE {_quote_ident(table)} (" + ", ".join(cols) + ")")

        # gpkg_data_columns row per non-coded field: title = label (round-trips); constraint_name for number ranges.
        for f in fields:
            if f["data_type"] == "coded":
                continue
            cname = None
            c = f.get("constraints") or {}
            if f["data_type"] == "number" and (c.get("min") is not None or c.get("max") is not None):
                cname = f"{f['field_key']}_range"
                con.execute(
                    "INSERT INTO gpkg_data_column_constraints"
                    " (constraint_name, constraint_type, min, min_is_inclusive, max, max_is_inclusive)"
                    " VALUES (?, 'range', ?, 1, ?, 1)",
                    (cname, c.get("min"), c.get("max")),
                )
            con.execute(
                "INSERT INTO gpkg_data_columns (table_name, column_name, title, constraint_name)"
                " VALUES (?, ?, ?, ?)",
                (table, f["field_key"], f.get("label") or f["field_key"], cname),
            )

        return con.serialize()
    finally:
        con.close()


@router.get(
    "/layers/{layer_id}/custom-fields/export.gpkg",
    summary="Export a slot layer's ACTIVE field schema as an empty OGC GeoPackage (re-imports; view-or-manage)",
)
async def export_fields_gpkg(
    layer_id: str,
    conn: AsyncConnection = Depends(get_db),
    claims: dict | None = Depends(_validated_claims),
    user: User = Depends(get_current_user),
) -> Response:
    await _validate_slot(conn, layer_id)
    await _assert_can_read(claims, user, layer_id)
    # Derive the geometry type + srid from the slot's own geom column (no hardcoded POINT). Defensive
    # fallback to a generic GEOMETRY/4326 if PostGIS has no row (shouldn't happen for a ready slot).
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT type, srid FROM geometry_columns "
            "WHERE f_table_schema = 'assets' AND f_table_name = %s AND f_geometry_column = 'geom'",
            (layer_id,),
        )
        grow = await cur.fetchone()
    geom_type = (grow[0] if grow and grow[0] else "GEOMETRY").upper()
    srid = grow[1] if grow and grow[1] else 4326
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT field_key, label, data_type, required, default_value, default_kind, constraints, numeric_subtype "
            "FROM platform.layer_fields WHERE layer_id = %s AND active ORDER BY sort_order, field_key",
            (layer_id,),
        )
        fields = await cur.fetchall()
    data = _build_schema_gpkg(layer_id, fields, geom_type, srid)
    return Response(
        content=data,
        media_type="application/geopackage+sqlite3",
        headers={"Content-Disposition": f'attachment; filename="{layer_id}-schema.gpkg"'},
    )
