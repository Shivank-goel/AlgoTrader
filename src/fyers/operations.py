"""Local FYERS readiness and recoverable SQLite backup procedures."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import time

from src.fyers.models import ROOT, RuntimeConfig
from src.fyers.qualification import qualification


def backup_database(source: Path, destination: Path) -> Path:
    """Create and integrity-check a consistent SQLite backup without stopping WAL writers."""
    if destination.exists():
        raise ValueError("Backup destination already exists")
    if not source.exists():
        raise ValueError("Source database does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    if temporary.exists():
        raise ValueError("Incomplete backup already exists; inspect it before retrying")
    source_db = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    target_db = sqlite3.connect(temporary)
    try:
        source_db.backup(target_db)
        target_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if target_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Backup failed SQLite integrity check")
        target_db.close()
        source_db.close()
        temporary.replace(destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return destination
    except BaseException:
        target_db.close()
        source_db.close()
        temporary.unlink(missing_ok=True)
        raise


def verify_backup(path: Path) -> dict:
    """Validate a backup before an operator considers a restore."""
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0] if "events" in tables else None
    finally:
        db.close()
    if integrity != "ok" or not {"events", "state", "fills", "positions"}.issubset(tables):
        raise ValueError("Backup is not a valid FYERS journal")
    return {"integrity": integrity, "events": events, "tables": sorted(tables)}


def prune_backups(directory: Path, *, retain: int) -> list[str]:
    """Remove only verified-name FYERS backups beyond the configured retention count."""
    if type(retain) is not int or retain < 1:
        raise ValueError("Backup retention must be positive")
    backups = sorted(directory.glob("runtime-????????T??????Z.sqlite3"),
                     key=lambda item: item.stat().st_mtime, reverse=True)
    removed = []
    for path in backups[retain:]:
        verify_backup(path)
        path.unlink()
        removed.append(str(path))
    return removed


def restore_drill(backup: Path, output_directory: Path) -> dict:
    """Restore into a new isolated directory and verify it; never replace production."""
    if output_directory.exists():
        raise ValueError("Restore-drill output directory already exists")
    source_report = verify_backup(backup)
    output_directory.mkdir(parents=True)
    restored = output_directory / "restored.sqlite3"
    source = sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)
    target = sqlite3.connect(restored)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    restored_report = verify_backup(restored)
    if restored_report != source_report:
        raise ValueError("Restored database differs from verified backup")
    marker = output_directory / "RESTORE_DRILL_ONLY"
    marker.write_text("Not approved for production replacement.\n")
    return {"backup": str(backup), "restored": str(restored), **restored_report,
            "production_replaced": False}


def readiness(config: RuntimeConfig, *, now: float | None = None) -> dict:
    """Report observable blockers; this function can never authorize live trading."""
    now = time.time() if now is None else now
    database = ROOT / config.database
    qualified, qualification_reason = qualification(ROOT / config.qualification_file,
                                                      ROOT / config.trials_file)
    blockers = []
    alerts = []
    details = {"qualification": qualification_reason, "database": str(database)}
    if not qualified:
        blockers.append("strategy_not_qualified")
    if not database.exists():
        blockers.append("journal_missing")
    else:
        db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            last = db.execute("SELECT received,kind FROM events ORDER BY id DESC LIMIT 1").fetchone()
            halt = db.execute("SELECT value FROM state WHERE key='halt_reason'").fetchone()
            pending = 0
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_orders'").fetchone():
                pending = db.execute("SELECT COUNT(*) FROM live_orders WHERE status NOT IN ('FILLED','CANCELLED','REJECTED','EXPIRED')").fetchone()[0]
            details.update(last_event=dict(last) if last else None, halt_reason=halt[0] if halt else None,
                           unresolved_orders=pending)
            if halt:
                blockers.append("persistent_halt")
            if pending:
                blockers.append("unresolved_orders")
            if not last or now - last["received"] > config.alert_after_seconds:
                alerts.append("journal_event_stale")
        finally:
            db.close()
    backup_dir = ROOT / config.backup_directory
    backups = sorted(backup_dir.glob("runtime-*.sqlite3"), key=lambda item: item.stat().st_mtime)
    details["latest_backup"] = str(backups[-1]) if backups else None
    if not backups or now - backups[-1].stat().st_mtime > config.backup_max_age_seconds:
        alerts.append("backup_missing_or_stale")
    blockers.extend(["forward_evidence_not_accepted", "live_release_not_implemented"])
    return {"as_of": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "live_enabled": False, "entry_ready": not blockers and not alerts,
            "blockers": blockers, "alerts": alerts, "details": details}
