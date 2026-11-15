"""Offline paid-tier entitlement verification (decision-paid-module-home-entitlement.md).

Talus GIS LLC signs a small licence document with an Ed25519 private key that never ships;
the product carries only the public key (ENTITLEMENT_PUBLIC_KEY_PEM below). Verification is a
pure local signature check — no network call, ever — so this works identically air-gapped or
online (the decision doc's Part B: an air-gapped deployment forbids activation servers and phone-home
by definition).

Contract for paid/ code (the one-way dependency: paid -> core, never the reverse):
  * This module knows NOTHING about specific paid features. It verifies a signed document and
    reports which module NAMES it grants; a caller in paid/ decides what "dashboards" or
    "story_maps" means and gates its own routes accordingly. Core must never import from paid/.
  * ★ PERPETUAL FALLBACK (decision doc Part B, ruled 2026-09-03): expiry NEVER disables an
    installed feature. `EntitlementStatus.signed_and_valid` is what feature-gating code must
    check — it stays True forever once a genuine licence has been read, independent of term.
    Only `within_term` flips False on expiry, and that flag exists SOLELY to drive the admin
    renewal notice ("subscription year = LTR year") — never wire `within_term` into whether a
    feature renders. These are deliberately two separate booleans, not one, so that mistake is
    a type error rather than a silent regression.
  * Anti-piracy is an explicit non-goal (decision doc Part B) — this module makes it easy to be
    honest, not impossible to be dishonest. Do not "harden" this into obfuscation/DRM.
  * Every failure mode (missing file, corrupt JSON, schema mismatch, bad signature, wrong key)
    resolves to the SAME `EntitlementStatus.absent()` — a paid feature that can't prove its
    licence behaves exactly like a customer with no licence at all. Core is never touched by
    any of this; there is no failure path here that can affect a free-tier capability.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import structlog
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from pydantic import BaseModel, Field, field_validator

logger = structlog.get_logger()

# ---------------------------------------------------------------------------------------------
# The product's public key. This is the ONLY half of the keypair that ever ships — the matching
# private key never ships.
# ---------------------------------------------------------------------------------------------
ENTITLEMENT_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAbHuwRxlHx0d0i/OV7LRo/H/EosfMqN3KB+d27dv4wyk=
-----END PUBLIC KEY-----
"""


class LicenceDocument(BaseModel):
    """The signed fields of a Talus+ licence. Canonical JSON (sorted keys, compact separators)
    of exactly these fields is what gets signed — see `_canonical_bytes`. Deliberately
    human-readable (decision doc: "a municipal IT director can read what they bought —
    legibility as a feature") rather than an opaque token."""

    licence_id: str
    customer: str
    sku: str  # e.g. "talus-plus" — kept a free string, not an enum: core doesn't need to know
    # the SKU catalog, only paid/ code interpreting `modules` does.
    issued_at: date
    term_start: date
    term_end: date
    # Fork B2 (decided: ship as a list even under one paid SKU) — e.g. ["dashboards",
    # "story_maps"]. New paid modules extend this list; this file never needs to change for it.
    modules: list[str] = Field(default_factory=list)
    issuer: str = "Talus GIS LLC"

    @field_validator("term_end")
    @classmethod
    def _term_end_after_start(cls, v: date, info) -> date:
        start = info.data.get("term_start")
        if start is not None and v < start:
            raise ValueError("term_end before term_start")
        return v


class SignedLicence(BaseModel):
    """The on-disk / uploaded artifact: the licence fields plus a detached Ed25519 signature
    over their canonical serialization."""

    licence: LicenceDocument
    signature: str  # hex-encoded


def _canonical_bytes(licence: LicenceDocument) -> bytes:
    """The exact byte sequence that is signed and verified — sorted keys, compact separators,
    ISO-format dates (pydantic's json mode). ★ This function's output must never change after
    the first real licence ships — doing so invalidates every licence issued before the change."""
    payload = licence.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_licence(licence: LicenceDocument, private_key: Ed25519PrivateKey) -> SignedLicence:
    """Issuer-side only. This function is never called from a running Talus deployment —
    only from the licence-issuing script, run by hand against the private
    key. It exists in this module (rather than only the script) so the signing and verifying
    code share one `_canonical_bytes` implementation and can never drift apart."""
    signature = private_key.sign(_canonical_bytes(licence))
    return SignedLicence(licence=licence, signature=signature.hex())


def _verify_signature(signed: SignedLicence, public_key: Ed25519PublicKey) -> bool:
    try:
        public_key.verify(bytes.fromhex(signed.signature), _canonical_bytes(signed.licence))
        return True
    except (InvalidSignature, ValueError):
        return False


@dataclass(frozen=True)
class EntitlementStatus:
    """What paid/ code actually consults.

    ★ Feature-gating code MUST check `signed_and_valid` and `module in modules` — never
    `within_term` — to decide whether a feature renders. `within_term` exists only to drive the
    admin renewal notice. See the module docstring: this is the perpetual-fallback ruling made
    hard to get backwards by construction (two booleans, not one)."""

    signed_and_valid: bool
    modules: frozenset[str]
    within_term: bool
    term_end: date | None
    customer: str | None

    @classmethod
    def absent(cls) -> "EntitlementStatus":
        """No usable licence — the honest default for every failure mode. Every paid feature
        must render exactly as if no licence exists under this; core is never touched."""
        return cls(
            signed_and_valid=False,
            modules=frozenset(),
            within_term=False,
            term_end=None,
            customer=None,
        )


def unwrap_activation(doc: object) -> object:
    """An ACTIVATION FILE is `{"talus_activation": 1, "licence": <a signed licence, verbatim>, …}`
    — one file a customer receives that carries the licence plus whatever else a paid module was
    sent with it. Core takes the `licence` member and looks at nothing else: the other members
    are not core's to read, never verified here and never stored by core. Anything that is not an
    activation file is returned unchanged, so a bare licence keeps working everywhere."""
    if isinstance(doc, dict) and "talus_activation" in doc:
        # Never hand the wrapper itself onward: a malformed activation file must fail as "no
        # licence", not travel into a validation error (and a log line) with its other members.
        inner = doc.get("licence")
        return inner if isinstance(inner, dict) else {}
    return doc


def storable_licence_json(doc: object) -> str | None:
    """What the upload door may write to the licence path for an uploaded document: the signed
    licence's OWN fields, re-serialised, and nothing else — or None when the document is not
    licence-shaped. Anything riding along (an activation file's other members, stray keys an
    edited file nests inside the licence) is dropped here, so it can never reach the storage
    volume or a backup. The signature covers the canonical form of the licence fields, so
    re-serialising does not disturb it; the caller still verifies the result before storing."""
    try:
        return SignedLicence.model_validate(unwrap_activation(doc)).model_dump_json()
    except Exception:  # noqa: BLE001 - not licence-shaped; never log the document
        return None


def load_licence(path: str | Path) -> SignedLicence | None:
    """Read + parse a licence file (or an activation file wrapping one). Returns None on ANYTHING
    wrong (missing file, unreadable, malformed JSON, schema mismatch) — never raises. This is what
    makes every failure mode collapse to the same EntitlementStatus.absent() in check_entitlement
    below."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
        return SignedLicence.model_validate(unwrap_activation(json.loads(raw)))
    except Exception as exc:  # noqa: BLE001 - any parse failure means "no usable licence"
        logger.warning("entitlement.load_failed", path=str(path), error=str(exc))
        return None


def check_entitlement(
    path: str | Path | None,
    *,
    public_key_pem: bytes = ENTITLEMENT_PUBLIC_KEY_PEM,
    today: date | None = None,
) -> EntitlementStatus:
    """The seam. paid/ code calls this (directly, or via a thin FastAPI dependency it defines
    itself — this module has no router of its own) and gates on
    `status.signed_and_valid and "the_module_name" in status.modules`.

    `today` is injectable for tests; production callers omit it (uses the real UTC date).

    NOTE on `path`: resolving the actual on-disk/uploaded location (file mount vs admin upload,
    decision doc Fork B3) is deliberately NOT this module's job — it stays a pure function of
    "given a path, what's the entitlement" so it's trivially testable and carries no opinion
    about deployment shape. Whoever wires the first paid feature resolves B3 and passes the
    resulting path in.
    """
    if not path:
        return EntitlementStatus.absent()

    signed = load_licence(path)
    if signed is None:
        return EntitlementStatus.absent()

    try:
        public_key = load_pem_public_key(public_key_pem)
    except Exception:
        # A bad public key is a build/config bug, not a customer-facing one — but it must
        # still fail closed to absent, never raise into a paid route.
        logger.error("entitlement.public_key_invalid")
        return EntitlementStatus.absent()

    if not isinstance(public_key, Ed25519PublicKey) or not _verify_signature(
        signed, public_key
    ):
        logger.warning(
            "entitlement.signature_invalid", licence_id=signed.licence.licence_id
        )
        return EntitlementStatus.absent()

    when = today or datetime.now(timezone.utc).date()
    within_term = signed.licence.term_start <= when <= signed.licence.term_end
    return EntitlementStatus(
        signed_and_valid=True,
        modules=frozenset(signed.licence.modules),
        within_term=within_term,
        term_end=signed.licence.term_end,
        customer=signed.licence.customer,
    )
