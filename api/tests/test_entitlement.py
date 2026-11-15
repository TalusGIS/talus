"""Tests for api/app/entitlement.py (decision-paid-module-home-entitlement.md).

Every test signs with a FRESH, throwaway Ed25519 keypair generated in-memory. Nothing here
ever reads, writes, or references the real production keypair.
"""
from __future__ import annotations

import inspect
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.entitlement import (
    EntitlementStatus,
    LicenceDocument,
    check_entitlement,
    sign_licence,
)


@pytest.fixture
def keypair():
    private_key = Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    return private_key, public_pem


def _licence(**overrides) -> LicenceDocument:
    today = date.today()
    fields = dict(
        licence_id="lic_test_0001",
        customer="Test Municipality",
        sku="talus-plus",
        issued_at=today,
        term_start=today - timedelta(days=10),
        term_end=today + timedelta(days=355),
        modules=["dashboards", "story_maps"],
    )
    fields.update(overrides)
    return LicenceDocument(**fields)


def _write(tmp_path: Path, private_key, licence: LicenceDocument) -> Path:
    signed = sign_licence(licence, private_key)
    path = tmp_path / "licence.json"
    path.write_text(signed.model_dump_json())
    return path


def test_valid_licence_grants_entitlement(tmp_path, keypair):
    private_key, public_pem = keypair
    path = _write(tmp_path, private_key, _licence())
    status = check_entitlement(path, public_key_pem=public_pem)
    assert status.signed_and_valid is True
    assert status.within_term is True
    assert "dashboards" in status.modules
    assert "story_maps" in status.modules
    assert status.customer == "Test Municipality"


def test_no_path_is_absent(keypair):
    _, public_pem = keypair
    assert check_entitlement(None, public_key_pem=public_pem) == EntitlementStatus.absent()
    assert check_entitlement("", public_key_pem=public_pem) == EntitlementStatus.absent()


def test_missing_file_is_absent(tmp_path, keypair):
    _, public_pem = keypair
    status = check_entitlement(tmp_path / "does-not-exist.json", public_key_pem=public_pem)
    assert status == EntitlementStatus.absent()


def test_malformed_json_is_absent(tmp_path, keypair):
    _, public_pem = keypair
    path = tmp_path / "licence.json"
    path.write_text("{not valid json")
    status = check_entitlement(path, public_key_pem=public_pem)
    assert status == EntitlementStatus.absent()


def test_schema_mismatch_is_absent(tmp_path, keypair):
    """Valid JSON, but missing the fields a SignedLicence needs — must fail closed, not raise
    into a caller that forgot to catch a validation error."""
    _, public_pem = keypair
    path = tmp_path / "licence.json"
    path.write_text(json.dumps({"unexpected": "shape"}))
    status = check_entitlement(path, public_key_pem=public_pem)
    assert status == EntitlementStatus.absent()


def test_tampered_field_is_rejected(tmp_path, keypair):
    """A modified module list after signing must fail the signature check, not silently grant
    the tampered entitlement — the negative test the whole mechanism exists for."""
    private_key, public_pem = keypair
    path = _write(tmp_path, private_key, _licence())
    raw = json.loads(path.read_text())
    raw["licence"]["modules"] = ["dashboards", "story_maps", "unlisted_module"]
    path.write_text(json.dumps(raw))
    status = check_entitlement(path, public_key_pem=public_pem)
    assert status.signed_and_valid is False
    assert status.modules == frozenset()


def test_wrong_key_is_rejected(tmp_path, keypair):
    """Signed with one keypair, verified against a DIFFERENT public key — must fail exactly
    like a forged licence would against the real product key. (This also covers the
    placeholder-key-in-the-shipped-module case: any licence not signed by the real private key
    fails identically, whether the placeholder is still in place or a real key is deployed.)"""
    private_key, _ = keypair
    other_private = Ed25519PrivateKey.generate()
    other_public_pem = other_private.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    path = _write(tmp_path, private_key, _licence())
    status = check_entitlement(path, public_key_pem=other_public_pem)
    assert status.signed_and_valid is False


def test_expired_term_stays_signed_and_valid_but_not_within_term(tmp_path, keypair):
    """★ THE PERPETUAL-FALLBACK PROOF. An expired licence must still report
    signed_and_valid=True (installed features keep working) while within_term flips False
    (drives the renewal notice only, never feature visibility). Merging these into one boolean
    is exactly the regression this test exists to catch."""
    private_key, public_pem = keypair
    expired = _licence(
        term_start=date.today() - timedelta(days=400),
        term_end=date.today() - timedelta(days=30),
    )
    path = _write(tmp_path, private_key, expired)
    status = check_entitlement(path, public_key_pem=public_pem)
    assert status.signed_and_valid is True
    assert status.within_term is False
    assert "dashboards" in status.modules  # still entitled to the installed feature


def test_not_yet_started_term_is_signed_and_valid_but_not_within_term(tmp_path, keypair):
    """The symmetric case — a licence dated to start in the future (e.g. clock skew, or an
    intentionally future-dated renewal) is a valid signature but not currently within term."""
    private_key, public_pem = keypair
    future = _licence(
        term_start=date.today() + timedelta(days=5),
        term_end=date.today() + timedelta(days=370),
    )
    path = _write(tmp_path, private_key, future)
    status = check_entitlement(path, public_key_pem=public_pem)
    assert status.signed_and_valid is True
    assert status.within_term is False


def test_module_not_granted_is_excluded(tmp_path, keypair):
    """A valid licence that simply doesn't list a module must not grant it — this is a
    per-module check, not a single valid/invalid bit."""
    private_key, public_pem = keypair
    path = _write(tmp_path, private_key, _licence(modules=["dashboards"]))
    status = check_entitlement(path, public_key_pem=public_pem)
    assert "dashboards" in status.modules
    assert "story_maps" not in status.modules


def test_injected_today_controls_term_evaluation(tmp_path, keypair):
    """The `today` override exists for exactly this: deterministic term-boundary tests without
    depending on the real clock."""
    private_key, public_pem = keypair
    licence = _licence(
        term_start=date(2026, 1, 1),
        term_end=date(2026, 12, 31),
    )
    path = _write(tmp_path, private_key, licence)
    assert check_entitlement(
        path, public_key_pem=public_pem, today=date(2026, 6, 1)
    ).within_term
    assert not check_entitlement(
        path, public_key_pem=public_pem, today=date(2027, 1, 1)
    ).within_term


def test_term_end_before_term_start_is_rejected_at_construction():
    with pytest.raises(ValueError):
        _licence(term_start=date(2026, 6, 1), term_end=date(2026, 1, 1))


def test_entitlement_module_has_no_dependency_on_paid_code():
    """Structural proof of the one-way dependency (paid -> core, never core -> paid;
    decision-paid-module-home-entitlement.md, paid/README.md). If this module ever imports
    from a `paid` package, the open-core boundary has been violated.

    Parses actual import AST nodes rather than grepping raw source text — a naive substring
    search false-positives on this very module's own docstring warning ("Core must never
    import from paid/"), which is prose, not an import statement.

    tripwire-allow-name-ref — this test must compare against the literal namespace name, so
    T8's path-reference arm is opted out here. It reaches no paid file: the names below are
    compared, never joined into a path or loaded."""
    import ast

    import app.entitlement as entitlement_mod

    tree = ast.parse(inspect.getsource(entitlement_mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name.split(".")[0] == "paid" for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or node.module.split(".")[0] != "paid"


# --- activation files (decision-ai-substrate.md §11): one file wraps the licence, UNCHANGED -------

def _activation(signed_json: str, **extra) -> str:
    import json
    doc = {"talus_activation": 1, "licence": json.loads(signed_json),
           "ai": {"code": "tac_" + "c" * 43, "expires": "2026-10-10"}}
    doc.update(extra)
    return json.dumps(doc)


def test_an_activation_file_grants_exactly_what_its_licence_grants(tmp_path, keypair):
    private_key, public_pem = keypair
    bare = _write(tmp_path, private_key, _licence())
    wrapped = tmp_path / "activation.json"
    wrapped.write_text(_activation(bare.read_text()))
    assert check_entitlement(wrapped, public_key_pem=public_pem) == check_entitlement(bare, public_key_pem=public_pem)
    assert check_entitlement(wrapped, public_key_pem=public_pem).signed_and_valid is True


def test_a_tampered_licence_inside_an_activation_file_is_still_rejected(tmp_path, keypair):
    import json
    private_key, public_pem = keypair
    doc = json.loads(_activation(_write(tmp_path, private_key, _licence()).read_text()))
    doc["licence"]["licence"]["modules"].append("ai")
    path = tmp_path / "activation.json"
    path.write_text(json.dumps(doc))
    assert check_entitlement(path, public_key_pem=public_pem) == EntitlementStatus.absent()


def test_nothing_in_the_wrapper_can_add_to_the_licence(tmp_path, keypair):
    """Core reads the `licence` member and nothing else: a `modules` list, a second signature or
    a `gateway` address beside it changes nothing."""
    private_key, public_pem = keypair
    bare = _write(tmp_path, private_key, _licence())
    path = tmp_path / "activation.json"
    path.write_text(_activation(bare.read_text(), modules=["ai", "reports"], signature="00",
                                gateway="https://evil.example"))
    status = check_entitlement(path, public_key_pem=public_pem)
    assert status.modules == frozenset({"dashboards", "story_maps"})


def test_unwrap_takes_the_licence_member_and_never_passes_the_wrapper_on():
    from app.entitlement import unwrap_activation
    bare = {"licence": {"licence_id": "x"}, "signature": "ab"}
    assert unwrap_activation(bare) is bare                                   # a bare licence is untouched
    assert unwrap_activation({"talus_activation": 1, "licence": bare, "ai": {"code": "tac_x"}}) is bare
    # a malformed activation file becomes "no licence" — its other members go nowhere
    for broken in ({"talus_activation": 1}, {"talus_activation": 1, "licence": "tac_secret"},
                   {"talus_activation": 1, "licence": None, "ai": {"code": "tac_secret"}}):
        assert unwrap_activation(broken) == {}
    assert unwrap_activation([1, 2]) == [1, 2] and unwrap_activation("x") == "x"


def test_a_malformed_activation_file_is_absent_and_its_code_is_not_logged(tmp_path, keypair, monkeypatch):
    """The loader logs the parse error. A validation error quotes its input, so the wrapper must
    never be what gets validated (checked by passing the wrapper through: this test then fails)."""
    from app import entitlement
    logged: list = []

    class _Recorder:
        def warning(self, event, **kw):
            logged.append((event, kw))

    monkeypatch.setattr(entitlement, "logger", _Recorder())
    _, public_pem = keypair
    path = tmp_path / "activation.json"
    path.write_text('{"talus_activation": 1, "licence": "nope", "ai": {"code": "tac_SECRETSECRETSECRETSECRETSECRETSECRET"}}')
    assert check_entitlement(path, public_key_pem=public_pem) == EntitlementStatus.absent()
    assert logged and "SECRET" not in repr(logged)   # pydantic truncates the MIDDLE of a quoted input: test the tail too


def test_what_the_upload_door_stores_is_the_licence_fields_and_nothing_else(tmp_path, keypair):
    import json
    from app.entitlement import storable_licence_json
    private_key, public_pem = keypair
    signed = json.loads(_write(tmp_path, private_key, _licence()).read_text())
    # an activation file, with a second wrapper and stray keys smuggled INSIDE the licence member
    smuggled = dict(signed, talus_activation=1, ai={"code": "tac_SMUGGLED"}, note="tac_SMUGGLED")
    out = storable_licence_json({"talus_activation": 1, "licence": smuggled, "ai": {"code": "tac_OUTER"}})
    assert out is not None and "tac_" not in out and "talus_activation" not in out
    assert set(json.loads(out)) == {"licence", "signature"}
    stored = tmp_path / "stored.json"
    stored.write_text(out)
    assert check_entitlement(stored, public_key_pem=public_pem).signed_and_valid is True   # the signature survives
    for junk in ({"talus_activation": 1}, {"x": 1}, "text", None, [signed]):
        assert storable_licence_json(junk) is None
