"""Explicit FYERS commands; no implicit fallback to the crypto engine."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click

from src.fyers.costs import FyersCosts
from src.fyers.journal import Journal
from src.fyers.models import ROOT, RuntimeConfig, Side
from src.fyers.qualification import qualification


@click.group()
def fyers() -> None:
    """FYERS NSE market recording, readiness and gated paper execution."""


@fyers.command()
@click.option("--seconds", type=click.FloatRange(min=1), default=60, show_default=True)
def record(seconds: float) -> None:
    """Record live market events for a bounded duration; no orders."""
    from src.fyers.runtime import observe
    try:
        click.echo(json.dumps(asyncio.run(observe(seconds)), indent=2))
    except Exception:
        raise click.ClickException("Recorder failed; check token, connectivity and configuration. No orders sent.") from None


@fyers.command()
@click.argument("intents", type=click.Path(exists=True, path_type=Path))
@click.option("--seconds", type=click.FloatRange(min=1), default=60)
def paper(intents: Path, seconds: float) -> None:
    """Simulate qualified entries or validated reductions of existing holdings."""
    from src.fyers.runtime import observe
    try:
        click.echo(json.dumps(asyncio.run(observe(seconds, intents)), indent=2))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None


@fyers.command()
def status() -> None:
    """Show deployment blockers and local evidence; never expose account data."""
    config = RuntimeConfig.load()
    passed, reason = qualification(ROOT / config.qualification_file, ROOT / config.trials_file)
    payload = {"broker": "fyers", "strategy_qualified": passed, "qualification": reason,
               "live_enabled": False, "live_blocker": "Production risk/reconciliation integration and deployment drills pending",
               "symbols": config.symbols}
    path = ROOT / config.database
    if path.exists():
        journal = Journal(path)
        try:
            payload["halt_reason"] = journal.get("halt_reason")
            payload["account_check"] = journal.get("account_check")
            payload["recorded_ticks"] = journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='tick'").fetchone()[0]
            payload["paper_fills"] = journal.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
            payload["paper_valuation"] = journal.get("paper_valuation")
            payload["strategy_lab"] = journal.get("strategy_lab_status")
        finally:
            journal.close()
    click.echo(json.dumps(payload, indent=2))


@fyers.command()
@click.argument("reason")
def halt(reason: str) -> None:
    """Persistently block new paper exposure; validated exits remain possible."""
    journal = Journal(ROOT / RuntimeConfig.load().database)
    try:
        journal.halt(reason)
    finally:
        journal.close()
    click.echo("Paper account halted. Existing positions are not liquidated.")


@fyers.command("backup")
@click.option("--output", type=click.Path(path_type=Path))
def backup(output: Path | None) -> None:
    """Create an integrity-checked SQLite backup; never overwrite a backup."""
    from datetime import datetime

    from src.fyers.operations import backup_database, prune_backups, verify_backup
    config = RuntimeConfig.load()
    destination = output or (ROOT / config.backup_directory /
                             f"runtime-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.sqlite3")
    try:
        backup_database(ROOT / config.database, destination)
        report = {"path": str(destination), **verify_backup(destination)}
        if output is None:
            report["pruned"] = prune_backups(destination.parent, retain=config.backup_retention_count)
        click.echo(json.dumps(report, indent=2))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None


@fyers.command("readiness")
def readiness_command() -> None:
    """Show alerts and release blockers; never enable live execution."""
    from src.fyers.operations import readiness
    click.echo(json.dumps(readiness(RuntimeConfig.load()), indent=2))


@fyers.command("preflight")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
@click.option("--strict", is_flag=True, help="Exit non-zero when a blocking check fails")
@click.option("--offline", is_flag=True, help="Skip FYERS authentication and outbound-IP checks")
@click.pass_context
def preflight_command(ctx, json_output: bool, strict: bool, offline: bool) -> None:
    """Audit VM/runtime readiness without enabling orders."""
    from src.fyers.operations import preflight
    result = asyncio.run(preflight(RuntimeConfig.load(), network=not offline))
    if json_output:
        click.echo(json.dumps(result, indent=2))
    else:
        for row in result["checks"]:
            click.echo(f"{'PASS' if row['passed'] else 'FAIL'} {row['name']}: {row['detail']}")
    if strict and not result["passed"]:
        ctx.exit(1)


@fyers.command("sync-daily")
@click.option("--start", "start_date", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--end", "end_date", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--days", type=click.IntRange(min=1, max=5000), default=550, show_default=True)
def sync_daily_command(start_date: datetime | None, end_date: datetime | None, days: int) -> None:
    """Backfill immutable completed FYERS daily bars; never fetch an incomplete day."""
    from dotenv import load_dotenv

    from src.execution.fyers import FyersClient
    from src.fyers.daily_data import sync_daily_history
    from src.fyers.models import environment_path
    from src.fyers.sessions import IST

    config = RuntimeConfig.load()
    now = datetime.now(IST)
    completed = now.date() if now.strftime("%H:%M") > config.session_end else now.date() - timedelta(days=1)
    end = end_date.date() if end_date else completed
    start = start_date.date() if start_date else end - timedelta(days=days - 1)
    load_dotenv(environment_path(), override=False)

    async def run() -> list:
        client = FyersClient.from_env()
        try:
            return await sync_daily_history(client, config, start=start, end=end)
        finally:
            await client.close()

    try:
        artifacts = asyncio.run(run())
    except Exception:
        raise click.ClickException("Daily-bar sync failed; check token, calendar and FYERS connectivity") from None
    click.echo(json.dumps({"start": start.isoformat(), "end": end.isoformat(),
                           "sessions": len(artifacts), "live_enabled": False}, indent=2))


@fyers.command("finalize-session")
@click.option("--date", "session_date", type=click.DateTime(formats=["%Y-%m-%d"]), required=True)
def finalize_session_command(session_date: datetime) -> None:
    """Finalize one completed session into bars, quality evidence and backup."""
    from src.fyers.operations import finalize_session
    result = asyncio.run(finalize_session(RuntimeConfig.load(), session_date.date()))
    click.echo(json.dumps(result, indent=2))
    if result["status"] != "complete":
        raise click.ClickException("Session finalization is incomplete; inspect the reported steps")


@fyers.command("restore-drill")
@click.option("--backup", "backup_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--output-directory", type=click.Path(path_type=Path), required=True)
def restore_drill_command(backup_path: Path, output_directory: Path) -> None:
    """Restore a backup into a new isolated directory and verify it."""
    from src.fyers.operations import restore_drill
    try:
        click.echo(json.dumps(restore_drill(backup_path, output_directory), indent=2))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None


@fyers.command()
@click.option("--notional", type=click.FloatRange(min=0, min_open=True), default=10000)
def costs(notional: float) -> None:
    """Published-tariff estimate, not a contract-note reconciliation."""
    model = FyersCosts.load()
    for delivery in (False, True):
        total = sum(model.fee(notional, side, delivery=delivery, charge_dp=delivery and side == Side.SELL) for side in Side)
        click.echo(f"{'Delivery' if delivery else 'Intraday'} round trip: INR {total:.4f}; excludes spread/slippage")


@fyers.command("reconcile-ledger")
@click.argument("report", type=click.Path(exists=True, path_type=Path))
@click.option("--tolerance-inr", type=click.FloatRange(min=0), default=1.0, show_default=True)
def reconcile_ledger_command(report: Path, tolerance_inr: float) -> None:
    """Import a normalized FYERS charge report read-only and compare modelled fees."""
    from src.fyers.ledger_import import reconcile_ledger_export

    config = RuntimeConfig.load()
    journal = Journal(ROOT / config.database)
    try:
        result = reconcile_ledger_export(report, journal, tolerance_inr=tolerance_inr)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        journal.close()
    click.echo(json.dumps(result, indent=2))


@fyers.command("quality")
@click.option("--start", type=float, required=True, help="Eligible window start: Unix seconds")
@click.option("--end", type=float, required=True, help="Eligible window end: Unix seconds")
def quality(start: float, end: float) -> None:
    """Replay recorded events read-only and report executable-quote coverage."""
    from src.data.quality import quote_quality
    from src.data.replay import read_events
    config = RuntimeConfig.load()
    events, digest = read_events(ROOT / config.database)
    result = quote_quality(events, config.symbols, start=start, end=end, stale_seconds=config.stale_seconds)
    click.echo(json.dumps({**result, "event_snapshot_sha256": digest}, indent=2))


@fyers.command("export-session")
@click.option("--start", type=float, required=True, help="Eligible window start: Unix seconds")
@click.option("--end", type=float, required=True, help="Eligible window end: Unix seconds")
@click.option("--output", type=click.Path(path_type=Path), required=True)
def export_session_command(start: float, end: float, output: Path) -> None:
    """Export one immutable, bounded FYERS session for replay."""
    from src.data.session import export_session
    config = RuntimeConfig.load()
    try:
        artifact = export_session(ROOT / config.database, output, start=start, end=end,
                                  symbols=config.symbols, stale_seconds=config.stale_seconds)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(json.dumps({"path": str(output), "session_sha256": artifact.session_sha256,
                           "quality": artifact.quality}, indent=2))


@fyers.command("replay")
@click.argument("intents_path", type=click.Path(exists=True, path_type=Path))
@click.option("--output-directory", type=click.Path(path_type=Path), required=True)
@click.option("--session-export", type=click.Path(exists=True, path_type=Path))
def replay(intents_path: Path, output_directory: Path, session_export: Path | None) -> None:
    """Replay recorded quotes into a NEW gated shadow ledger; never send orders."""
    from src.data.replay import read_events
    from src.fyers.models import Instrument, Intent
    from src.fyers.paper import PaperBroker
    from src.shadow.runtime import replay_session
    config = RuntimeConfig.load()
    if session_export:
        from src.data.session import load_session
        try:
            artifact = load_session(session_export)
        except ValueError as exc:
            raise click.ClickException(f"Invalid session export: {exc}") from None
        events, digest = artifact.events, artifact.session_sha256
        instruments = artifact.instruments
    else:
        events, digest = read_events(ROOT / config.database)
        masters = [e["data"] for e in events if e["kind"] == "instruments"]
        if not masters or any(master != masters[0] for master in masters):
            raise click.ClickException("Replay requires one consistent recorded instrument master; use --session-export")
        instruments = {s: Instrument.model_validate(row) for s, row in masters[0].items()}
    intents = [Intent.model_validate(row) for row in json.loads(intents_path.read_text())]
    if output_directory.exists():
        raise click.ClickException("Output directory already exists; never reuse a shadow ledger")
    output_directory.mkdir(parents=True)
    journal = Journal(output_directory / "shadow.sqlite3")
    try:
        result = replay_session(events, instruments, intents, PaperBroker(journal, config, FyersCosts.load()))
        journal.event("replay_provenance", {"event_snapshot_sha256": digest})
        click.echo(json.dumps({**result, "event_snapshot_sha256": digest}, indent=2))
    finally:
        journal.close()
