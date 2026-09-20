"""Local FYERS readiness and recoverable SQLite backup procedures."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from dotenv import dotenv_values, load_dotenv

from src.execution.fyers import FyersAuthenticationError, FyersClient
from src.fyers.journal import Journal
from src.fyers.models import ROOT, RuntimeConfig, environment_path
from src.fyers.qualification import qualification
from src.fyers.sessions import NseSessionCalendar


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
        blockers.extend(["strategy_not_qualified", "forward_evidence_not_accepted"])
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
    details["latest_backup_age_seconds"] = now - backups[-1].stat().st_mtime if backups else None
    if not backups or now - backups[-1].stat().st_mtime > config.backup_max_age_seconds:
        alerts.append("backup_missing_or_stale")
    blockers.append("live_release_not_implemented")
    return {"as_of": datetime.fromtimestamp(now, UTC).isoformat(),
            "live_enabled": False, "entry_ready": not blockers and not alerts,
            "blockers": blockers, "alerts": alerts, "details": details}


def _check(name: str, passed: bool, detail: str, *, blocking: bool = True) -> dict:
    return {"name": name, "passed": bool(passed), "blocking": blocking, "detail": detail}


async def preflight(config: RuntimeConfig, *, network: bool = True) -> dict:
    """Secret-safe environment and runtime audit; it can never authorize live trading."""
    checks: list[dict] = []
    checks.append(_check("python", sys.version_info >= (3, 12),
                         f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"))
    env_path = environment_path()
    env_exists = env_path.is_file()
    checks.append(_check("environment_file", env_exists, str(env_path)))
    if env_exists:
        mode = env_path.stat().st_mode & 0o777
        checks.append(_check("environment_permissions", mode & 0o077 == 0,
                             f"mode {mode:04o}; group/other access must be zero"))
        values = dotenv_values(env_path)
        missing = [key for key in ("FYERS_APP_ID", "FYERS_APP_SECRET", "FYERS_REDIRECT_URI",
                                    "FYERS_ACCESS_TOKEN", "DASHBOARD_CONTROL_TOKEN")
                   if not str(values.get(key) or os.environ.get(key) or "").strip()]
        checks.append(_check("required_environment", not missing,
                             "configured" if not missing else "missing: " + ", ".join(missing)))
        load_dotenv(env_path, override=False)
    calendar = NseSessionCalendar(ROOT / config.holidays_file,
                                  config.session_start, config.session_end)
    state = calendar.state()
    checks.append(_check("reviewed_calendar", state.phase.value != "calendar_unavailable",
                         state.reason))
    database = ROOT / config.database
    if database.exists():
        try:
            db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            integrity = db.execute("PRAGMA quick_check").fetchone()[0]
            db.close()
            checks.append(_check("database_integrity", integrity == "ok", str(integrity)))
        except sqlite3.Error as exc:
            checks.append(_check("database_integrity", False, type(exc).__name__))
    else:
        checks.append(_check("database_integrity", False, "journal missing"))
    free = shutil.disk_usage(ROOT).free
    checks.append(_check("disk_space", free >= config.minimum_free_disk_bytes,
                         f"{free} bytes free"))
    backup_dir = ROOT / config.backup_directory
    backups = sorted(backup_dir.glob("runtime-*.sqlite3"), key=lambda item: item.stat().st_mtime)
    backup_ok = bool(backups and time.time() - backups[-1].stat().st_mtime <= config.backup_max_age_seconds)
    checks.append(_check("recent_backup", backup_ok,
                         str(backups[-1]) if backups else "missing"))
    hashes = {}
    for relative in ("config/fyers_runtime.yaml", "config/fyers_costs.yaml",
                     "config/fyers_economics.yaml", "config/fyers_strategy_lab.yaml",
                     "config/nse_forward_universe.yaml", config.holidays_file):
        path = ROOT / relative
        if path.is_file():
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    checks.append(_check("configuration_hashes", len(hashes) == 6,
                         json.dumps(hashes, sort_keys=True)))
    if network:
        auth_state = "UNAVAILABLE"
        try:
            client = FyersClient.from_env()
            try:
                await client.get_profile()
            finally:
                await client.close()
            checks.append(_check("fyers_authentication", True, "profile verified"))
            auth_state = "READY"
        except FyersAuthenticationError as exc:
            checks.append(_check("fyers_authentication", False, type(exc).__name__))
            auth_state = "AUTH_REQUIRED"
        except Exception as exc:
            checks.append(_check("fyers_authentication", False, type(exc).__name__))
        if config.expected_outbound_ip:
            try:
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get("https://ifconfig.me/ip", allow_redirects=False) as response:
                        observed = (await response.text()).strip()
                        ipaddress.ip_address(observed)
                checks.append(_check("outbound_ip", observed == config.expected_outbound_ip,
                                     f"observed {observed}"))
            except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
                checks.append(_check("outbound_ip", False, type(exc).__name__))
    passed = all(row["passed"] or not row["blocking"] for row in checks)
    result = {"as_of": datetime.now(UTC).isoformat(), "passed": passed,
              "live_enabled": False, "checks": checks}
    if database.exists():
        try:
            auth = next((row for row in checks if row["name"] == "fyers_authentication"), None)
            journal = Journal(database)
            with journal.db:
                journal.put("preflight", result)
                if auth is not None:
                    previous_auth = journal.get("authentication", {})
                    journal.put("authentication", {
                        "state": auth_state,
                        "at": time.time(), "detail": auth["detail"],
                    })
                    previous_state = (previous_auth.get("state")
                                      if isinstance(previous_auth, dict) else None)
                    if auth_state == "AUTH_REQUIRED" and previous_state != "AUTH_REQUIRED":
                        journal.db.execute(
                            "INSERT INTO events(received,kind,payload) VALUES(?,?,?)",
                            (time.time(), "authentication_required", json.dumps({
                                "reason": "daily FYERS access token rejected",
                            })),
                        )
            journal.close()
        except (OSError, sqlite3.Error):
            pass
    return result


async def finalize_session(config: RuntimeConfig, day, *, client: FyersClient | None = None) -> dict:
    """Idempotently publish completed bars, session evidence and a verified backup."""
    from src.data.session import export_session, load_session
    from src.fyers.daily_data import sync_daily_history

    calendar = NseSessionCalendar(ROOT / config.holidays_file,
                                  config.session_start, config.session_end)
    start, end = calendar.session_bounds(day)
    journal = Journal(ROOT / config.database)
    old = journal.get("session_finalization", {})
    if isinstance(old, dict) and old.get("session_date") == day.isoformat() and old.get("status") == "complete":
        journal.close()
        return old
    report = {"session_date": day.isoformat(), "status": "failed", "steps": {},
              "completed_at": None, "live_enabled": False}
    try:
        from src.data.nse.bhavcopy import BhavcopyClient

        frame = await asyncio.to_thread(BhavcopyClient().fetch_day, day)
        bhav_path = ROOT / "data/nse/bhavcopy/days" / f"{day.isoformat()}.parquet"
        if frame.empty or not bhav_path.exists():
            # The legacy client caches an unavailable download as a non-trading
            # marker. This date is already a reviewed session, so retain retryability.
            marker = ROOT / "data/nse/bhavcopy/non_trading_days.txt"
            if marker.exists():
                values = set(marker.read_text().split())
                if day.isoformat() in values:
                    values.remove(day.isoformat())
                    marker.write_text("\n".join(sorted(values)))
            raise ValueError("official NSE bhavcopy unavailable for reviewed session")
        report["steps"]["nse_bhavcopy"] = {
            "status": "complete", "rows": len(frame),
            "sha256": hashlib.sha256(bhav_path.read_bytes()).hexdigest(),
        }
    except Exception as exc:
        report["steps"]["nse_bhavcopy"] = {
            "status": "failed", "error_type": type(exc).__name__,
        }
    owns_client = client is None
    try:
        if client is None:
            load_dotenv(environment_path(), override=False)
            client = FyersClient.from_env()
        daily = await sync_daily_history(client, config, start=day, end=day)
        if len(daily) != 1 or daily[0].benchmark is None:
            raise ValueError("completed FYERS session or benchmark candle unavailable")
        report["steps"]["daily_bars"] = {
            "status": "complete", "artifacts": [row.content_sha256 for row in daily],
        }
    except Exception as exc:
        report["steps"]["daily_bars"] = {"status": "failed", "error_type": type(exc).__name__}
        if isinstance(exc, FyersAuthenticationError):
            with journal.db:
                journal.put("authentication", {"state": "AUTH_REQUIRED", "at": time.time()})
    finally:
        if owns_client and client is not None:
            await client.close()
    try:
        output = ROOT / config.session_export_directory / f"{day.isoformat()}.json"
        artifact = load_session(output) if output.exists() else export_session(
            ROOT / config.database, output, start=start.timestamp(), end=end.timestamp(),
            symbols=config.symbols, stale_seconds=config.stale_seconds,
        )
        report["steps"]["session_export"] = {
            "status": "complete", "path": str(output),
            "sha256": artifact.session_sha256, "quality": artifact.quality,
        }
    except Exception as exc:
        report["steps"]["session_export"] = {"status": "failed", "error_type": type(exc).__name__}
    with journal.db:
        journal.put("session_finalization", {**report, "status": "finalizing"})
    try:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = ROOT / config.backup_directory / f"runtime-{stamp}.sqlite3"
        backup_database(ROOT / config.database, destination)
        report["steps"]["backup"] = {"status": "complete", "path": str(destination),
                                      **verify_backup(destination)}
        report["steps"]["backup"]["pruned"] = prune_backups(
            destination.parent, retain=config.backup_retention_count,
        )
    except Exception as exc:
        report["steps"]["backup"] = {"status": "failed", "error_type": type(exc).__name__}
    if all(step.get("status") == "complete" for step in report["steps"].values()):
        report["status"] = "complete"
        report["completed_at"] = datetime.now(UTC).isoformat()
    with journal.db:
        journal.put("session_finalization", report)
    journal.event("session_finalization", report)
    journal.close()
    return report
