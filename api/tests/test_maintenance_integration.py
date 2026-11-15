"""Talus 39 — housekeeping job correctness.

- prune_applied_op_keys: live DB (opt-in; skips without a DB). Inserts a known OLD + FRESH idempotency
  key, runs the job, asserts the old one is pruned and the fresh one survives, then cleans up. Mirrors
  the repo's asyncio.run(case()) integration idiom (no pytest-asyncio).
- _select_orphans: the orphaned-media SAFETY rule (delete iff UNREFERENCED *and* older than the grace
  window) — pure logic, unit-tested with no live object store.

Override the DSN with TEST_DATABASE_URL if the default doesn't match your stack.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app.config import settings
from app.maintenance import _field_pack_live_keys, _select_orphans, prune_applied_op_keys
from app.media import StoredObject

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://talus:changeme@localhost:5432/talusgis")


async def _open_or_skip() -> psycopg.AsyncConnection:
    try:
        return await psycopg.AsyncConnection.connect(_DSN, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 — any connect failure -> skip
        pytest.skip(f"no DB at {_DSN}: {exc.__class__.__name__}")


def test_prune_applied_op_keys_deletes_only_old(monkeypatch):
    monkeypatch.setattr(settings, "maintenance_idempotency_key_retention_days", 30)
    old_key = uuid.uuid4()
    fresh_key = uuid.uuid4()

    async def case() -> None:
        conn = await _open_or_skip()
        try:
            async with conn.cursor() as cur:
                # OLD key (45d ago) -> must be pruned; FRESH key (now) -> must survive.
                await cur.execute(
                    "INSERT INTO platform.applied_op_keys (op_key, item_type, item_id, created_at) "
                    "VALUES (%s, 't39-test', %s, now() - interval '45 days'), "
                    "       (%s, 't39-test', %s, now())",
                    (old_key, str(old_key), fresh_key, str(fresh_key)),
                )
            await conn.commit()

            result = await prune_applied_op_keys(conn)
            assert result.items_affected >= 1  # at least our old key

            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT op_key FROM platform.applied_op_keys WHERE op_key = ANY(%s)",
                    ([old_key, fresh_key],),
                )
                remaining = {r[0] for r in await cur.fetchall()}
            assert old_key not in remaining   # old pruned
            assert fresh_key in remaining     # fresh kept (younger than retention)
        finally:
            # The old row is already gone; remove the surviving fresh test row.
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM platform.applied_op_keys WHERE item_type = 't39-test'")
            await conn.commit()
            await conn.close()

    asyncio.run(case())


def test_select_orphans_only_unreferenced_and_aged():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    objects = [
        StoredObject(key="attachments/referenced-old", size=10, last_modified=now - timedelta(days=30)),
        StoredObject(key="attachments/orphan-old", size=20, last_modified=now - timedelta(days=30)),
        StoredObject(key="attachments/orphan-fresh", size=30, last_modified=now - timedelta(days=1)),
        StoredObject(key="attachments/orphan-undated", size=40, last_modified=None),
    ]
    live_keys = {"attachments/referenced-old"}

    orphans = _select_orphans(objects, live_keys, cutoff)
    keys = {o.key for o in orphans}

    assert keys == {"attachments/orphan-old"}        # ONLY unreferenced AND older than the grace cutoff
    assert "attachments/referenced-old" not in keys  # referenced -> kept even though it's old
    assert "attachments/orphan-fresh" not in keys    # within the grace window -> kept (in-flight-upload guard)
    assert "attachments/orphan-undated" not in keys  # un-age-able -> never swept


def test_select_orphans_never_touches_other_bucket_tenants():
    """The prefix fence (field-projects security review, 2026-08-16): the media bucket hosts
    basemaps/ (PR #72) and field-projects/ (V085) objects whose registries the sweep does not
    read. They must survive the sweep no matter how old and 'unreferenced' they look — before
    the fence, every one of them was classified an orphan and deleted at grace+1 days while
    its catalog row lived on (silently breaking the basemap and every crew Import URL)."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    ancient = now - timedelta(days=365)
    objects = [
        # Other tenants — old, absent from media.attachments, and STILL never sweepable:
        StoredObject(key="basemaps/current.pmtiles", size=100, last_modified=ancient),
        StoredObject(key="basemaps/staging", size=100, last_modified=ancient),
        StoredObject(key="field-projects/2b6f9d1c-0000-4000-8000-000000000000/project.qgz",
                     size=100, last_modified=ancient),
        # Control: a genuine attachment orphan of the same age IS swept alongside them.
        StoredObject(key="attachments/orphan-old", size=20, last_modified=ancient),
    ]
    orphans = _select_orphans(objects, live_keys=set(), cutoff=cutoff)
    keys = {o.key for o in orphans}

    assert keys == {"attachments/orphan-old"}  # the fence: attachments-charter only


def test_select_orphans_reports_leg_uses_its_own_registry():
    """The reports/ leg (V100, decision-scheduled-reports.md r5): a report artifact blob is swept iff it is
    UNREFERENCED by platform.report_artifacts AND aged — judged against ITS OWN registry, never the
    attachments table. With the leg switched off (reports_live_keys=None) a reports/ key is fenced like
    any other tenant, so a deployment without the paid module never sweeps it by accident."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    ancient = now - timedelta(days=365)
    live_report = "reports/6d3cf7f6-0000-4000-8000-000000000000/live.pdf"
    # The r8 Word template is registered on platform.reports.template_object_key — the caller
    # unions it into reports_live_keys; aged, it must SURVIVE like any referenced artifact.
    live_template = "reports/6d3cf7f6-0000-4000-8000-000000000000/template.docx"
    objects = [
        StoredObject(key=live_report, size=10, last_modified=ancient),
        StoredObject(key=live_template, size=10, last_modified=ancient),
        StoredObject(key="reports/6d3cf7f6-0000-4000-8000-000000000000/stray.pdf", size=20, last_modified=ancient),
        StoredObject(key="reports/6d3cf7f6-0000-4000-8000-000000000000/fresh.pdf", size=30, last_modified=now),
        StoredObject(key="attachments/orphan-old", size=40, last_modified=ancient),
        StoredObject(key="basemaps/current.pmtiles", size=100, last_modified=ancient),
    ]
    # Leg ON: the stray, aged report blob goes; the referenced one and the fresh one stay; the
    # attachment leg is unchanged; other tenants stay fenced.
    keys = {o.key for o in _select_orphans(objects, live_keys=set(), cutoff=cutoff, reports_live_keys={live_report, live_template})}
    assert keys == {"reports/6d3cf7f6-0000-4000-8000-000000000000/stray.pdf", "attachments/orphan-old"}
    assert live_template not in keys  # the referenced template survives the sweep
    # A report key present in the ATTACHMENTS registry must not save it — registries never cross.
    keys = {o.key for o in _select_orphans(objects, live_keys={"reports/6d3cf7f6-0000-4000-8000-000000000000/stray.pdf"},
                                            cutoff=cutoff, reports_live_keys={live_report})}
    assert "reports/6d3cf7f6-0000-4000-8000-000000000000/stray.pdf" in keys
    # Leg OFF: no reports/ key is ever selected.
    keys = {o.key for o in _select_orphans(objects, live_keys=set(), cutoff=cutoff)}
    assert keys == {"attachments/orphan-old"}


def test_select_orphans_field_packs_leg_uses_the_manifest_registry():
    """The field-packs/ leg (V105, decision-talus-field-f3.md §6): a pack blob is swept iff it is
    UNREFERENCED by the served build's manifest AND aged — its own registry; None fences the prefix."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    ancient = now - timedelta(days=365)
    pid = "9d0d0d0d-0000-4000-8000-000000000000"
    manifest = {"basemap": {"path": "basemap.pmtiles"}, "layers": [{"path": "layers/tal_0000000a.geojson"}]}
    live = _field_pack_live_keys(pid, 2, manifest)
    assert live == {f"field-packs/{pid}/build/2/manifest.json", f"field-packs/{pid}/build/2/basemap.pmtiles",
                    f"field-packs/{pid}/build/2/layers/tal_0000000a.geojson"}
    objects = [
        StoredObject(key=f"field-packs/{pid}/build/2/basemap.pmtiles", size=1, last_modified=ancient),   # served → keep
        StoredObject(key=f"field-packs/{pid}/build/1/basemap.pmtiles", size=1, last_modified=ancient),   # superseded → sweep
        StoredObject(key="field-packs/gone-pack/build/1/manifest.json", size=1, last_modified=ancient),   # row deleted → sweep
        StoredObject(key=f"field-packs/{pid}/build/3/basemap.pmtiles", size=1, last_modified=now),       # mid-build, young → keep
        StoredObject(key="basemaps/current.pmtiles", size=1, last_modified=ancient),                        # fenced tenant
    ]
    swept = {o.key for o in _select_orphans(objects, set(), cutoff, field_packs_live_keys=live)}
    assert swept == {f"field-packs/{pid}/build/1/basemap.pmtiles", "field-packs/gone-pack/build/1/manifest.json"}
    # leg OFF: nothing under field-packs/ is judged
    assert _select_orphans(objects, set(), cutoff) == []
