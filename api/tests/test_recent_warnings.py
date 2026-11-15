"""V114: the ring numbers its records, the flusher's
reader sees only what is newer than its watermark, the public read never exposes the counter, and the
row/record shaping + merge are pure. The flush loop itself touches the pool; that path is proven on
a running stack."""
import structlog

from app import logging_config as lc
import structlog.contextvars

from app.recent_warnings_store import like_prefix, memory_records_for_request, merge, record_to_row, row_to_record


def _emit(n: int, event: str = "planted_warning"):
    log = structlog.get_logger("test.ring")
    for i in range(n):
        log.warning(event, i=i)


def test_ring_records_carry_a_monotonic_seq_hidden_from_the_public_read():
    before = lc.oldest_recent_seq()
    _emit(3)
    since = lc.recent_warnings_since(0)
    seqs = [s for s, _ in since]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all("seq" not in rec for _, rec in since)
    assert all("seq" not in rec for rec in lc.recent_warnings())
    assert lc.oldest_recent_seq() is not None and (before is None or lc.oldest_recent_seq() >= before)


def test_since_is_a_watermark_read():
    _emit(2, "first_batch")
    mark = lc.recent_warnings_since(0)[-1][0]
    _emit(2, "second_batch")
    newer = lc.recent_warnings_since(mark)
    assert [rec["event"] for _, rec in newer] == ["second_batch", "second_batch"]
    assert lc.recent_warnings_since(newer[-1][0]) == []


def test_record_to_row_lifts_request_id_and_bounds_every_field():
    rec = {"timestamp": "2026-10-13T20:00:00.000000Z", "level": "warn", "logger": "x" * 300,
           "event": "e" * 600, "fields": {"request_id": "abc-123", "detail": "d"}}
    at, level, logger, event, fields, rid = record_to_row(rec)
    assert at is not None and at.tzinfo is not None
    assert level == "warning"                      # 'warn' normalised to the CHECK's vocabulary
    assert len(logger) == 200 and len(event) == 500
    assert rid == "abc-123" and fields.obj["request_id"] == "abc-123"


def test_record_to_row_tolerates_missing_or_odd_values():
    at, level, logger, event, fields, rid = record_to_row({"timestamp": "not a date", "level": "weird", "event": None})
    assert at is None and level == "warning" and logger is None and event == "" and rid is None


def test_row_to_record_matches_the_ring_shape_and_says_table():
    from datetime import datetime, timezone
    rec = row_to_record({"at": datetime(2026, 10, 13, tzinfo=timezone.utc), "level": "error", "logger": "l",
                         "event": "backup_env_unreadable", "fields": {"path": "/x"}})
    assert set(rec) == {"timestamp", "level", "logger", "event", "fields", "source"}
    assert rec["source"] == "table" and rec["timestamp"].startswith("2026-10-13")


def test_merge_puts_the_table_first_and_marks_every_record():
    out = merge([{"event": "old"}], [{"event": "new"}])
    assert [(r["event"], r["source"]) for r in out] == [("old", "table"), ("new", "memory")]


def test_memory_records_for_request_matches_by_prefix_and_ignores_short_codes():
    structlog.contextvars.bind_contextvars(request_id="feedface-0000-4000-8000-000000000001")
    try:
        structlog.get_logger("test.ring").warning("bound_warning")
    finally:
        structlog.contextvars.clear_contextvars()
    structlog.get_logger("test.ring").warning("unbound_warning")
    hits = memory_records_for_request("feedface")
    assert [r["event"] for r in hits] == ["bound_warning"]
    assert memory_records_for_request("feedface-0000-4000-8000-000000000001") == hits
    assert memory_records_for_request("feedfac") == []          # 7 chars: below the minimum, never a wildcard match


def test_like_prefix_escapes_like_metacharacters():
    assert like_prefix("ab%_c\\d") == "ab\\%\\_c\\\\d%"
