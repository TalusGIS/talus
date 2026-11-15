"""The certificate and clock probes' pure classifiers
and their issue lines. The handshake and the DB read are proven on the dev stack.
Pure-logic only: no network, no DB."""
from datetime import datetime, timedelta, timezone

from app.routers.system_health import (
    BackupState,
    JobState,
    Probe,
    _certificate_probe_from,
    _clock_probe_from,
    _issues,
)

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
ACME = "R11"
INTERNAL = "Caddy Local Authority - ECC Intermediate"


def test_public_certificate_is_green_then_amber_inside_two_weeks_then_red_when_expired():
    ok = _certificate_probe_from(NOW + timedelta(days=60), NOW, ACME, 12)
    assert (ok.status, ok.error) == ("up", None) and "expires in 60 days" in ok.detail and ACME in ok.detail
    amber = _certificate_probe_from(NOW + timedelta(days=9), NOW, ACME, 12)
    assert amber.status == "slow" and "expires in 9 days" in amber.detail
    red = _certificate_probe_from(NOW - timedelta(hours=5), NOW, ACME, 12)
    assert (red.status, red.error) == ("down", "certificate expired") and "expired 5 hours ago" in red.detail


def test_internal_ca_short_leaf_is_not_a_warning_only_a_lapsed_renewal_is():
    # Caddy's internal CA issues ~12 h leaves and renews them itself: 9 hours left is normal.
    fine = _certificate_probe_from(NOW + timedelta(hours=9), NOW, INTERNAL, 8)
    assert fine.status == "up" and "renewed automatically" in fine.detail
    lapsing = _certificate_probe_from(NOW + timedelta(minutes=20), NOW, INTERNAL, 8)
    assert lapsing.status == "slow"
    assert _certificate_probe_from(NOW - timedelta(minutes=1), NOW, INTERNAL, 8).status == "down"


def test_per_deployment_named_internal_ca_is_recognised_by_its_short_leaf_lifetime():
    # an upgraded appliance: issuer "Talus GIS (192.0.2.10) - ECC Intermediate", a 12 h leaf.
    issuer = "Talus GIS (192.0.2.10) - ECC Intermediate"
    p = _certificate_probe_from(NOW + timedelta(hours=10), NOW, issuer, 9, not_before=NOW - timedelta(hours=2))
    assert p.status == "up" and "renewed automatically" in p.detail
    # the same name with a long-lived leaf (not Caddy's internal issuance) keeps the public rule
    assert _certificate_probe_from(NOW + timedelta(days=9), NOW, issuer, 9, not_before=NOW - timedelta(days=81)).status == "slow"
    # without a not_before (older callers) the name-only rule still holds
    assert _certificate_probe_from(NOW + timedelta(hours=9), NOW, INTERNAL, 8).status == "up"


def test_clock_is_green_within_a_minute_and_red_past_it():
    p = _clock_probe_from(NOW, NOW + timedelta(seconds=1.2), "Tue, 13 Oct 2026 12:00:01 GMT")
    assert (p.status, p.error) == ("up", None)
    assert "database +1.2 s" in p.detail and "sign-in server +1 s" in p.detail
    red = _clock_probe_from(NOW, NOW + timedelta(seconds=90), None)
    assert (red.status, red.error) == ("down", "clock skew") and "sends no Date header" in red.detail
    red2 = _clock_probe_from(NOW, NOW, "Tue, 13 Oct 2026 12:05:00 GMT")
    assert red2.status == "down" and "+300 s" in red2.detail


def test_clock_tolerates_an_unreadable_database_and_a_bad_date_header():
    p = _clock_probe_from(NOW, None, "not a date")
    assert p.status == "up" and "database unreadable" in p.detail and "unparseable" in p.detail


def _call(probes):
    return _issues(probes=probes,
                   backup=BackupState(lastOkAt=(NOW - timedelta(hours=2)).isoformat(), target="s3://x", scheduledEnabled=True),
                   maintenance=[JobState(job="scheduled_backup", lastRunAt=NOW.isoformat(), lastStatus="ok")],
                   job_failures={}, restore_drill_ok_at=NOW.isoformat(), backup_interval_seconds=86_400, now=NOW)


def test_issues_name_an_expiring_certificate_and_a_skewed_clock_but_not_a_slow_certificate_as_slow():
    amber = _certificate_probe_from(NOW + timedelta(days=3), NOW, ACME, 10)
    keys = {i.key: i for i in _call([amber])}
    assert "certificate_expiring" in keys and keys["certificate_expiring"].severity == "warn"
    assert "expires in 3 days" in keys["certificate_expiring"].summary
    assert "probe_slow" not in keys                                   # not "responding slowly"
    expired = _certificate_probe_from(NOW - timedelta(days=1), NOW, ACME, 10)
    assert {i.key for i in _call([expired])} == {"certificate_expired"}
    skew = _clock_probe_from(NOW, NOW + timedelta(seconds=400), None)
    crit = {i.key: i for i in _call([skew])}["clock_skew"]
    assert crit.severity == "critical" and "+400.0 s" in crit.summary
    assert _call([Probe(name="clock", status="up", latencyMs=None, error=None, detail="database +0.0 s")]) == []
