"""Deployment-aware entitlement runtime (decision-paid-packaging.md p1, Fork B3 resolved).

`app/entitlement.py` is deliberately a PURE mechanism — "given a path and a key, what's the
entitlement" — with no opinion about deployment shape (its own docstring). This module is the
companion that carries those opinions for a RUNNING deployment, in one place, so the core
admin door and every paid module gate resolve the licence identically:

  * WHERE the licence lives (Fork B3, ruled "both" — an explicitly mounted path wins;
    otherwise the admin-uploaded file at a fixed spot on the storage volume, which survives
    upgrades. It is NOT part of a backup set — the backup copies the media and raster folders
    only — so after a rebuild the licence file is applied again);
  * WHICH key verifies it (the baked production key — with the dev-only throwaway override,
    fenced to ENVIRONMENT=development exactly like stub-auth);
  * HOW uploaded content is verified and stored (verify FIRST with the full proven pipeline,
    then an atomic write — never an unverified byte at the licence path).

Core owns this because core owns the licence MECHANISM; it still knows nothing about what any
module name means (the one-way rule, paid/README.md).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import structlog

from .config import settings
from .entitlement import ENTITLEMENT_PUBLIC_KEY_PEM, EntitlementStatus, check_entitlement

log = structlog.get_logger(__name__)

# The admin-uploaded licence's fixed home (Fork B3): a subtree of the storage volume, so it
# persists across upgrades. The backup does not copy this subtree; the licence is a file the
# customer keeps and applies again after a rebuild.
_UPLOAD_SUBDIR = "talus-licence"
_UPLOAD_NAME = "licence.json"


def verifying_key_pem() -> bytes:
    """The verifying key. Production: the baked ENTITLEMENT_PUBLIC_KEY_PEM, full stop. In a
    DEVELOPMENT environment only, TALUS_ENTITLEMENT_DEV_PUBLIC_KEY_FILE may point at a
    throwaway public key so paid features can be exercised end-to-end before the real keypair
    exists — the same fence shape as stub-auth (config.py forbids that outside dev too).
    Outside dev the override is ignored entirely, so it can never weaken a real deployment's
    trust anchor. (Moved here from paid/api/entitlement_gate.py at p1 so the admin upload door
    and the module gates verify with the SAME key by construction.)"""
    if settings.environment.lower() in ("development", "dev", "local"):
        override = os.environ.get("TALUS_ENTITLEMENT_DEV_PUBLIC_KEY_FILE")
        if override:
            try:
                return Path(override).read_bytes()
            except OSError:
                log.warning("entitlement.dev_public_key_unreadable", path=override)
    return ENTITLEMENT_PUBLIC_KEY_PEM


def explicit_licence_path() -> str | None:
    """The operator-mounted licence path (TALUS_LICENCE_PATH), or None. When set, the install
    is FILE-MANAGED: the mount is the source of truth and the upload door refuses (409) rather
    than writing shadow state the gate would never read."""
    return os.environ.get("TALUS_LICENCE_PATH") or None


def uploaded_licence_path() -> Path:
    """Where the admin-uploaded licence lives (the default when no mount is configured)."""
    return Path(settings.storage_root) / _UPLOAD_SUBDIR / _UPLOAD_NAME


def effective_licence_path() -> str:
    """B3's precedence in one place: explicit mount if configured, else the uploaded file."""
    return explicit_licence_path() or str(uploaded_licence_path())


def current_status() -> EntitlementStatus:
    """The running deployment's entitlement — the one call every consumer (admin status door,
    paid module gates, renewal notice) shares. Per-request cheap: one small local file read."""
    return check_entitlement(effective_licence_path(), public_key_pem=verifying_key_pem())


def verify_licence_content(raw: bytes) -> EntitlementStatus:
    """Evaluate candidate licence BYTES through the exact proven pipeline (load -> schema ->
    signature -> term), without touching the live licence path: the bytes are staged in a
    private temp file and fed to check_entitlement. Every failure mode collapses to
    .absent(), same as the runtime gate."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
        fh.write(raw)
        tmp = fh.name
    try:
        return check_entitlement(tmp, public_key_pem=verifying_key_pem())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def store_uploaded_licence(raw: bytes) -> Path:
    """Atomically place VERIFIED licence bytes at the uploaded-licence path (callers verify
    first — the door's contract is that no unverified byte ever lands there). tmp + os.replace
    in the same directory, so the per-request gate only ever sees the old file or the new one,
    never a partial write."""
    dest = uploaded_licence_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, dest)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest
