"""Talus 34 — optimistic-concurrency guard tests (unit layer).

★ THE LIVE-DB LAYER WAS RETIRED with the pilot built-ins (decision-retire-builtins.md
phase 2): it drove `app.routers.signs` against `assets.signs`, both deleted. Per §5.1
("coverage must not silently vanish"), all four of its assertions now live in
verify-attachments.sh on a slot fixture: stale delete -> 409 with rollback (check 16),
correct-version delete succeeds (check 14a), 409 body carries the CURRENT row (T34a),
and missing row -> 404 not 409 (T34b). Landed 2026-08-28 — nothing is owed.


Unit (no DB): `require_if_match` — the mandatory If-Match dependency. Missing -> 428,
  malformed -> 400, ETag-quote tolerance.
- Integration (live Postgres, rolled back): a stale version -> 409 carrying the current
  server row; a vanished row -> 404; a correct version -> success returning a version.

The integration cases reuse the no-commit/rollback harness from the signs-mutation suite:
the route runs its writes against the live DB but nothing persists. Skips if the DB is
unreachable. Override the DSN with TEST_DATABASE_URL.
"""
import asyncio
import os
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException, status

from app.concurrency import require_if_match
from app.models.user import User

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://talus:changeme@localhost:5432/talusgis")


# --- require_if_match unit tests (no DB) ---------------------------------------


def test_require_if_match_missing_is_428():
    with pytest.raises(HTTPException) as exc:
        require_if_match(None)
    assert exc.value.status_code == status.HTTP_428_PRECONDITION_REQUIRED


def test_require_if_match_blank_is_428():
    with pytest.raises(HTTPException) as exc:
        require_if_match("   ")
    assert exc.value.status_code == status.HTTP_428_PRECONDITION_REQUIRED


def test_require_if_match_malformed_is_400():
    # non-numeric, the ETag wildcard, fractional, negative, and a value past int64 (bigint max + 1)
    for bad in ["abc", "*", "1.5", "-1", "9223372036854775808"]:
        with pytest.raises(HTTPException) as exc:
            require_if_match(bad)
        assert exc.value.status_code == status.HTTP_400_BAD_REQUEST, bad


def test_require_if_match_tolerates_etag_quoting():
    assert require_if_match("838") == "838"
    assert require_if_match('"838"') == "838"
    assert require_if_match('W/"838"') == "838"
