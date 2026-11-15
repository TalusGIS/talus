"""The failed-runs read's pure shaping — bounded
reasons and samples, display names only, newest first across kinds. The SQL half is proven on the dev
stack (a run against a stopped source)."""
from datetime import datetime, timezone

from app.run_history import REASON_CHARS, SAMPLE_MAX, import_row_to_entry, merge_newest_first, pull_row_to_entry


def test_pull_entry_names_the_connection_source_and_layer_and_bounds_the_reason():
    e = pull_row_to_entry({"connection": "City DB", "source_schema": "gis", "source_table": "parcels", "layer": "Parcels",
                           "trigger": "schedule", "error": "x" * 400, "failed_rows": 2, "processed_rows": 10, "total_rows": 10,
                           "at": datetime(2026, 10, 13, 12, tzinfo=timezone.utc),
                           "error_sample": [{"row": i, "reason": "r" * 500} for i in range(8)]})
    assert e["kind"] == "sql_pull" and e["name"] == "City DB → gis.parcels" and e["layer"] == "Parcels"
    assert e["when"].startswith("2026-10-13T12:00") and e["trigger"] == "schedule"
    assert len(e["reason"]) == REASON_CHARS
    assert len(e["error_sample"]) == SAMPLE_MAX and all(len(s["reason"]) == REASON_CHARS for s in e["error_sample"])


def test_import_entry_uses_the_display_filename_and_tolerates_empties():
    e = import_row_to_entry({"format": "shp-zip", "original_name": "roads.zip", "layer": None, "error": None,
                             "failed_rows": 0, "processed_rows": 0, "total_rows": None, "at": None, "error_sample": "junk"})
    assert e["kind"] == "import" and e["name"] == "shp-zip: roads.zip"
    assert e["reason"] is None and e["when"] is None and e["error_sample"] == []


def test_merge_is_newest_first_across_kinds_with_undated_last():
    out = merge_newest_first([{"kind": "import", "when": "2026-10-01T00:00:00+00:00"},
                              {"kind": "sql_pull", "when": None},
                              {"kind": "sql_pull", "when": "2026-10-02T00:00:00+00:00"}])
    assert [(e["kind"], e["when"]) for e in out][0] == ("sql_pull", "2026-10-02T00:00:00+00:00")
    assert out[-1]["when"] is None
