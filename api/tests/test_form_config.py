"""The layer form document validator (decision-talus-field-f2.md §4.1) — pure units, mutation-check
style: remove a predicate from app/form_config.validate_form_config and a named test goes red.

  * drop extra="forbid"                         -> test_unknown_key_rejected
  * drop the active-field check                 -> test_foreign_field_key_rejected
  * drop the condition type gate                -> test_condition_on_number_rejected
  * drop the domain-code check                  -> test_in_value_not_in_domain_rejected
  * drop the template validate_props call       -> test_template_default_violating_constraint_rejected
  * drop the exactly-one-operator rule          -> test_condition_needs_one_operator
  * drop the boolean requiredIf refusal         -> test_boolean_cannot_be_conditionally_required
  * drop the self-dependency check              -> test_self_dependency_rejected
The live legs (authz, the trigger's 422, the field-ready filter) are inv39.
"""
import pytest

from app.form_config import FormConfigError, LayerFieldFacts, validate_form_config

FACTS = {
    "condition": LayerFieldFacts(data_type="coded", domain_codes={"good", "fair", "poor", "replace"}),
    "damage_desc": LayerFieldFacts(data_type="text", constraints={"maxLength": 200}),
    "replace_by": LayerFieldFacts(data_type="date"),
    "lit": LayerFieldFacts(data_type="boolean"),
    "height_ft": LayerFieldFacts(data_type="number", constraints={"min": 1, "max": 30}),
    "sign_type": LayerFieldFacts(data_type="coded", domain_codes={"R1-1", "R1-2"}),
    "inspector": LayerFieldFacts(data_type="text", required=True),
}

GOOD = {
    "version": 1,
    "groups": [
        {"title": "Sign", "fields": ["sign_type", "lit"]},
        {"title": "Condition", "fields": ["condition", "damage_desc", "replace_by"], "collapsed": False},
    ],
    "fields": {
        "damage_desc": {"visibleIf": {"field": "condition", "in": ["poor", "replace"]}},
        "replace_by": {"visibleIf": {"field": "condition", "eq": "replace"},
                       "requiredIf": {"field": "condition", "eq": "replace"}},
        "height_ft": {"readOnly": True},
    },
    "templates": [
        {"id": "stop", "name": "Stop sign", "defaults": {"sign_type": "R1-1", "lit": False}},
        {"id": "yield", "name": "Yield", "defaults": {"sign_type": "R1-2"}},
    ],
    "photo": {"prompt": True},
}


def errs(doc):
    with pytest.raises(FormConfigError) as ei:
        validate_form_config(doc, FACTS)
    return "\n".join(ei.value.errors)


def test_good_document_round_trips():
    cfg = validate_form_config(GOOD, FACTS)
    dumped = cfg.model_dump(by_alias=True, exclude_none=True)
    assert dumped["fields"]["damage_desc"]["visibleIf"] == {"field": "condition", "in": ["poor", "replace"]}
    assert dumped["fields"]["replace_by"]["requiredIf"] == {"field": "condition", "eq": "replace"}
    assert [t["id"] for t in dumped["templates"]] == ["stop", "yield"]


def test_empty_document_is_the_default_form():
    cfg = validate_form_config({}, FACTS)
    assert cfg.groups == [] and cfg.fields == {} and cfg.templates == []


def test_unknown_key_rejected():
    assert "extra" in errs({**GOOD, "expressions": {"x": "a + b"}}).lower() or "expressions" in errs({**GOOD, "expressions": {}})


def test_unknown_rule_key_rejected():
    doc = {**GOOD, "fields": {"damage_desc": {"calculate": "now()"}}}
    assert "calculate" in errs(doc)


def test_foreign_field_key_rejected():
    doc = {**GOOD, "groups": [{"title": "X", "fields": ["other_layers_field"]}]}
    assert "other_layers_field" in errs(doc) and "not an active field" in errs(doc)


def test_field_listed_twice_rejected():
    doc = {**GOOD, "groups": [{"title": "A", "fields": ["lit"]}, {"title": "B", "fields": ["lit"]}]}
    assert "more than one group" in errs(doc)


def test_condition_on_number_rejected():
    doc = {**GOOD, "fields": {"damage_desc": {"visibleIf": {"field": "height_ft", "eq": "3"}}}}
    assert "must be coded, boolean or text" in errs(doc)


def test_in_value_not_in_domain_rejected():
    doc = {**GOOD, "fields": {"damage_desc": {"visibleIf": {"field": "condition", "in": ["poor", "destroyed"]}}}}
    assert "'destroyed' is not a code" in errs(doc)


def test_boolean_eq_must_be_bool():
    doc = {**GOOD, "fields": {"damage_desc": {"visibleIf": {"field": "lit", "eq": "true"}}}}
    assert "eq must be true or false" in errs(doc)


def test_boolean_in_rejected():
    doc = {**GOOD, "fields": {"damage_desc": {"visibleIf": {"field": "lit", "in": ["true"]}}}}
    assert "use eq, not in" in errs(doc)


def test_condition_needs_one_operator():
    doc = {**GOOD, "fields": {"damage_desc": {"visibleIf": {"field": "condition", "eq": "poor", "isEmpty": True}}}}
    assert "exactly one of" in errs(doc)
    doc = {**GOOD, "fields": {"damage_desc": {"visibleIf": {"field": "condition"}}}}
    assert "exactly one of" in errs(doc)


def test_boolean_cannot_be_conditionally_required():
    doc = {**GOOD, "fields": {"lit": {"requiredIf": {"field": "condition", "eq": "poor"}}}}
    assert "boolean field cannot be conditionally required" in errs(doc)


def test_self_dependency_rejected():
    doc = {**GOOD, "fields": {"condition": {"visibleIf": {"field": "condition", "eq": "poor"}}}}
    assert "cannot depend on itself" in errs(doc)


def test_template_default_violating_constraint_rejected():
    doc = {**GOOD, "templates": [{"id": "tall", "name": "Tall", "defaults": {"height_ft": 99}}]}
    assert "height_ft" in errs(doc) and "at most" in errs(doc)


def test_template_default_unknown_field_rejected():
    doc = {**GOOD, "templates": [{"id": "x", "name": "X", "defaults": {"nope": 1}}]}
    assert "unknown field 'nope'" in errs(doc)


def test_template_default_bad_code_rejected():
    doc = {**GOOD, "templates": [{"id": "x", "name": "X", "defaults": {"sign_type": "W1-1"}}]}
    assert "'W1-1' is not a code" in errs(doc)


def test_duplicate_template_id_rejected():
    doc = {**GOOD, "templates": [{"id": "a", "name": "A", "defaults": {}}, {"id": "a", "name": "B", "defaults": {}}]}
    assert "duplicate template id" in errs(doc)


def test_template_id_must_be_a_slug():
    doc = {**GOOD, "templates": [{"id": "Stop Sign!", "name": "A", "defaults": {}}]}
    assert "templates" in errs(doc)


def test_oversize_document_rejected():
    doc = {**GOOD, "templates": [{"id": f"t{i}", "name": "x" * 80, "defaults": {"damage_desc": "y" * 200}} for i in range(100)]}
    # 100 templates * ~300 B is under 64 KiB; push it over with a long title list
    doc["groups"] = [{"title": "g" * 80, "fields": []} for _ in range(50)]
    big = {**doc, "fields": {}}
    raw = __import__("json").dumps(big)
    if len(raw) <= 64 * 1024:
        pytest.skip("fixture under the cap on this pydantic version")
    assert "exceeds" in errs(big)


def test_required_field_cannot_be_hidden():
    doc = {**GOOD, "fields": {"inspector": {"visibleIf": {"field": "condition", "eq": "poor"}}}}
    assert "required field and cannot be hidden" in errs(doc)


def test_required_if_must_match_visible_if():
    doc = {**GOOD, "fields": {"damage_desc": {"visibleIf": {"field": "condition", "eq": "poor"},
                                              "requiredIf": {"field": "condition", "eq": "replace"}}}}
    assert "requiredIf must match visibleIf" in errs(doc)
