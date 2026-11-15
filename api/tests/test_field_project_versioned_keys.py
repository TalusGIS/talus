"""Each version of a field project's file has its own key (once-over 2026-11-15 R2).

The backup's media copy is append-only and skips any key it has already archived. A project file was
replaced IN PLACE under one fixed key (Replace project file, Regenerate), so no backup after the first
ever held the newer file: a restore brought back the first version while the restored row described
the newer one, its embedded credential included. Every write now uses a fresh key recorded in the
row's `object_key`; the live store drops the replaced file once the row points past it.
"""
import asyncio
import inspect

from app import backup
from app.routers import field_projects
from app.routers.field_projects import _new_object_key, _remove_replaced

PID = "11111111-2222-4333-8444-555555555555"


def test_each_version_gets_a_fresh_key_under_the_projects_folder():
    a, b = _new_object_key(PID), _new_object_key(PID)
    assert a != b
    for k in (a, b):
        assert k.startswith(f"field-projects/{PID}/project-") and k.endswith(".qgz")


def test_every_write_uses_a_fresh_key_and_records_it():
    for fn in (field_projects.create_field_project, field_projects.generate_field_project):
        assert "_new_object_key(" in inspect.getsource(fn), f"{fn.__name__} writes the fixed key"
    for fn in (field_projects.replace_field_project, field_projects.regenerate_field_project):
        src = inspect.getsource(fn)
        assert "_new_object_key(" in src, f"{fn.__name__} overwrites the fixed key in place"
        assert "object_key = %s" in src, f"{fn.__name__} does not record the new key in the row"
        assert "_remove_replaced(" in src, f"{fn.__name__} leaves the replaced file in the live store"


def test_delete_removes_the_file_the_row_names():
    src = inspect.getsource(field_projects.delete_field_project)
    assert "object_key FROM platform.field_projects" in src and "remove_object, stored_key" in src


def _patch_remove(monkeypatch, fail=False):
    removed: list[str] = []

    def _remove(key):
        if fail:
            raise RuntimeError("store down")
        removed.append(key)

    monkeypatch.setattr(field_projects, "remove_object", _remove)
    return removed


def test_the_replaced_file_is_removed_after_the_switch(monkeypatch):
    removed = _patch_remove(monkeypatch)
    asyncio.run(_remove_replaced("field-projects/x/project-old.qgz", "field-projects/x/project-new.qgz", PID))
    assert removed == ["field-projects/x/project-old.qgz"]


def test_nothing_is_removed_when_there_was_no_other_version(monkeypatch):
    removed = _patch_remove(monkeypatch)
    asyncio.run(_remove_replaced(None, "k", PID))
    asyncio.run(_remove_replaced("k", "k", PID))
    assert removed == []


def test_a_failed_removal_never_fails_the_request(monkeypatch):
    _patch_remove(monkeypatch, fail=True)
    asyncio.run(_remove_replaced("old", "new", PID))  # logs, does not raise


def test_a_backup_skips_a_file_removed_since_it_was_listed():
    """A replaced file can vanish between the backup's listing and its copy; that is not a failure."""
    src = inspect.getsource(backup.run_backup)
    assert "except MediaObjectMissing:" in src and "backup_media_object_gone" in src
