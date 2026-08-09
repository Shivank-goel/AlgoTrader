#!/usr/bin/env python3
"""Adaptive Crypto Trading System — CLI entry point."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click
import uvicorn
import yaml
from rich.console import Console
from rich.table import Table

console = Console()


def setup_logging(level: str = "INFO") -> None:
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/trader.log"),
        ],
    )


@click.group()
@click.option("--config", default="config", help="Config directory path")
@click.pass_context
def cli(ctx, config):
    """Adaptive Crypto Trading System for Delta Exchange India."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    setup_logging()


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
@click.option("--symbol", default="BTCUSDT")
@click.option("--start", default="2026-01-01")
@click.option("--end", default="2026-06-30")
@click.pass_context
def backtest(ctx, symbol, start, end):
    """Run a backtest on historical data."""
    console.print(f"[bold]Backtesting {symbol} from {start} to {end}[/bold]")
    console.print("[yellow]Backtest engine requires historical data download.[/yellow]")
    console.print("Use: python scripts/backtest.py --symbol BTCUSDT")


@cli.command()
@click.option("--symbol", default="BTCUSDT")
@click.option("--is-window", default=90, type=int)
@click.option("--oos-window", default=30, type=int)
@click.pass_context
def optimize(ctx, symbol, is_window, oos_window):
    """Run walk-forward optimization."""
    console.print(f"[bold]Optimizing {symbol} (IS={is_window}d, OOS={oos_window}d)[/bold]")
    console.print("Use: python scripts/optimize.py --symbol BTCUSDT")


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
