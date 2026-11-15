"""App-side validation of owner-defined feature attribute values (`props`) — c3 of the
data-owner-independence arc (decision-owner-defined-fields.md), extended for constraint rules (slice 3,
decision-constraint-rules.md).

The friendly half of the V044/V048 enforcement: it rejects unknown keys, type-mismatched values, AND the
declarative constraints (range/length/pattern) with clear per-field messages BEFORE the write. Kept
deliberately THIN — it mirrors the generic trigger `assets.fn_validate_layer_props`, which stays the
DB-authoritative belt and owns DEFAULTS + REQUIRED + the holistic guarantee. Anything the app misses still
fails closed at the trigger; `map_props_check_violation` surfaces that as a clean 422. Mirror the trigger's
rules — keep the two in lockstep (see V044/V048).
"""
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from fastapi import HTTPException, status
from psycopg import AsyncConnection, errors as pg_errors


class PropsError(Exception):
    """One or more per-field validation errors (-> 422)."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class FieldDef:
    """The writable shape of one active field: its type + (optional) declarative constraints jsonb."""
    data_type: str
    constraints: dict | None


def _is_iso_date(v: Any) -> bool:
    if not isinstance(v, str):
        return False
    try:
        date.fromisoformat(v)
        return True
    except ValueError:
        return False


# field data_type -> (predicate, human phrase). Mirrors assets.fn_validate_layer_props (V044).
# bool is a subclass of int — exclude it from `number` explicitly.
_TYPE_RULES: dict[str, tuple] = {
    "text": (lambda v: isinstance(v, str), "text"),
    "number": (lambda v: not isinstance(v, bool) and isinstance(v, (int, float)), "a number"),
    "boolean": (lambda v: isinstance(v, bool), "true or false"),
    "date": (_is_iso_date, "a valid date (YYYY-MM-DD)"),
    # 'coded' is a string code here; MEMBERSHIP (the code exists in the bound domain) is the V046
    # trigger's job — the app only shape-checks, since the edit form's dropdown offers valid codes.
    # Likewise the V077 contingency PAIR check (child valid for parent's value) is trigger-only: it
    # needs the merged row (call sites pass partial props) and the trigger's 422 is already clean.
    "coded": (lambda v: isinstance(v, str), "a code"),
}


def _check_constraints(key: str, dtype: str, value: Any, c: dict) -> list[str]:
    """Mirror the V048 trigger's range/length/pattern branches for a single (already type-valid) value.
    Returns friendly per-field messages (may be empty). Only keys relevant to `dtype` are read."""
    errs: list[str] = []
    if dtype == "number":
        if "min" in c and value < c["min"]:
            errs.append(f"field '{key}' must be at least {c['min']}")
        if "max" in c and value > c["max"]:
            errs.append(f"field '{key}' must be at most {c['max']}")
    elif dtype == "date":
        v = date.fromisoformat(value)  # value passed _is_iso_date already
        if "min" in c and v < date.fromisoformat(c["min"]):
            errs.append(f"field '{key}' must be on or after {c['min']}")
        if "max" in c and v > date.fromisoformat(c["max"]):
            errs.append(f"field '{key}' must be on or before {c['max']}")
    elif dtype == "text":
        if "minLength" in c and len(value) < c["minLength"]:
            errs.append(f"field '{key}' must be at least {c['minLength']} characters")
        if "maxLength" in c and len(value) > c["maxLength"]:
            errs.append(f"field '{key}' must be at most {c['maxLength']} characters")
        pat = c.get("pattern")
        if pat:
            try:
                if re.search(pat, value) is None:  # stored pattern is anchored at author time
                    errs.append(f"field '{key}' does not match the required format")
            except re.error:
                pass  # a non-Python-compilable pattern still fails closed at the trigger
    return errs


async def active_field_defs(conn: AsyncConnection, layer_id: str) -> dict[str, FieldDef]:
    """field_key -> FieldDef for the layer's ACTIVE fields (the writable set)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT field_key, data_type, constraints FROM platform.layer_fields "
            "WHERE layer_id = %s AND active",
            (layer_id,),
        )
        return {r[0]: FieldDef(r[1], r[2]) for r in await cur.fetchall()}


def validate_props(defs: dict[str, FieldDef], props: dict[str, Any]) -> None:
    """Reject unknown keys, type-mismatched non-null values, and constraint violations. NULL passes here
    (the trigger enforces `required`). Raises PropsError listing every problem. Defaults + required are the
    trigger's job."""
    errors: list[str] = []
    for key, value in props.items():
        fd = defs.get(key)
        if fd is None:
            errors.append(f"unknown field '{key}'")
            continue
        if value is None:
            continue
        predicate, human = _TYPE_RULES[fd.data_type]
        if not predicate(value):
            errors.append(f"field '{key}' must be {human}")
            continue  # type is wrong — constraint checks below would be meaningless
        if fd.constraints:
            errors.extend(_check_constraints(key, fd.data_type, value, fd.constraints))
    if errors:
        raise PropsError(errors)


def map_props_check_violation(e: pg_errors.CheckViolation) -> HTTPException:
    """Translate the trigger's check_violation (the defaults/required/holistic backstop) into a clean
    422 carrying its friendly message (e.g. 'layer_fields: field "x" is required')."""
    msg = (getattr(e.diag, "message_primary", None) or str(e)).strip()
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=msg)
