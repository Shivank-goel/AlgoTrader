import os
import sqlite3

import pytest

from src.fyers.journal import Journal
from src.fyers.models import RuntimeConfig
from src.fyers.operations import backup_database, prune_backups, readiness, restore_drill, verify_backup


def test_backup_is_consistent_verified_and_never_overwritten(tmp_path):
    source, backup = tmp_path / "runtime.db", tmp_path / "backup.db"
    journal = Journal(source)
    journal.event("fixture", {"safe": True}, 100)
    backup_database(source, backup)
    journal.event("later", {}, 101)
    journal.close()
    assert verify_backup(backup)["events"] == 1
    with pytest.raises(ValueError, match="already exists"):
        backup_database(source, backup)


def test_corrupt_backup_is_rejected(tmp_path):
    path = tmp_path / "bad.db"
    path.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        verify_backup(path)


def test_restore_drill_never_replaces_source(tmp_path):
    source, backup = tmp_path / "runtime.db", tmp_path / "backup.db"
    journal = Journal(source)
    journal.event("fixture", {}, 100)
    journal.close()
    backup_database(source, backup)
    before = source.read_bytes()
    report = restore_drill(backup, tmp_path / "drill")
    assert report["events"] == 1 and not report["production_replaced"]
    assert source.read_bytes() == before
    assert (tmp_path / "drill/RESTORE_DRILL_ONLY").exists()


def test_backup_retention_only_removes_verified_named_backups(tmp_path):
    source = tmp_path / "runtime.db"
    journal = Journal(source)
    journal.close()
    for index in range(3):
        path = tmp_path / f"runtime-2026092{index}T120000Z.sqlite3"
        backup_database(source, path)
        os.utime(path, (index, index))
    unrelated = tmp_path / "other.sqlite3"
    unrelated.write_text("keep")
    removed = prune_backups(tmp_path, retain=2)
    assert len(removed) == 1 and unrelated.exists()


def test_readiness_is_fail_closed_and_reports_stale_backup(tmp_path, monkeypatch):
    config = RuntimeConfig.load().model_copy(update={
        "database": "runtime.db", "qualification_file": "missing.json",
        "trials_file": "missing-trials.json", "backup_directory": "backups",
        "alert_after_seconds": 10, "backup_max_age_seconds": 10})
    journal = Journal(tmp_path / "runtime.db")
    journal.event("fixture", {}, 100)
    journal.close()
    backups = tmp_path / "backups"
    backups.mkdir()
    backup = backups / "runtime-old.sqlite3"
    backup_database(tmp_path / "runtime.db", backup)
    os.utime(backup, (100, 100))
    monkeypatch.setattr("src.fyers.operations.ROOT", tmp_path)
    report = readiness(config, now=1000)
    assert not report["live_enabled"] and not report["entry_ready"]
    assert "strategy_not_qualified" in report["blockers"]
    assert set(report["alerts"]) == {"journal_event_stale", "backup_missing_or_stale"}
