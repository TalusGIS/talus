"""Admin → Talus+ entitlement: status + licence upload (decision-paid-packaging.md p1).

This door lives in CORE on purpose: core owns the licence mechanism (app/entitlement.py) and
its runtime resolution (app/entitlement_runtime.py), and the upload path must work regardless
of any module's gate state — a paid route that 404s while unlicensed could never accept the
licence that would unlock it. Core still knows nothing about what a module NAME means; the
module list is reported as opaque strings (the one-way rule holds).

Fail direction unchanged: this endpoint grants nothing. A free install answering here simply
reports "no licence" to an admin; features appear only where a licensed module gates itself.

Upload contract (Fork B3, ruled 2026-09-05):
  * admin-only (both routes);
  * candidate bytes are VERIFIED FIRST through the full proven pipeline (schema + Ed25519
    signature via the same key the gates use) — unsigned/tampered/malformed => 422, and no
    byte ever lands on disk;
  * a FILE-MANAGED install (TALUS_LICENCE_PATH mounted) refuses with 409 — the mount is the
    source of truth and an upload would be shadow state the gate never reads;
  * the write is atomic (tmp + rename beside the destination), the audit trail records the
    licence id — never the licence contents;
  * no restart needed: gates re-read per request (proven in both paid arcs' suites).
"""
from typing import Any

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, status

from .. import admin_audit
from ..db import get_db
from ..deps import _require_admin
from ..entitlement import storable_licence_json
from ..entitlement_runtime import (
    current_status,
    explicit_licence_path,
    store_uploaded_licence,
    verify_licence_content,
)
from ..models.user import User
from psycopg import AsyncConnection

log = structlog.get_logger(__name__)

router = APIRouter(tags=["entitlement-admin"])

# A licence file is ~half a KB; a megabyte is already absurd. Bound the door anyway.
_MAX_LICENCE_BYTES = 64 * 1024


def _status_payload() -> dict[str, Any]:
    st = current_status()
    return {
        "present": st.signed_and_valid,
        "withinTerm": st.within_term,
        "customer": st.customer,
        "termEnd": st.term_end.isoformat() if st.term_end else None,
        "modules": sorted(st.modules),
        # How this install manages its licence — drives the admin UI (an upload affordance is
        # pointless on a file-managed appliance).
        "fileManaged": explicit_licence_path() is not None,
    }


@router.get("/admin/entitlement", summary="Talus+ entitlement status (admin only)")
async def entitlement_status(user: User = Depends(_require_admin)) -> dict[str, Any]:
    return _status_payload()


@router.put(
    "/admin/entitlement/licence",
    summary="Upload/replace the Talus+ license file (admin only; verified before stored)",
)
async def upload_licence(
    body: dict[str, Any] = Body(..., description="The license file's JSON content, verbatim"),
    conn: AsyncConnection = Depends(get_db),
    user: User = Depends(_require_admin),
) -> dict[str, Any]:
    # An activation file is accepted whole, but ONLY the signed licence's own fields are verified
    # and stored — whatever else the upload carries is not core's, and never lands at the licence path.
    storable = storable_licence_json(body)
    if storable is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="not a valid signed license (schema or signature check failed)",
        )
    raw = storable.encode("utf-8")
    if len(raw) > _MAX_LICENCE_BYTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="license content is implausibly large")

    # Verify FIRST — the exact pipeline the gates run. Anything short of signed-and-valid is
    # refused honestly; an EXPIRED-but-signed licence is accepted (perpetual fallback: renewal
    # paperwork can lag, and the term flags drive only the notice).
    candidate = verify_licence_content(raw)
    if not candidate.signed_and_valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="not a valid signed license (schema or signature check failed)",
        )

    if explicit_licence_path() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this install's license is file-managed (TALUS_LICENCE_PATH is mounted) — "
            "replace the mounted file instead of uploading",
        )

    dest = store_uploaded_licence(raw)
    log.info(
        "entitlement_licence_uploaded", actor=str(user.id), customer=candidate.customer,
        within_term=candidate.within_term, path=str(dest),
    )
    await admin_audit.record(
        conn, actor_id=user.id, action="entitlement_licence_uploaded",
        target_type="entitlement", target_id="licence",
        detail={"customer": candidate.customer, "withinTerm": candidate.within_term},
    )
    return _status_payload()
