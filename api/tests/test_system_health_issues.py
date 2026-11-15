"""System-health issue escalation (decision-system-health-issue-escalation.md).

`_issues` is the missing step between "every process is running" and "the system is fine". It is
pure and `now`-injectable precisely so the 2026-07-12 → 2026-07-26 DR outage can be replayed as a
fixture — that replay (`test_the_outage_would_now_be_caught`) is the regression guard this whole
slice exists for. During that window the page held `lastOkAt` 14 days stale, `scheduled_backup` at
`status='error'`, and every probe green, and it reported "All systems operational".

Pure-logic only: no live DB, no HTTP, no event loop.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.routers.system_health import (
    BackupState,
    JobFailure,
    JobState,
    Probe,
    _issues,
)

NOW = datetime(2026, 7, 26, 3, 0, 0, tzinfo=timezone.utc)
DAILY = 86_400


def _up(name: str) -> Probe:
    return Probe(name=name, status="up", latencyMs=5, error=None)


ALL_GREEN = [_up("postgres"), _up("storage"), _up("keycloak"), _up("api")]


def _call(**kw):
    """Defaults describe a HEALTHY system; each test overrides only what it is about."""
    base = dict(
        probes=ALL_GREEN,
        backup=BackupState(lastOkAt=(NOW - timedelta(hours=2)).isoformat(),
                           target="s3://backups (off-host)", scheduledEnabled=True),
        maintenance=[JobState(job="scheduled_backup", lastRunAt=NOW.isoformat(), lastStatus="ok")],
        job_failures={},
        restore_drill_ok_at=(NOW - timedelta(days=3)).isoformat(),
        backup_interval_seconds=DAILY,
        now=NOW,
    )
    base.update(kw)
    return _issues(**base)


def _keys(issues):
    return [i.key for i in issues]


# ---- the regression guard -----------------------------------------------------------------------

def test_the_outage_would_now_be_caught():
    """Replay 2026-07-12 → 2026-07-26 exactly: backup 14 days stale, scheduled_backup failing 119
    consecutive runs, EVERY PROBE GREEN. The old banner said "All systems operational" for 14 days
    because it derived from probes alone."""
    issues = _call(
        backup=BackupState(lastOkAt="2026-07-11T03:40:56+00:00",
                           target="s3://backups (off-host)", scheduledEnabled=True),
        maintenance=[JobState(job="scheduled_backup",
                              lastRunAt="2026-07-26T00:32:02+00:00", lastStatus="error")],
        job_failures={"scheduled_backup": JobFailure(consecutive=119,
                                                     failingSince="2026-07-12T06:22:22+00:00")},
    )
    assert "backup_stale" in _keys(issues)
    assert "job_failing:scheduled_backup" in _keys(issues)
    # Both are CRITICAL — the banner cannot stay green, which is the entire point.
    assert all(i.severity == "critical" for i in issues)
    assert any(i.severity == "critical" for i in issues)


def test_outage_summary_is_actionable_and_leaks_no_detail():
    """F5 text, and the §5 disclosure rule: summaries are fixed strings + scalars we already return.
    `maintenance_runs.detail` for this very outage quoted a LOCK TABLE enumerating internal table
    names — asserting its ABSENCE is the point, not just the presence of nice text."""
    issues = _call(
        backup=BackupState(lastOkAt="2026-07-11T03:40:56+00:00", target=None, scheduledEnabled=True),
        maintenance=[JobState(job="scheduled_backup", lastRunAt=None, lastStatus="error")],
        job_failures={"scheduled_backup": JobFailure(consecutive=119,
                                                     failingSince="2026-07-12T06:22:22+00:00")},
    )
    job = next(i for i in issues if i.key == "job_failing:scheduled_backup")
    assert "2026-07-12" in job.summary and "119 consecutive" in job.summary

    blob = " ".join(i.summary for i in issues)
    for leaked in ("LOCK TABLE", "flyway_schema_history", "pg_dump", "permission denied",
                   "Traceback", "postgresql://"):
        assert leaked not in blob


# ---- backup rules (F1, F3) ----------------------------------------------------------------------

def test_healthy_system_raises_nothing():
    """No new nagging: a healthy portal must still read exactly 'All systems operational'."""
    assert _call() == []


def test_disabled_schedule_suppresses_backup_issues():
    """A deliberately disabled schedule is an operator choice, not a fault."""
    issues = _call(backup=BackupState(lastOkAt="2026-01-01T00:00:00+00:00", target=None,
                                      scheduledEnabled=False))
    assert not [k for k in _keys(issues) if k.startswith("backup_")]


def test_never_backed_up_is_critical():
    issues = _call(backup=BackupState(lastOkAt=None, target=None, scheduledEnabled=True))
    assert [i.severity for i in issues if i.key == "backup_never"] == ["critical"]


def test_f3_boundary_interval_aware():
    """At 2x the configured interval: quiet. Just past it: an issue. Not a hardcoded 2 days."""
    inside = _call(backup=BackupState(lastOkAt=(NOW - timedelta(seconds=2 * DAILY - 60)).isoformat(),
                                      target=None, scheduledEnabled=True))
    assert "backup_stale" not in _keys(inside)

    outside = _call(backup=BackupState(lastOkAt=(NOW - timedelta(seconds=2 * DAILY + 600)).isoformat(),
                                       target=None, scheduledEnabled=True))
    assert "backup_stale" in _keys(outside)


def test_f3_floor_protects_short_intervals():
    """An hourly schedule must not hair-trigger: the 26h floor wins over 2 x interval."""
    issues = _call(backup_interval_seconds=3600,
                   backup=BackupState(lastOkAt=(NOW - timedelta(hours=6)).isoformat(),
                                      target=None, scheduledEnabled=True))
    assert "backup_stale" not in _keys(issues)


def test_weekly_schedule_does_not_false_positive():
    """The old hardcoded 2-day constant would have fired here every single day."""
    issues = _call(backup_interval_seconds=7 * DAILY,
                   backup=BackupState(lastOkAt=(NOW - timedelta(days=5)).isoformat(),
                                      target=None, scheduledEnabled=True))
    assert "backup_stale" not in _keys(issues)


# ---- other conditions ---------------------------------------------------------------------------

def test_non_backup_job_failure_is_warn_not_critical():
    issues = _call(maintenance=[JobState(job="cleanup_orphaned_media", lastRunAt=None,
                                         lastStatus="error")])
    assert [i.severity for i in issues if i.key.startswith("job_failing:")] == ["warn"]


def test_slow_probe_is_a_warn_issue():
    probes = [*ALL_GREEN, Probe(name="storage", status="slow", latencyMs=1500, error=None)]
    issues = _call(probes=probes)
    assert [i.severity for i in issues if i.key == "probe_slow"] == ["warn"]


def test_never_drilled_is_warn():
    issues = _call(restore_drill_ok_at=None)
    assert [i.severity for i in issues if i.key == "drill_never"] == ["warn"]


# ---- A4r: the drill_never grace period (decision-deployment-appliance.md §6) ---------------------

def test_never_drilled_inside_grace_is_quiet():
    """A box installed 1 day ago has honestly never drilled — and says nothing: the nag arms 14
    days after install so a fresh install doesn't look sick on day one."""
    issues = _call(restore_drill_ok_at=None, installed_at=NOW - timedelta(days=1))
    assert "drill_never" not in _keys(issues)


def test_never_drilled_past_grace_nags():
    issues = _call(restore_drill_ok_at=None, installed_at=NOW - timedelta(days=15))
    assert [i.severity for i in issues if i.key == "drill_never"] == ["warn"]


def test_unreadable_install_anchor_means_no_grace():
    """installed_at=None (owner pool down, anchor unreadable) fails toward NAGGING, never toward
    silence — the 14-day quiet window is a courtesy, not a right."""
    issues = _call(restore_drill_ok_at=None, installed_at=None)
    assert "drill_never" in _keys(issues)


def test_grace_does_not_quiet_a_recorded_drill_regression():
    """The grace window suppresses only drill_NEVER. A backup that has gone stale inside the same
    fortnight still screams — grace must not blanket other issues."""
    issues = _call(
        backup=BackupState(lastOkAt=None, target=None, scheduledEnabled=True),
        restore_drill_ok_at=None,
        installed_at=NOW - timedelta(days=1),
    )
    assert "backup_never" in _keys(issues)
    assert "drill_never" not in _keys(issues)


def test_critical_sorts_before_warn():
    issues = _call(
        backup=BackupState(lastOkAt=None, target=None, scheduledEnabled=True),
        restore_drill_ok_at=None,
    )
    assert [i.severity for i in issues] == ["critical", "warn"]


# ---- why the endpoint guards this call ------------------------------------------------------------

def test_issues_is_not_total_so_the_endpoint_must_guard_it():
    """`_issues` parses timestamps and does arithmetic on them, so it is NOT total — a malformed
    or timezone-naive `lastOkAt` raises. That is fine in itself; what matters is that the endpoint
    treats it as fallible.

    This file's contract is fail-open on DISPLAY (module docstring: a dead dependency renders a red
    row, it must never error the page), and every read in `_operational` self-guards. The `_issues`
    call in `get_system_health` is wrapped for the same reason: a health page that 500s is the worst
    possible failure for a screen whose whole job is telling you something is wrong.

    This test exists so the hazard is demonstrable rather than hypothetical — if someone later
    removes the try/except, this documents what it was protecting against.
    """
    with pytest.raises(Exception):
        _call(backup=BackupState(lastOkAt="not-a-timestamp", target=None, scheduledEnabled=True))

    # Timezone-naive is the realistic version: aware `now` minus naive parse -> TypeError.
    with pytest.raises(TypeError):
        _call(backup=BackupState(lastOkAt="2026-07-11T03:40:56", target=None, scheduledEnabled=True))
