"""The layer form document (decision-talus-field-f2.md §4.1) — the closed declarative vocabulary,
its pydantic shape, and the layer-aware validator the form door runs before a byte is stored.

★ Data, not code (the standing fence): nothing in this document is evaluated, templated or used as a
selector. Every key is enumerated below; an unknown key is a 400 (`extra="forbid"`). A condition
names ONE sibling field and ONE operator with enumerable values; the only composition is that every
rule on a field applies (implicit AND). The trigger (V104) evaluates `requiredIf` in SQL with the
same four operators and nothing else; the client model (slotFormModel.ts) mirrors it.
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .layer_props import PropsError, validate_props

MAX_DOC_BYTES = 64 * 1024
MAX_GROUPS = 50
MAX_TEMPLATES = 100
MAX_GROUP_FIELDS = 100
CONDITION_FIELD_TYPES = ("coded", "boolean", "text")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Condition(_Strict):
    """ONE sibling field, ONE operator. `eq` on coded/text is the code/string; on boolean the bool."""
    field: str = Field(min_length=1, max_length=63)
    eq: str | bool | None = None
    in_: list[str] | None = Field(default=None, alias="in", max_length=200)
    isEmpty: Literal[True] | None = None
    notEmpty: Literal[True] | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _exactly_one_operator(self) -> "Condition":
        ops = [k for k, v in (("eq", self.eq), ("in", self.in_), ("isEmpty", self.isEmpty), ("notEmpty", self.notEmpty)) if v is not None]
        if len(ops) != 1:
            raise ValueError("a condition needs exactly one of eq / in / isEmpty / notEmpty")
        if self.in_ is not None and not self.in_:
            raise ValueError("'in' needs at least one value")
        return self

    @property
    def operator(self) -> str:
        if self.eq is not None:
            return "eq"
        if self.in_ is not None:
            return "in"
        return "isEmpty" if self.isEmpty else "notEmpty"


class FieldRules(_Strict):
    visibleIf: Condition | None = None
    requiredIf: Condition | None = None
    readOnly: Literal[True] | None = None


class Group(_Strict):
    title: str = Field(min_length=1, max_length=80)
    fields: list[str] = Field(max_length=MAX_GROUP_FIELDS)
    collapsed: bool | None = None


class Template(_Strict):
    id: str = Field(pattern=_SLUG_RE.pattern)
    name: str = Field(min_length=1, max_length=80)
    defaults: dict[str, Any] = Field(default_factory=dict)


class Photo(_Strict):
    prompt: bool = True


class FormConfig(_Strict):
    version: Literal[1] = 1
    groups: list[Group] = Field(default_factory=list, max_length=MAX_GROUPS)
    fields: dict[str, FieldRules] = Field(default_factory=dict)
    templates: list[Template] = Field(default_factory=list, max_length=MAX_TEMPLATES)
    photo: Photo | None = None


class FormConfigError(Exception):
    """Every problem found, in one go (the PropsError idiom). The door turns it into a 400."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class LayerFieldFacts(BaseModel):
    """What the validator needs to know about ONE active field of the layer."""
    data_type: str
    constraints: dict | None = None
    domain_codes: set[str] | None = None  # coded fields only: the bound domain's codes (active + retired)
    required: bool = False  # the STATIC required flag (V044) — a hidden required field would block every save


def validate_form_config(doc: dict[str, Any], facts: dict[str, LayerFieldFacts]) -> FormConfig:
    """Shape-check the document (pydantic, unknown keys refused), then check every reference against
    the layer's ACTIVE fields: group members, rule targets, condition fields (and their types), `eq`/`in`
    values against the domain, template defaults through the same `validate_props` every write uses.
    Raises FormConfigError listing every problem. Returns the parsed config on success."""
    raw = json.dumps(doc, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_DOC_BYTES:
        raise FormConfigError([f"form config exceeds {MAX_DOC_BYTES} bytes"])
    try:
        cfg = FormConfig.model_validate(doc)
    except Exception as exc:  # pydantic ValidationError -> one readable line per problem
        lines = []
        errors = getattr(exc, "errors", None)
        if callable(errors):
            for e in errors():
                loc = ".".join(str(p) for p in e.get("loc", ()))
                lines.append(f"{loc or 'document'}: {e.get('msg', 'invalid')}")
        raise FormConfigError(lines or [str(exc)]) from exc

    errors: list[str] = []
    known = set(facts)

    def check_field(key: str, where: str) -> None:
        if key not in known:
            errors.append(f"{where}: '{key}' is not an active field of this layer")

    seen_in_groups: set[str] = set()
    for gi, g in enumerate(cfg.groups):
        for key in g.fields:
            check_field(key, f"groups[{gi}] '{g.title}'")
            if key in seen_in_groups:
                errors.append(f"groups[{gi}] '{g.title}': '{key}' is listed in more than one group")
            seen_in_groups.add(key)

    def check_condition(cond: Condition, where: str, target: str) -> None:
        check_field(cond.field, where)
        f = facts.get(cond.field)
        if f is None:
            return
        if cond.field == target:
            errors.append(f"{where}: a field cannot depend on itself")
        if f.data_type not in CONDITION_FIELD_TYPES:
            errors.append(f"{where}: condition field '{cond.field}' must be coded, boolean or text (is {f.data_type})")
            return
        op = cond.operator
        if f.data_type == "boolean":
            if op == "eq" and not isinstance(cond.eq, bool):
                errors.append(f"{where}: '{cond.field}' is boolean — eq must be true or false")
            elif op == "in":
                errors.append(f"{where}: '{cond.field}' is boolean — use eq, not in")
        elif f.data_type == "coded":
            codes = f.domain_codes or set()
            vals = [cond.eq] if op == "eq" else (cond.in_ or [])
            for v in vals:
                if not isinstance(v, str) or v not in codes:
                    errors.append(f"{where}: '{v}' is not a code of '{cond.field}'s domain")
        else:  # text
            if op == "eq" and not isinstance(cond.eq, str):
                errors.append(f"{where}: '{cond.field}' is text — eq must be a string")

    for key, rules in cfg.fields.items():
        where = f"fields.{key}"
        check_field(key, where)
        f = facts.get(key)
        if rules.visibleIf:
            check_condition(rules.visibleIf, f"{where}.visibleIf", key)
            # A statically required field can never be hidden: the trigger would refuse every save made
            # while it was invisible — a block the crew cannot see. Make it conditionally required instead.
            if f is not None and f.required:
                errors.append(f"{where}.visibleIf: '{key}' is a required field and cannot be hidden — use requiredIf")
        if rules.visibleIf and rules.requiredIf and rules.visibleIf.model_dump(by_alias=True, exclude_none=True) != rules.requiredIf.model_dump(by_alias=True, exclude_none=True):
            # The trigger cannot see visibility; a requirement that fires while the field is hidden is a
            # save the crew cannot complete. When both exist they must be the same condition.
            errors.append(f"{where}: requiredIf must match visibleIf when both are set")
        if rules.requiredIf:
            check_condition(rules.requiredIf, f"{where}.requiredIf", key)
            if f is not None and f.data_type == "boolean":
                errors.append(f"{where}.requiredIf: a boolean field cannot be conditionally required")

    ids: set[str] = set()
    defs = {k: _FieldDefLike(v.data_type, v.constraints) for k, v in facts.items()}
    for ti, t in enumerate(cfg.templates):
        where = f"templates[{ti}] '{t.name}'"
        if t.id in ids:
            errors.append(f"{where}: duplicate template id '{t.id}'")
        ids.add(t.id)
        try:
            validate_props(defs, t.defaults)  # unknown keys, types, constraints — the write path's rules
        except PropsError as exc:
            errors.extend(f"{where}: {e}" for e in exc.errors)
        for k, v in t.defaults.items():
            f = facts.get(k)
            if f is not None and f.data_type == "coded" and v is not None and v not in (f.domain_codes or set()):
                errors.append(f"{where}: '{v}' is not a code of '{k}'s domain")

    if errors:
        raise FormConfigError(errors)
    return cfg


class _FieldDefLike:
    """The duck `validate_props` expects (layer_props.FieldDef without the import cycle)."""
    __slots__ = ("data_type", "constraints")

    def __init__(self, data_type: str, constraints: dict | None):
        self.data_type = data_type
        self.constraints = constraints
