#!/usr/bin/env python3
"""Adaptive Crypto Trading System — CLI entry point."""

from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import click
import uvicorn
import yaml
from rich.console import Console
from rich.table import Table

console = Console()

DEFAULT_LOG_FILE = "logs/trader.log"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5


def _load_logging_config(config_dir: str) -> dict:
    """Read the `logging:` block from settings.yaml, tolerating a missing file."""
    try:
        with open(Path(config_dir) / "settings.yaml") as fh:
            return (yaml.safe_load(fh) or {}).get("logging", {}) or {}
    except (OSError, yaml.YAMLError):
        return {}


def setup_logging(config_dir: str = "config") -> None:
    """Configure console + size-rotated file logging from settings.yaml.

    The rotation settings have always been present in config/settings.yaml but
    were never read, so logs/trader.log grew unbounded (343 MB when found).
    """
    cfg = _load_logging_config(config_dir)
    level = str(cfg.get("level", "INFO"))
    log_file = Path(cfg.get("file", DEFAULT_LOG_FILE))
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                log_file,
                maxBytes=int(cfg.get("max_bytes", DEFAULT_LOG_MAX_BYTES)),
                backupCount=int(cfg.get("backup_count", DEFAULT_LOG_BACKUP_COUNT)),
            ),
        ],
    )


@click.group()
@click.option("--config", default="config", help="Config directory path")
@click.pass_context
def cli(ctx, config):
    """Adaptive Crypto Trading System for Delta Exchange India."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    setup_logging(config)


@cli.command()
@click.pass_context
def run(ctx):
    """Start the trading engine."""
    from src.core.engine import TradingEngine

    engine = TradingEngine(config_dir=ctx.obj["config"])
    console.print("[bold green]Starting trading engine...[/bold green]")

    async def main():
        try:
            engine.setup_signal_handlers()
            await engine.start()
        except KeyboardInterrupt:
            await engine.stop()

    asyncio.run(main())


@cli.command()
@click.pass_context
def paper(ctx):
    """Start paper trading mode (testnet)."""
    from src.core.engine import TradingEngine

    engine = TradingEngine(config_dir=ctx.obj["config"], paper=True)
    console.print("[bold yellow]Paper trading mode enabled (testnet)[/bold yellow]")
    console.print("[bold green]Starting trading engine...[/bold green]")

    async def main():
        try:
            engine.setup_signal_handlers()
            await engine.start()
        except KeyboardInterrupt:
            await engine.stop()

    asyncio.run(main())


@cli.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8080, type=int)
@click.pass_context
def trade(ctx, host, port):
    """Launch dashboard (engine starts via UI Start button)."""
    from src.core.engine import TradingEngine
    from src.dashboard.app import create_app

    engine = TradingEngine(config_dir=ctx.obj["config"])
    app = create_app(engine)
    console.print(f"[bold green]Dashboard at http://{host}:{port}[/bold green]")
    console.print("[dim]Engine is idle — click Start in the UI to begin scanning[/dim]")
    uvicorn.run(app, host=host, port=port)


@cli.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8080, type=int)
@click.pass_context
def dashboard(ctx, host, port):
    """Start the web dashboard."""
    from src.dashboard.app import create_app

    app = create_app()
    console.print(f"[bold blue]Dashboard at http://{host}:{port}[/bold blue]")
    uvicorn.run(app, host=host, port=port)


@cli.command()
@click.pass_context
def status(ctx):
    """Show current portfolio status."""
    from src.core.engine import TradingEngine

    engine = TradingEngine(config_dir=ctx.obj["config"])
    portfolio = engine.portfolio.get_snapshot()

    table = Table(title="Portfolio Status")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Equity", f"${portfolio.equity:,.2f}")
    table.add_row("Unrealized P&L", f"${portfolio.unrealized_pnl:,.2f}")
    table.add_row("Daily P&L", f"${portfolio.realized_pnl_today:,.2f}")
    table.add_row("Drawdown", f"{portfolio.drawdown_pct:.1f}%")
    table.add_row("Open Positions", str(len(portfolio.open_positions)))
    table.add_row("Regime", engine.regime_detector.current_regime.value)

    console.print(table)


@cli.command()
@click.option("--symbol", default="BTCUSD")
@click.option("--start", default="2026-01-01")
@click.option("--end", default="2026-06-30")
@click.pass_context
def backtest(ctx, symbol, start, end):
    """Run a backtest on historical data."""
    console.print(f"[bold]Backtesting {symbol} from {start} to {end}[/bold]")
    console.print("[yellow]Backtest engine requires historical data download.[/yellow]")
    console.print("Use: python scripts/backtest.py --symbol BTCUSDT")


@cli.command()
@click.option("--symbol", default="BTCUSD")
@click.option("--is-window", default=90, type=int)
@click.option("--oos-window", default=30, type=int)
@click.pass_context
def optimize(ctx, symbol, is_window, oos_window):
    """Run walk-forward optimization."""
    console.print(f"[bold]Optimizing {symbol} (IS={is_window}d, OOS={oos_window}d)[/bold]")
    console.print("Use: python scripts/optimize.py --symbol BTCUSDT")


@cli.command("fetch-history")
@click.option("--symbol", "symbols", multiple=True, help="Repeatable; default BTCUSD ETHUSD SOLUSD")
@click.option("--timeframe", "timeframes", multiple=True, help="Repeatable; default 15m 1h 4h")
@click.option("--days", default=365, type=int, help="How far back to pull")
@click.option("--prod/--testnet", default=True, help="Pull from production (real prices)")
@click.pass_context
def fetch_history(ctx, symbols, timeframes, days, prod):
    """Backfill real OHLCV history into data/hist/*.parquet.

    Defaults to production because the candles endpoint is public — real
    history must not require flipping exchange.testnet, and testnet prices are
    synthetic (frozen tails, flat bars, PAXGUSD at $0.01).
    """
    from src.data.manager import DataManager
    from src.execution.exchange import DeltaExchangeClient

    symbols = symbols or ("BTCUSD", "ETHUSD", "SOLUSD")
    timeframes = timeframes or ("15m", "1h", "4h")

    with open(Path(ctx.obj["config"]) / "settings.yaml") as fh:
        exchange_cfg = (yaml.safe_load(fh) or {})["exchange"]
    base_url = exchange_cfg["base_url_prod"] if prod else exchange_cfg["base_url_testnet"]

    # No credentials: /v2/history/candles is a public endpoint.
    client = DeltaExchangeClient(api_key="", api_secret="", base_url=base_url, testnet=not prod)
    manager = DataManager(client)

    console.print(
        f"[bold]Backfilling {days}d[/bold] from "
        f"[cyan]{'production' if prod else 'testnet'}[/cyan] ({base_url})"
    )

    table = Table(title="Backfilled History")
    for col in ("Symbol", "TF", "Bars", "Days", "Start", "End", "Flat %"):
        table.add_column(col, style="cyan" if col == "Symbol" else None)

    async def main():
        try:
            for symbol in symbols:
                for tf in timeframes:
                    df = await manager.backfill(symbol, tf, days=days)
                    info = manager.describe_history(df)
                    if not info["bars"]:
                        table.add_row(symbol, tf, "0", "-", "-", "-", "-")
                        continue
                    flat_pct = info["flat_pct"]
                    table.add_row(
                        symbol,
                        tf,
                        str(info["bars"]),
                        str(info["days"]),
                        str(info["start"].date()),
                        str(info["end"].date()),
                        f"[red]{flat_pct}[/red]" if flat_pct > 5 else str(flat_pct),
                    )
        finally:
            await client.close()

    asyncio.run(main())
    console.print(table)
    console.print("[dim]A high flat-bar % means a synthetic or dead feed.[/dim]")


@cli.command("microstructure")
@click.option("--once", is_flag=True, help="Take a single snapshot and exit")
@click.option("--interval", default=300, type=int, help="Seconds between snapshots")
@click.option("--stats", is_flag=True, help="Show what has been collected and exit")
@click.option("--min-turnover", default=1_000_000.0, type=float)
@click.pass_context
def microstructure(ctx, once, interval, stats, min_turnover):
    """Record funding rate, open interest and basis into data/microstructure.parquet.

    Records only — no features, no signals. These fields have no history on the
    exchange, so a strategy needing six months of funding data can only start
    accumulating it today.
    """
    from src.market.microstructure import MicrostructureRecorder, MicrostructureSnapshot

    recorder = MicrostructureRecorder()

    if stats:
        info = recorder.stats()
        table = Table(title="Recorded Microstructure")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        for key, value in info.items():
            table.add_row(key, str(value))
        console.print(table)
        return

    with open(Path(ctx.obj["config"]) / "settings.yaml") as fh:
        exchange_cfg = (yaml.safe_load(fh) or {})["exchange"]

    async def snapshot_once(client) -> int:
        result = await client._request(
            "GET", "/v2/tickers", params={"contract_types": "perpetual_futures"}
        )
        rows = result if isinstance(result, list) else result.get("result", [])
        snaps = [
            MicrostructureSnapshot.from_ticker(t.get("symbol", ""), t)
            for t in rows
            if float(t.get("turnover_usd") or 0) >= min_turnover
        ]
        recorder.record(snaps)
        recorder.flush()
        return len(snaps)

    async def main():
        from src.execution.exchange import DeltaExchangeClient

        client = DeltaExchangeClient(
            api_key="", api_secret="",
            base_url=exchange_cfg["base_url_prod"], testnet=False,
        )
        try:
            if once:
                n = await snapshot_once(client)
                console.print(f"[green]recorded {n} symbol snapshot(s)[/green]")
                return
            console.print(f"[dim]Recording every {interval}s — Ctrl-C to stop[/dim]")
            while True:
                n = await snapshot_once(client)
                console.print(f"[dim]{n} symbols recorded[/dim]")
                await asyncio.sleep(interval)
        finally:
            recorder.flush()
            await client.close()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Recorder stopped[/yellow]")


@cli.command("news")
@click.option("--once", is_flag=True, help="Poll a single time and exit")
@click.option("--interval", default=900, type=int, help="Seconds between polls")
@click.option("--stats", is_flag=True, help="Show what has been collected and exit")
@click.pass_context
def news(ctx, once, interval, stats):
    """Collect crypto headlines into data/trades.db.

    Runs independently of the trading engine. Start it early: no free source
    backfills history, so the training set only accumulates from first run.
    """
    from src.news.poller import NewsPoller
    from src.news.store import NewsStore

    if stats:
        store = NewsStore()
        by_source = store.stats()
        by_asset = store.asset_stats()

        table = Table(title="Collected News")
        table.add_column("Bucket", style="cyan")
        table.add_column("Items", style="green", justify="right")
        table.add_row("TOTAL", str(by_source.pop("total", 0)))
        for name, count in by_source.items():
            table.add_row(f"source: {name}", str(count))
        for asset, count in by_asset.items():
            table.add_row(f"asset: {asset}", str(count))
        console.print(table)
        return

    poller = NewsPoller(interval_seconds=interval)
    source_names = ", ".join(s.name for s in poller.sources)
    console.print(f"[bold green]News poller[/bold green] sources: {source_names}")

    async def main():
        import aiohttp

        if once:
            async with aiohttp.ClientSession() as session:
                inserted = await poller.poll_once(session)
            console.print(f"[green]{inserted} new item(s) stored[/green]")
            return

        console.print(f"[dim]Polling every {interval}s — Ctrl-C to stop[/dim]")
        await poller.run()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]News poller stopped[/yellow]")


@cli.command()
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def resume(ctx, yes):
    """Manually resume trading after a circuit breaker halt."""
    import getpass
    import socket

    from src.core.engine import TradingEngine

    engine = TradingEngine(config_dir=ctx.obj["config"])
    halt_status = engine.circuit_breaker.get_halt_status()

    if not halt_status["halted"]:
        console.print("[yellow]Circuit breaker is not halted — nothing to resume.[/yellow]")
        return

    operator = f"{getpass.getuser()}@{socket.gethostname()}"
    reason = halt_status.get("reason") or "unknown"
    halted_at = halt_status.get("halted_at") or "unknown"

    console.print("[bold red]Circuit breaker is ACTIVE[/bold red]")
    console.print(f"  Reason:    {reason}")
    console.print(f"  Halted at: {halted_at}")
    console.print(f"  Operator:  {operator}")

    if not yes and not click.confirm(
        "Resume trading? This requires explicit human approval.",
        default=False,
    ):
        console.print("[dim]Resume aborted.[/dim]")
        raise SystemExit(1)

    if engine.circuit_breaker.manual_resume(operator):
        console.print(
            f"[bold green]Circuit breaker resumed by {operator}[/bold green]"
        )
        console.print(
            "[dim]Audit entry written to logs/circuit_breaker_audit.log[/dim]"
        )
    else:
        console.print("[red]Resume failed — circuit breaker is not halted.[/red]")
        raise SystemExit(1)


@cli.command()
@click.option("--accept", is_flag=True, help="Accept discrepancies and allow engine to start")
@click.pass_context
def reconcile(ctx, accept):
    """Check or accept reconciliation discrepancies after a crash.

    Without --accept: shows current persisted state and exchange positions.
    With --accept: clears the persisted state file so the engine adopts exchange truth on next start.
    """
    from src.core.reconciler import PositionStateStore

    store = PositionStateStore()
    local_state = store.load()

    if not local_state:
        console.print("[green]No persisted position state found — clean start.[/green]")
        return

    table = Table(title="Persisted Local State")
    table.add_column("Symbol", style="cyan")
    table.add_column("Side")
    table.add_column("Size")
    table.add_column("Entry")
    table.add_column("SL")
    table.add_column("TP")
    table.add_column("Strategy")

    for symbol, data in local_state.items():
        table.add_row(
            symbol,
            data.get("side", "?"),
            f"{data.get('size', 0):.4f}",
            f"{data.get('entry_price', 0):.2f}",
            f"{data.get('stop_loss', 'N/A')}",
            f"{data.get('take_profit', 'N/A')}",
            data.get("strategy_name", "unknown"),
        )

    console.print(table)

    if accept:
        store.clear()
        console.print(
            "[bold green]Persisted state cleared. Engine will adopt exchange positions "
            "as truth on next start without halting.[/bold green]"
        )
    else:
        console.print(
            "\n[yellow]To accept discrepancies and allow a clean start, run:[/yellow]"
        )
        console.print("  python main.py reconcile --accept")


if __name__ == "__main__":
    cli()
