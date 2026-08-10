"""Main async trading engine — orchestrates the full pipeline."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections import deque
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import os
import yaml
from dotenv import load_dotenv

from src.backtest.costs import CostModel
from src.core.events import (
    CircuitBreakerEvent,
    EventBus,
    FillEvent,
    RegimeChangeEvent,
    SignalEvent,
    TradeClosedEvent,
)
from src.core.models import Direction, OrderSide, Position, Regime, SignalAction
from src.core.reconciler import (
    PositionStateStore,
    ReconciliationResult,
    StartupReconciler,
)
from src.data.manager import DataManager
from src.data.websocket_client import WebSocketClient
from src.execution.exchange import DeltaExchangeClient, PositionCloseError, _extract_ticker_price
from src.execution.order_manager import OrderManager
from src.learning.adapter import ParameterAdapter
from src.learning.scorer import PerformanceScorer
from src.market.regime import RegimeDetector
from src.market.scanner import SymbolScanner
from src.market.state import MarketStateBuilder
from src.market.universe import UniverseManager
from src.notifications.telegram import TelegramNotifier
from src.portfolio.journal import TradeJournal
from src.portfolio.manager import PortfolioManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.manager import RiskManager
from src.risk.position_sizer import PositionSizer
from src.strategies.selector import StrategySelector

logger = logging.getLogger(__name__)

DEFAULT_TIMEFRAMES = {"signal": "15m", "trend": "1h", "context": "4h"}
VALID_MODES = {"demo", "live"}
DEFAULT_MAX_LOOP_TIMEOUT = 600
DEFAULT_WATCHDOG_INTERVAL = 120
DEFAULT_MAX_SYNC_FAILURES = 5


class TradingEngine:
    """Async event-driven trading engine with UI-controlled lifecycle."""

    def __init__(self, config_dir: str = "config", *, paper: bool = False) -> None:
        load_dotenv()
        self.config_dir = Path(config_dir)
        self.settings = self._load_yaml("settings.yaml")
        if paper:
            self.settings.setdefault("trading", {})["mode"] = "paper"
            self.settings.setdefault("exchange", {})["testnet"] = True
        self.risk_config = self._load_yaml("risk.yaml")
        self.strategy_config = self._load_yaml("strategies.yaml")

        self.event_bus = EventBus()
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._event_bus_started = False
        self._exchange_connected = False
        self._consecutive_sync_failures = 0
        self._last_daily_reset: Optional[datetime] = None

        self._active_pairs: list[dict[str, Any]] = deepcopy(
            self.settings.get("trading", {}).get("pairs", [])
        )
        self._mode: str = "demo" if self.settings["exchange"].get("testnet", True) else "live"
        self._heartbeat: dict[str, Any] = {
            "state": "idle",
            "mode": self._mode,
            "loop_count": 0,
            "last_scan_at": None,
            "last_scan_symbols": [],
            "started_at": None,
            "errors": 0,
        }
        self._signal_log: deque = deque(maxlen=100)
        self._symbol_snapshots: dict[str, dict[str, Any]] = {}
        self._universe_stop_event: Optional[asyncio.Event] = None
        self._universe_task: Optional[asyncio.Task] = None
        self._initial_rescan_task: Optional[asyncio.Task] = None
        self._loop_iteration_start: Optional[datetime] = None

        self._init_components()

    def _load_yaml(self, filename: str) -> dict[str, Any]:
        path = self.config_dir / filename
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Config file not found: {path}") from None
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(
                f"Config file {path} must contain a YAML mapping, got {type(data).__name__}"
            )
        return data

    def _init_components(self) -> None:
        testnet = self._mode == "demo"
        exchange_cfg = self.settings["exchange"]
        base_url = (
            exchange_cfg["base_url_testnet"]
            if testnet
            else exchange_cfg["base_url_prod"]
        )

        self.exchange = DeltaExchangeClient(
            api_key=os.getenv("DELTA_API_KEY", ""),
            api_secret=os.getenv("DELTA_API_SECRET", ""),
            base_url=base_url,
            testnet=testnet,
        )

        trading_mode = self.settings.get("trading", {}).get("mode", "live")
        paper_mode = trading_mode == "paper"
        # One cost model shared by paper execution and the backtester, so a
        # paper result is comparable to a backtested one.
        self.cost_model = CostModel.from_config(self.risk_config)
        self.data_manager = DataManager(self.exchange)
        self.regime_detector = RegimeDetector()
        self.market_builder = MarketStateBuilder(regime_detector=self.regime_detector)
        self.portfolio = PortfolioManager(initial_equity=0.0)
        self.journal = TradeJournal()
        selector_cfg = self.strategy_config.get("selector", {})
        self.scorer = PerformanceScorer(
            lookback_trades=int(selector_cfg.get("performance_lookback_trades", 200)),
            min_trades=int(selector_cfg.get("min_backtest_trades", 10)),
        )
        self.param_adapter = ParameterAdapter(self.strategy_config)

        scores = self.scorer.score_all(self.journal.get_trades_by_strategy())
        self.strategy_selector = StrategySelector(self.strategy_config, scores)

        self.position_sizer = PositionSizer(self.risk_config)
        self.circuit_breaker = CircuitBreaker(self.risk_config, self.event_bus)
        self.risk_manager = RiskManager(
            self.risk_config, self.position_sizer, self.circuit_breaker
        )
        self.order_manager = OrderManager(
            self.exchange, self.event_bus, paper_mode, cost_model=self.cost_model
        )
        self.notifier = TelegramNotifier(
            enabled=self.settings.get("notifications", {}).get("telegram_enabled", False)
        )
        self.order_manager.set_alert_callback(self.notifier.send_alert)
        self.ws_client = WebSocketClient(testnet=testnet)
        self.position_state_store = PositionStateStore()
        self.reconciler = StartupReconciler(
            exchange_client=self.exchange,
            state_store=self.position_state_store,
        )

        universe_cfg = self.settings.get("universe", {})
        self.scanner = SymbolScanner(
            self.exchange,
            top_n=int(universe_cfg.get("scan_top_n", 50)),
            quote_currencies=tuple(universe_cfg.get("quote_currencies", ["USD", "USDT"])),
            min_turnover_usd=float(universe_cfg.get("min_turnover_usd", 1_000_000)),
            excluded_underlyings=tuple(universe_cfg.get("excluded_underlyings", [])),
        )
        self.universe = UniverseManager(
            scanner=self.scanner,
            data_manager=self.data_manager,
            strategies=self.strategy_selector.get_all_strategies(),
            top_active=int(universe_cfg.get("top_active", 10)),
            history_timeframe=universe_cfg.get("history_timeframe", "15m"),
            history_bars=int(universe_cfg.get("history_bars", 3000)),
            rescan_interval_hours=float(universe_cfg.get("rescan_interval_hours", 6)),
            max_concurrent_backtests=int(universe_cfg.get("max_concurrent_backtests", 4)),
            min_backtest_trades=int(selector_cfg.get("min_backtest_trades", 10)),
            reassignment_margin=float(selector_cfg.get("reassignment_margin", 0.2)),
            journal=self.journal,
        )

        self._setup_event_handlers()

    def _setup_event_handlers(self) -> None:
        for event_type, handler in (
            (CircuitBreakerEvent, self._on_circuit_breaker),
            (FillEvent, self._on_fill),
            (RegimeChangeEvent, self._on_regime_change),
        ):
            self.event_bus.unsubscribe(event_type, handler)
            self.event_bus.subscribe(event_type, handler)

    def _log_signal(
        self,
        symbol: str,
        status: str,
        message: str,
        strategy: Optional[str] = None,
        direction: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> None:
        self._signal_log.appendleft({
            "timestamp": datetime.utcnow().isoformat(),
            "symbol": symbol,
            "status": status,
            "message": message,
            "strategy": strategy,
            "direction": direction,
            "confidence": confidence,
        })

    def get_signal_log(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._signal_log)[:limit]

    def get_active_pairs(self) -> list[dict[str, Any]]:
        return deepcopy(self._active_pairs)

    def add_pair(self, symbol: str, timeframes: Optional[dict[str, str]] = None) -> bool:
        symbol = symbol.upper()
        if any(p["symbol"] == symbol for p in self._active_pairs):
            return False
        self._active_pairs.append({
            "symbol": symbol,
            "timeframes": timeframes or deepcopy(DEFAULT_TIMEFRAMES),
        })
        if self._running:
            tf = (timeframes or DEFAULT_TIMEFRAMES).get("signal", "15m")
            self.ws_client.subscribe_candles(symbol, tf)
        return True

    def remove_pair(self, symbol: str) -> bool:
        symbol = symbol.upper()
        before = len(self._active_pairs)
        self._active_pairs = [p for p in self._active_pairs if p["symbol"] != symbol]
        self._symbol_snapshots.pop(symbol, None)
        return len(self._active_pairs) < before

    def get_symbol_snapshot(self, symbol: str) -> Optional[dict[str, Any]]:
        return self._symbol_snapshots.get(symbol.upper())

    def _current_price_for(self, symbol: str) -> Optional[float]:
        snap = self._symbol_snapshots.get(symbol.upper())
        if snap and snap.get("price") is not None:
            return float(snap["price"])
        return None

    def _serialize_position(self, position: Position) -> dict[str, Any]:
        current_price = self._current_price_for(position.symbol)
        entry = position.entry_price
        stop_loss = position.stop_loss
        take_profit = position.take_profit

        sl_distance_pct = None
        tp_distance_pct = None
        progress_pct = None

        if current_price and entry:
            if position.side == Direction.LONG:
                if stop_loss:
                    sl_distance_pct = ((current_price - stop_loss) / current_price) * 100
                if take_profit:
                    tp_distance_pct = ((take_profit - current_price) / current_price) * 100
                if stop_loss and take_profit and take_profit != stop_loss:
                    progress_pct = max(
                        0.0,
                        min(100.0, ((current_price - stop_loss) / (take_profit - stop_loss)) * 100),
                    )
            else:
                if stop_loss:
                    sl_distance_pct = ((stop_loss - current_price) / current_price) * 100
                if take_profit:
                    tp_distance_pct = ((current_price - take_profit) / current_price) * 100
                if stop_loss and take_profit and stop_loss != take_profit:
                    progress_pct = max(
                        0.0,
                        min(100.0, ((stop_loss - current_price) / (stop_loss - take_profit)) * 100),
                    )

        return {
            "symbol": position.symbol,
            "side": position.side.value,
            "size": position.size,
            "entry_price": entry,
            "current_price": current_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "sl_distance_pct": round(sl_distance_pct, 2) if sl_distance_pct is not None else None,
            "tp_distance_pct": round(tp_distance_pct, 2) if tp_distance_pct is not None else None,
            "progress_pct": round(progress_pct, 1) if progress_pct is not None else None,
            "unrealized_pnl": position.unrealized_pnl,
            "strategy_name": position.strategy_name,
            "opened_at": position.opened_at.isoformat() if position.opened_at else None,
        }

    def get_positions_view(self) -> list[dict[str, Any]]:
        views: list[dict[str, Any]] = []
        for position in self.portfolio.positions:
            try:
                views.append(self._serialize_position(position))
            except Exception:
                logger.exception("Failed to serialize position for %s", position.symbol)
        return views

    @property
    def mode(self) -> str:
        return self._mode

    async def set_mode(self, mode: str) -> dict[str, str]:
        mode = mode.lower()
        if mode not in VALID_MODES:
            return {"status": "error", "message": f"Invalid mode '{mode}'. Use demo or live."}
        if mode == self._mode:
            return {"status": "unchanged", "mode": mode, "message": f"Already in {mode} mode"}

        if self._running:
            await self.stop_background()
        if self._exchange_connected:
            try:
                await self.exchange.close()
            except Exception:
                logger.exception("Exchange close failed during mode switch")
            self._exchange_connected = False

        self._mode = mode
        self.settings["exchange"]["testnet"] = mode == "demo"
        self._init_components()
        self._heartbeat["mode"] = mode
        self._heartbeat["state"] = "idle"
        self._heartbeat["last_scan_at"] = None
        self._heartbeat["last_scan_symbols"] = []
        self._symbol_snapshots.clear()
        logger.info("Engine switched to %s mode", mode)
        return {"status": "ok", "mode": mode, "message": f"Switched to {mode} mode"}

    async def rescan_universe(self) -> dict[str, Any]:
        if not self._exchange_connected:
            try:
                await self.exchange.connect()
                self._exchange_connected = True
            except Exception:
                logger.exception("Exchange connect failed during rescan")
        fallback = [p["symbol"] for p in self._active_pairs]
        result = await self.universe.rescan(fallback_symbols=fallback)
        self._sync_active_pairs_with_universe()
        return result

    def _sync_active_pairs_with_universe(self) -> None:
        top = self.universe.get_active_symbols()
        if not top:
            return
        existing = {p["symbol"]: p for p in self._active_pairs}
        new_pairs: list[dict[str, Any]] = []
        for symbol in top:
            if symbol in existing:
                new_pairs.append(existing[symbol])
            else:
                new_pairs.append({
                    "symbol": symbol,
                    "timeframes": deepcopy(DEFAULT_TIMEFRAMES),
                })
        self._active_pairs = new_pairs

    def get_heartbeat(self) -> dict[str, Any]:
        hb = deepcopy(self._heartbeat)
        uptime_seconds = None
        if hb.get("started_at"):
            started = datetime.fromisoformat(hb["started_at"])
            uptime_seconds = int((datetime.utcnow() - started).total_seconds())
        hb["uptime_seconds"] = uptime_seconds
        hb["mode"] = self._mode
        hb["active_pairs"] = [p["symbol"] for p in self._active_pairs]
        interval = self._scan_interval_seconds()
        hb["loop_interval_seconds"] = interval
        hb["scan_interval_seconds"] = interval
        stale = False
        if hb["state"] == "running" and hb.get("last_scan_at"):
            last = datetime.fromisoformat(hb["last_scan_at"])
            elapsed = (datetime.utcnow() - last).total_seconds()
            stale = elapsed > interval * 2
        hb["stale"] = stale
        hb["universe_last_scan_at"] = (
            self.universe.last_scan_at.isoformat() if self.universe.last_scan_at else None
        )
        hb["circuit_breaker"] = self.circuit_breaker.get_halt_status()
        hb["halted_symbols"] = list(self.order_manager.get_halted_symbols())
        return hb

    def get_universe_snapshot(self) -> dict[str, Any]:
        return self.universe.snapshot()

    # --- Finding #14: daily equity reset ---
    def _maybe_reset_daily(self) -> None:
        now = datetime.utcnow()
        if self._last_daily_reset is None or self._last_daily_reset.date() < now.date():
            self.circuit_breaker.reset_daily()
            self.portfolio.reset_daily_pnl()
            self._last_daily_reset = now
            logger.info("Daily risk counters reset")

    async def _publish_pending_cb_events(self) -> None:
        for take in (
            self.circuit_breaker.take_pending_event,
            self.risk_manager.take_pending_cb_event,
        ):
            pending = take()
            if pending is not None:
                await self.event_bus.publish(pending)

    async def start_background(self) -> dict[str, str]:
        if self._running or (self._task and not self._task.done()):
            return {"status": "already_running", "message": "Engine is already running"}

        self._shutdown_event = asyncio.Event()
        self._running = True
        self._heartbeat["state"] = "running"
        self._heartbeat["mode"] = self._mode
        self._heartbeat["started_at"] = datetime.utcnow().isoformat()
        self._heartbeat["loop_count"] = 0
        self._heartbeat["errors"] = 0
        self._consecutive_sync_failures = 0

        self.circuit_breaker.record_api_success()

        if not self._exchange_connected:
            await self.exchange.connect()
            self._exchange_connected = True

        connectivity = await self._verify_exchange_connectivity()
        if not connectivity["ok"]:
            message = connectivity["message"]
            logger.warning(
                "Exchange unreachable at startup — engine will scan with cached data: %s",
                message,
            )
            self._heartbeat["state"] = "degraded"
            await self.notifier.send_alert(
                "Engine started in scan-only mode (exchange unreachable). "
                "Opportunity scans continue; orders resume when connectivity returns."
            )
        elif self.circuit_breaker.resume_on_connectivity_restore("engine_startup"):
            logger.info("Circuit breaker cleared after successful connectivity check")
            self._heartbeat["state"] = "running"

        if not self._event_bus_started:
            await self.event_bus.start()
            self._event_bus_started = True

        # Startup reconciliation: verify local state matches exchange before trading
        if not self.order_manager.paper_mode:
            self.reconciler._alert_callback = self.notifier.send_alert
            reconciliation = await self._run_startup_reconciliation()
            if reconciliation and reconciliation.has_critical:
                self._running = False
                self._heartbeat["state"] = "halted_reconciliation"
                message = (
                    "Engine halted due to reconciliation discrepancies. "
                    "Review the logs and run `python main.py reconcile --accept` to resume."
                )
                logger.critical(message)
                await self.notifier.send_alert(message)
                return {
                    "status": "halted",
                    "mode": self._mode,
                    "message": message,
                    "discrepancies": [
                        {"symbol": d.symbol, "kind": d.kind, "details": d.details}
                        for d in reconciliation.discrepancies
                    ],
                }

        self._universe_stop_event = asyncio.Event()
        self._universe_task = asyncio.create_task(
            self.universe.start_periodic_rescan(
                self._universe_stop_event,
                fallback_provider=lambda: [p["symbol"] for p in self._active_pairs],
            )
        )

        try:
            await asyncio.wait_for(self._initial_rescan(), timeout=300)
        except asyncio.TimeoutError:
            logger.warning("Initial universe rescan timed out — starting with available pairs")
        except Exception:
            logger.exception("Initial universe rescan failed")

        for pair in self._active_pairs:
            tf = pair.get("timeframes", DEFAULT_TIMEFRAMES).get("signal", "15m")
            try:
                self.ws_client.subscribe_candles(pair["symbol"], tf)
            except Exception:
                logger.debug("WS subscribe failed for %s", pair["symbol"])

        self._task = asyncio.create_task(self._background_run())
        self._watchdog_task = asyncio.create_task(self._watchdog())
        logger.info("Trading engine started (mode=%s, active pairs=%d)", self._mode, len(self._active_pairs))
        await self.notifier.send_alert(f"Trading engine started ({self._mode} mode)")
        return {
            "status": "started",
            "mode": self._mode,
            "message": f"Engine is now scanning ({self._mode} mode)",
        }

    async def _verify_exchange_connectivity(self) -> dict[str, Any]:
        """Verify the exchange API is reachable before entering the trading loop."""
        base_url = self.exchange.base_url
        try:
            products = await self.exchange.get_products()
            if not products:
                return {
                    "ok": False,
                    "message": (
                        f"Exchange at {base_url} returned no products. "
                        "Check API keys and network connectivity."
                    ),
                }
            logger.info("Exchange connectivity OK (%d products)", len(products))
            return {"ok": True, "message": "Exchange reachable"}
        except Exception as exc:
            return {
                "ok": False,
                "message": (
                    f"Cannot reach Delta Exchange at {base_url}: {exc}. "
                    "Start the dashboard from Terminal.app (not Cursor's sandbox) "
                    "and ensure DNS/network access is available."
                ),
            }

    async def _initial_rescan(self) -> None:
        try:
            fallback = [p["symbol"] for p in self._active_pairs]
            await self.universe.rescan(fallback_symbols=fallback)
            self._sync_active_pairs_with_universe()
            for pair in self._active_pairs:
                tf = pair.get("timeframes", DEFAULT_TIMEFRAMES).get("signal", "15m")
                try:
                    self.ws_client.subscribe_candles(pair["symbol"], tf)
                except Exception:
                    logger.debug("WS subscribe failed for %s", pair["symbol"])
        except Exception:
            logger.exception("Initial universe rescan failed")

    async def _run_startup_reconciliation(self) -> Optional[ReconciliationResult]:
        """Run reconciliation against exchange; adopt positions or halt on discrepancy."""
        try:
            result = await self.reconciler.reconcile()
        except Exception:
            logger.exception("Startup reconciliation failed unexpectedly")
            error_result = ReconciliationResult(
                success=False,
                message="Reconciliation raised an unexpected exception",
            )
            # Treat unhandled reconciliation failures as critical
            from src.core.reconciler import Discrepancy
            error_result.discrepancies.append(Discrepancy(
                symbol="SYSTEM",
                kind="reconciliation_error",
                details="Reconciliation raised an unhandled exception — see logs",
                severity="critical",
            ))
            return error_result

        if not result.success:
            return result

        if result.has_critical:
            return result

        # Apply reconciled positions to portfolio
        if result.adopted_positions:
            self.portfolio._positions = result.adopted_positions
            self._persist_position_state()
            logger.info(
                "Reconciliation adopted %d position(s) into portfolio",
                len(result.adopted_positions),
            )

        return result

    async def stop_background(self) -> dict[str, str]:
        if not self._running and not (self._task and not self._task.done()):
            self._heartbeat["state"] = "idle"
            return {"status": "already_stopped", "message": "Engine is not running"}

        self._running = False
        self._shutdown_event.set()

        if self._universe_stop_event is not None:
            self._universe_stop_event.set()
        for task_attr in ("_universe_task", "_initial_rescan_task", "_watchdog_task"):
            task = getattr(self, task_attr, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            setattr(self, task_attr, None)

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        self._heartbeat["state"] = "stopped"
        logger.info("Trading engine stopped from UI")
        await self.notifier.send_alert("Trading engine stopped from dashboard")
        return {"status": "stopped", "message": "Engine has been stopped"}

    async def _background_run(self) -> None:
        try:
            await self.run_loop()
        except asyncio.CancelledError:
            logger.info("Engine background task cancelled")
            raise
        except Exception:
            logger.exception("Engine background task failed")
            self._heartbeat["state"] = "error"
            self._running = False

    # --- Finding #6: watchdog detects stalled engine loop ---
    async def _watchdog(self) -> None:
        watchdog_interval = self.settings["engine"].get(
            "watchdog_interval_seconds", DEFAULT_WATCHDOG_INTERVAL
        )
        max_loop_timeout = self.settings["engine"].get(
            "max_loop_timeout_seconds", DEFAULT_MAX_LOOP_TIMEOUT
        )
        try:
            while self._running:
                await asyncio.sleep(watchdog_interval)
                if not self._running:
                    break
                if self._loop_iteration_start is not None:
                    elapsed = (datetime.utcnow() - self._loop_iteration_start).total_seconds()
                    if elapsed > max_loop_timeout:
                        message = (
                            f"WATCHDOG: Engine loop stalled for {elapsed:.0f}s "
                            f"(limit {max_loop_timeout}s) — alerting"
                        )
                        logger.critical(message)
                        self._heartbeat["state"] = "stalled"
                        await self.notifier.send_alert(message)
                last_scan = self._heartbeat.get("last_scan_at")
                if last_scan:
                    elapsed_since_scan = (
                        datetime.utcnow() - datetime.fromisoformat(last_scan)
                    ).total_seconds()
                    interval = self._scan_interval_seconds()
                    if elapsed_since_scan > interval * 5:
                        message = (
                            f"WATCHDOG: No completed scan for {elapsed_since_scan:.0f}s — engine may be hung"
                        )
                        logger.critical(message)
                        self._heartbeat["state"] = "stalled"
                        await self.notifier.send_alert(message)
        except asyncio.CancelledError:
            pass

    async def _on_circuit_breaker(self, event: CircuitBreakerEvent) -> None:
        await self.notifier.send_alert(
            f"CIRCUIT BREAKER: {event.reason}\nAction: {event.action}"
        )
        # Finding #7: both close_all AND halt close positions
        if event.action in ("close_all", "halt"):
            await self._close_all_positions()

    def _tick_size_for(self, symbol: str) -> float:
        """Get tick size from cached product data, default to 0.01."""
        product = self.exchange._product_cache.get(symbol, {})
        try:
            return float(product.get("tick_size", 0.01) or 0.01)
        except (TypeError, ValueError):
            return 0.01

    @staticmethod
    def _round_to_tick(price: float, tick: float) -> float:
        if tick <= 0:
            return price
        return round(round(price / tick) * tick, 10)

    def _apply_default_stops(self, pos: Position) -> None:
        """Apply default ATR-based SL/TP to a position missing them."""
        atr_mult = self.risk_config.get("stops", {}).get("default_atr_multiplier", 2.0)
        fallback_pct = 0.02
        price = pos.entry_price
        if price <= 0:
            return

        tick = self._tick_size_for(pos.symbol)
        snap = self._symbol_snapshots.get(pos.symbol, {})
        indicators = snap.get("indicators", {})
        atr = indicators.get("atr_14")
        stop_distance = atr * atr_mult if atr and atr > 0 else price * fallback_pct
        stop_distance = max(stop_distance, tick * 2)

        if pos.stop_loss is None:
            if pos.side == Direction.LONG:
                pos.stop_loss = self._round_to_tick(price - stop_distance, tick)
            else:
                pos.stop_loss = self._round_to_tick(price + stop_distance, tick)

        if pos.take_profit is None:
            tp_distance = stop_distance * 2
            if pos.side == Direction.LONG:
                pos.take_profit = self._round_to_tick(price + tp_distance, tick)
            else:
                pos.take_profit = self._round_to_tick(price - tp_distance, tick)

        if pos.stop_loss == price or pos.take_profit == price:
            logger.warning(
                "SL/TP collapsed to entry for %s (tick=%.6f, atr=%s) — stops too tight",
                pos.symbol, tick, atr,
            )
            return

        logger.info(
            "Applied default stops to %s: SL=%s, TP=%s (tick=%s)",
            pos.symbol, pos.stop_loss, pos.take_profit, tick,
        )
        asyncio.ensure_future(
            self._place_bracket_on_exchange(pos.symbol, pos.stop_loss, pos.take_profit)
        )

    async def _place_bracket_on_exchange(
        self,
        symbol: str,
        stop_loss: Optional[float],
        take_profit: Optional[float],
    ) -> None:
        """Place SL/TP as a bracket order on Delta Exchange for protection."""
        if self.order_manager.paper_mode:
            return
        if stop_loss is None and take_profit is None:
            return
        try:
            result = await self.exchange.place_bracket_order(
                symbol=symbol,
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
            )
            if result is not None:
                pos = next(
                    (p for p in self.portfolio.positions if p.symbol == symbol), None
                )
                if pos is not None:
                    pos._bracket_placed = True
        except Exception:
            logger.warning(
                "Failed to place bracket order on exchange for %s (SL=%s, TP=%s)",
                symbol, stop_loss, take_profit,
                exc_info=True,
            )

    def _persist_position_state(self) -> None:
        """Persist current positions to disk for crash recovery."""
        try:
            self.position_state_store.save(self.portfolio._positions)
        except Exception:
            logger.exception("Failed to persist position state to disk")

    async def _on_fill(self, event: FillEvent) -> None:
        order = event.order

        # A fill price of zero means the fill was synthesised without a mark
        # price. Acting on it books a -100%-of-notional trade, so refuse it
        # rather than corrupting portfolio state.
        if event.fill_price is None or event.fill_price <= 0:
            logger.error(
                "Ignoring fill for %s with non-positive price %r (order=%s)",
                order.symbol,
                event.fill_price,
                order.id,
            )
            return

        existing = self.portfolio._positions.get(order.symbol)

        if existing is None and order.is_exit:
            # Duplicate/late exit fill — the position is already closed. Opening
            # a new one here would create a phantom position facing the wrong way.
            logger.warning(
                "Ignoring exit fill for %s — no open position (already closed)",
                order.symbol,
            )
            return

        if existing is None:
            if self.circuit_breaker.is_halted:
                logger.warning(
                    "Ignoring entry fill for %s while circuit breaker is halted",
                    order.symbol,
                )
                return
            side = Direction.LONG if order.side == OrderSide.BUY else Direction.SHORT
            self.portfolio.open_position(
                Position(
                    symbol=order.symbol,
                    side=side,
                    entry_price=event.fill_price,
                    size=event.fill_size,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    strategy_name=order.strategy_name,
                    contract_value=order.contract_value,
                ),
                entry_fee=event.fee_usd,
            )
            self._persist_position_state()
            await self._place_bracket_on_exchange(
                order.symbol, order.stop_loss, order.take_profit
            )
            return

        funding = self.cost_model.funding_usd(
            notional=existing.entry_price * existing.size,
            direction=existing.side,
            entry_ts=existing.opened_at,
            exit_ts=event.timestamp,
        )
        trade = self.portfolio.close_position(
            order.symbol,
            event.fill_price,
            exit_fee=event.fee_usd,
            funding=funding,
        )
        self._persist_position_state()
        if trade:
            self.journal.record_trade(trade)
            self.circuit_breaker.record_trade_result(trade.pnl)
            scores = self.scorer.score_all(self.journal.get_trades_by_strategy())
            self.strategy_selector.update_performance_scores(scores)
            await self.event_bus.publish(TradeClosedEvent(trade=trade))

    async def _on_regime_change(self, event: RegimeChangeEvent) -> None:
        params = self.param_adapter.on_regime_change(event.new_regime, event.old_regime)
        for name, p in params.items():
            if name in self.strategy_selector.get_all_strategies():
                self.strategy_selector.get_all_strategies()[name].params.update(p)
        await self.notifier.send_alert(
            f"Regime change: {event.old_regime.value} -> {event.new_regime.value}"
        )

    async def _close_all_positions(self) -> None:
        for pos in list(self.portfolio.positions):
            try:
                await self.order_manager.close_position_verified(
                    symbol=pos.symbol,
                    direction=pos.side,
                    size=pos.size,
                    mark_price=self._current_price_for(pos.symbol) or pos.entry_price,
                    strategy_name="circuit_breaker",
                    reason="circuit_breaker",
                )
            except PositionCloseError:
                logger.critical(
                    "Circuit breaker failed to close %s — symbol halted, manual intervention required",
                    pos.symbol,
                )
            except Exception:
                logger.exception(
                    "Unexpected error closing %s during circuit breaker",
                    pos.symbol,
                )

    async def _enforce_stop_loss_and_take_profit(
        self, symbol: str, price: float
    ) -> Optional[str]:
        pos = next((p for p in self.portfolio.positions if p.symbol == symbol), None)
        if pos is None:
            return None

        exit_reason: Optional[str] = None
        if pos.side == Direction.LONG:
            if pos.stop_loss is not None and price <= pos.stop_loss:
                exit_reason = "stop_loss"
            elif pos.take_profit is not None and price >= pos.take_profit:
                exit_reason = "take_profit"
        else:
            if pos.stop_loss is not None and price >= pos.stop_loss:
                exit_reason = "stop_loss"
            elif pos.take_profit is not None and price <= pos.take_profit:
                exit_reason = "take_profit"

        if exit_reason is None:
            return None

        try:
            await self.order_manager.close_position_verified(
                symbol=pos.symbol,
                direction=pos.side,
                size=pos.size,
                mark_price=price,
                strategy_name=pos.strategy_name or "stop_enforcement",
                reason=exit_reason,
            )
        except PositionCloseError:
            logger.critical(
                "%s close failed for %s — symbol halted, manual intervention required",
                exit_reason,
                pos.symbol,
            )
        except Exception:
            logger.exception(
                "Unexpected error during %s close for %s",
                exit_reason,
                pos.symbol,
            )
            return None

        return exit_reason

    async def process_symbol(
        self,
        symbol: str,
        timeframes: dict[str, str],
        *,
        execute: bool = True,
    ) -> None:
        if self.order_manager.is_symbol_halted(symbol):
            self._log_signal(
                symbol,
                "rejected",
                "Trading halted: unresolved order on this symbol",
            )
            return

        signal_tf = timeframes.get("signal", "15m")
        trend_tf = timeframes.get("trend", "1h")
        context_tf = timeframes.get("context", "4h")

        df = await self.data_manager.get_dataframe(symbol, signal_tf)
        if df.empty:
            logger.warning("No data for %s", symbol)
            self._log_signal(symbol, "error", "No market data available")
            return

        multi_tf = await self.data_manager.get_multi_timeframe(
            symbol, [trend_tf, context_tf]
        )
        market_state = self.market_builder.build(symbol, df, multi_tf)
        self.circuit_breaker.record_data_received()

        self._symbol_snapshots[symbol] = {
            "symbol": symbol,
            "price": market_state.price,
            "regime": market_state.regime.value,
            "regime_confidence": market_state.regime_confidence,
            "indicators": {
                k: market_state.indicators[k]
                for k in ("rsi_14", "ADX_14", "atr_14", "volume_ratio", "ema_50")
                if k in market_state.indicators
            },
            "updated_at": datetime.utcnow().isoformat(),
        }

        exit_reason = await self._enforce_stop_loss_and_take_profit(
            symbol, market_state.price
        )
        if exit_reason is not None:
            self._log_signal(
                symbol,
                "executed",
                f"Position closed: {exit_reason}",
            )
            return

        old_regime = self.regime_detector.current_regime
        if market_state.regime != old_regime and old_regime != Regime.UNKNOWN:
            await self.event_bus.publish(
                RegimeChangeEvent(
                    symbol=symbol,
                    old_regime=old_regime,
                    new_regime=market_state.regime,
                    confidence=market_state.regime_confidence,
                )
            )

        portfolio = self.portfolio.get_snapshot()
        portfolio.portfolio_heat_pct = self.risk_manager.calculate_portfolio_heat(
            portfolio.open_positions, portfolio.equity
        )

        assigned = self.universe.get_strategy_for(symbol)
        trading_signal = None
        if assigned and assigned != "auto":
            strategy = self.strategy_selector.get_all_strategies().get(assigned)
            if strategy is not None:
                self.strategy_selector.update_params_for_regime(market_state.regime)
                try:
                    candidate = strategy.analyze(market_state)
                except Exception:
                    logger.exception("Assigned strategy %s failed on %s", assigned, symbol)
                    candidate = None
                if candidate:
                    perf_boost = self.strategy_selector.performance_scores.get(strategy.name, 0.5)
                    candidate.confidence = min(1.0, candidate.confidence * 0.7 + perf_boost * 0.3)
                    min_conf = self.strategy_config.get("selector", {}).get("min_confidence", 0.5)
                    if candidate.confidence >= min_conf:
                        trading_signal = candidate

        if trading_signal is None:
            trading_signal = self.strategy_selector.select_signal(market_state)

        if not trading_signal:
            self._log_signal(
                symbol,
                "scan",
                f"No signal — conditions not met (assigned={assigned or 'auto'})",
            )
            return

        signal_meta = {
            "strategy": trading_signal.strategy_name,
            "direction": trading_signal.direction.value,
            "confidence": trading_signal.confidence,
        }

        if not execute:
            self._log_signal(
                symbol,
                "scan",
                (
                    f"Signal detected ({trading_signal.strategy_name} "
                    f"{trading_signal.direction.value} conf={trading_signal.confidence:.2f}) "
                    "— execution paused (circuit breaker / degraded mode)"
                ),
                **signal_meta,
            )
            return

        if trading_signal.action == SignalAction.EXIT:
            open_pos = next(
                (p for p in portfolio.open_positions if p.symbol == symbol),
                None,
            )
            if open_pos is None:
                self._log_signal(symbol, "rejected", "Exit signal but no open position")
                return
            try:
                await self.order_manager.close_position_verified(
                    symbol=open_pos.symbol,
                    direction=open_pos.side,
                    size=open_pos.size,
                    mark_price=market_state.price,
                    strategy_name=trading_signal.strategy_name,
                    reason="signal_exit",
                    signal_id=trading_signal.id,
                )
                self._log_signal(
                    symbol,
                    "executed",
                    "Manual/strategy exit order verified flat",
                    strategy=trading_signal.strategy_name,
                    direction=trading_signal.direction.value,
                    confidence=trading_signal.confidence,
                )
            except PositionCloseError:
                self._log_signal(
                    symbol,
                    "error",
                    "Exit failed — position may still be open; manual intervention required",
                    strategy=trading_signal.strategy_name,
                    direction=trading_signal.direction.value,
                    confidence=trading_signal.confidence,
                )
            except Exception as exc:
                logger.exception("Exit execution failed for %s", symbol)
                self._log_signal(
                    symbol,
                    "error",
                    f"Exit failed: {exc}",
                    strategy=trading_signal.strategy_name,
                    direction=trading_signal.direction.value,
                    confidence=trading_signal.confidence,
                )
            return

        valid, reason = self.risk_manager.validate_signal(trading_signal, portfolio)
        if not valid:
            await self._publish_pending_cb_events()
            logger.debug("Signal rejected: %s", reason)
            self._log_signal(
                symbol,
                "rejected",
                reason,
                strategy=trading_signal.strategy_name,
                direction=trading_signal.direction.value,
                confidence=trading_signal.confidence,
            )
            return

        atr = market_state.indicators.get("atr_14")
        size = self.risk_manager.calculate_position_size(
            trading_signal, portfolio, atr
        )
        if size <= 0:
            self._log_signal(
                symbol,
                "rejected",
                "Position size below minimum",
                strategy=trading_signal.strategy_name,
                direction=trading_signal.direction.value,
                confidence=trading_signal.confidence,
            )
            return

        size = self.position_sizer.cap_to_available_balance(
            size,
            trading_signal.entry_price or market_state.price,
            portfolio.available_balance,
        )
        if size <= 0:
            self._log_signal(
                symbol,
                "rejected",
                "Insufficient available balance for minimum order",
                strategy=trading_signal.strategy_name,
                direction=trading_signal.direction.value,
                confidence=trading_signal.confidence,
            )
            return

        entry_price = trading_signal.entry_price or market_state.price
        notional = size * entry_price
        if notional > portfolio.available_balance * self.position_sizer.max_leverage * 0.7:
            self._log_signal(
                symbol,
                "rejected",
                "Notional exceeds available margin",
                strategy=trading_signal.strategy_name,
                direction=trading_signal.direction.value,
                confidence=trading_signal.confidence,
            )
            return

        await self.event_bus.publish(
            SignalEvent(signal=trading_signal, market_state=market_state)
        )
        try:
            await self.order_manager.execute_signal(trading_signal, size)
            await self.notifier.notify_trade(trading_signal, size)
            self._log_signal(
                symbol,
                "executed",
                f"Order placed size={size:.6f}",
                strategy=trading_signal.strategy_name,
                direction=trading_signal.direction.value,
                confidence=trading_signal.confidence,
            )
        except Exception as exc:
            logger.exception("Order execution failed for %s", symbol)
            self._log_signal(
                symbol,
                "error",
                f"Order failed: {exc}",
                strategy=trading_signal.strategy_name,
                direction=trading_signal.direction.value,
                confidence=trading_signal.confidence,
            )

    # --- Finding #9: track sync failures, halt after N consecutive ---
    async def sync_exchange_state(self) -> None:
        if self.order_manager.paper_mode:
            return
        max_sync_failures = self.settings.get("engine", {}).get(
            "max_sync_failures", DEFAULT_MAX_SYNC_FAILURES
        )
        try:
            balances = await self.exchange.get_balance()
            if isinstance(balances, list) and balances:
                for bal in balances:
                    if not isinstance(bal, dict):
                        continue
                    asset = bal.get("asset_symbol", bal.get("currency", ""))
                    if asset in ("USD", "USDT", "INR"):
                        equity_raw = bal.get("balance", bal.get("available_balance"))
                        try:
                            equity = float(equity_raw)
                        except (TypeError, ValueError):
                            logger.warning("Invalid balance value for %s: %s", asset, equity_raw)
                            continue
                        if equity > 0:
                            avail_raw = bal.get("available_balance", equity)
                            try:
                                avail = float(avail_raw)
                            except (TypeError, ValueError):
                                avail = equity
                            self.portfolio.seed_equity_from_exchange(equity)
                            self.portfolio.available_balance = avail
                            break

            existing = {p.symbol: p for p in self.portfolio.positions}
            positions = await self.exchange.get_all_positions()
            merged: dict[str, Position] = {}
            for exchange_pos in positions:
                local = existing.get(exchange_pos.symbol)
                if local:
                    exchange_pos.stop_loss = local.stop_loss
                    exchange_pos.take_profit = local.take_profit
                    exchange_pos.strategy_name = local.strategy_name
                    exchange_pos.opened_at = local.opened_at
                    if not getattr(local, "_bracket_placed", False):
                        asyncio.ensure_future(
                            self._place_bracket_on_exchange(
                                exchange_pos.symbol,
                                exchange_pos.stop_loss,
                                exchange_pos.take_profit,
                            )
                        )
                        exchange_pos._bracket_placed = True
                else:
                    self._apply_default_stops(exchange_pos)
                merged[exchange_pos.symbol] = exchange_pos

            self.portfolio._positions = merged
            self._persist_position_state()
            logger.info(
                "Exchange sync (%s): %d position(s), equity=%.2f",
                self._mode,
                len(merged),
                self.portfolio.equity,
            )

            prices: dict[str, float] = {}
            for symbol in merged:
                price = self._current_price_for(symbol)
                if price is None:
                    try:
                        ticker = await self.exchange.get_ticker(symbol)
                        price = _extract_ticker_price(ticker)
                        if price:
                            snap = self._symbol_snapshots.get(symbol, {"symbol": symbol})
                            snap["price"] = price
                            snap["updated_at"] = datetime.utcnow().isoformat()
                            self._symbol_snapshots[symbol] = snap
                    except Exception:
                        logger.debug("Ticker fetch failed for %s during sync", symbol, exc_info=True)
                if price:
                    prices[symbol] = price

            if prices:
                self.portfolio.update_prices(prices, from_exchange_sync=True)
            self._consecutive_sync_failures = 0
        except Exception:
            self._consecutive_sync_failures += 1
            logger.warning(
                "Exchange state sync failed (mode=%s, consecutive=%d/%d)",
                self._mode,
                self._consecutive_sync_failures,
                max_sync_failures,
                exc_info=True,
            )
            # Halt once, on the threshold *crossing* only. Previously this fired on
            # every failure past the threshold, rewriting the state file, appending to
            # the audit log and re-alerting each time — 3034 halts and a 2 MB audit
            # log accumulated from a single outage.
            crossed_threshold = self._consecutive_sync_failures == max_sync_failures
            if crossed_threshold and not self.circuit_breaker.is_halted:
                message = (
                    f"Exchange sync failed {self._consecutive_sync_failures} times "
                    f"consecutively — halting via circuit breaker"
                )
                logger.critical(message)
                await self.circuit_breaker.trigger_and_publish(
                    reason="exchange_sync_failure",
                    action="halt",
                    details={"consecutive_failures": self._consecutive_sync_failures},
                )
                await self.notifier.send_alert(message)

    def _scan_interval_seconds(self) -> int:
        engine_cfg = self.settings.get("engine", {})
        return int(
            engine_cfg.get(
                "scan_interval_seconds",
                engine_cfg.get("loop_interval_seconds", 30),
            )
        )

    async def run_loop(self) -> None:
        interval = self._scan_interval_seconds()
        max_loop_timeout = self.settings["engine"].get(
            "max_loop_timeout_seconds", DEFAULT_MAX_LOOP_TIMEOUT
        )

        while self._running:
            self._loop_iteration_start = datetime.utcnow()
            self._maybe_reset_daily()
            self.circuit_breaker.refresh_from_disk()

            execute_orders = not self.circuit_breaker.is_halted
            if self.circuit_breaker.is_halted:
                recovery = await self._verify_exchange_connectivity()
                if recovery["ok"] and self.circuit_breaker.resume_on_connectivity_restore(
                    "engine_auto_recovery"
                ):
                    logger.info("Circuit breaker auto-resumed — connectivity restored")
                    execute_orders = True
                    self._heartbeat["state"] = "running"

            scanned: list[str] = []
            loop_had_errors = False
            try:
                await asyncio.wait_for(
                    self._run_one_iteration(scanned, execute=execute_orders),
                    timeout=max_loop_timeout,
                )
                loop_had_errors = False
            except asyncio.TimeoutError:
                logger.critical(
                    "Engine loop iteration timed out after %ds", max_loop_timeout
                )
                self._heartbeat["state"] = "stalled"
                await self.notifier.send_alert(
                    f"Engine loop iteration timed out after {max_loop_timeout}s"
                )
                loop_had_errors = True
            except Exception:
                logger.exception("Error in trading loop")
                self.circuit_breaker.record_api_error()
                loop_had_errors = True

            if not loop_had_errors:
                self.circuit_breaker.record_api_success()
                self._heartbeat["errors"] = 0
                if self._heartbeat["state"] in ("error", "stalled", "halted"):
                    self._heartbeat["state"] = (
                        "running" if execute_orders else "scanning"
                    )
            else:
                self._heartbeat["errors"] = self._heartbeat.get("errors", 0) + 1
                if self._heartbeat["errors"] >= 3:
                    self._heartbeat["state"] = "error"

            self._heartbeat["loop_count"] = self._heartbeat.get("loop_count", 0) + 1
            self._heartbeat["last_scan_at"] = datetime.utcnow().isoformat()
            self._heartbeat["last_scan_symbols"] = scanned
            self._heartbeat["execution_enabled"] = execute_orders
            self._loop_iteration_start = None

            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                continue

        self._running = False

    async def _run_one_iteration(self, scanned: list[str], *, execute: bool = True) -> None:
        """Scan all active symbols for opportunities; optionally place orders."""
        if execute:
            await self.sync_exchange_state()
        self._sync_active_pairs_with_universe()
        pairs = self._active_pairs or []
        if not pairs:
            return

        max_concurrent = self.settings["engine"].get("max_concurrent_symbols", 10)
        sem = asyncio.Semaphore(max_concurrent)

        async def _scan_pair(pair: dict[str, Any]) -> None:
            if self.circuit_breaker.is_halted and execute:
                return
            symbol = pair["symbol"]
            timeframes = pair.get("timeframes", DEFAULT_TIMEFRAMES)
            async with sem:
                try:
                    await self.process_symbol(symbol, timeframes, execute=execute)
                    scanned.append(symbol)
                except Exception as exc:
                    logger.exception("Error processing %s", symbol)
                    self._log_signal(symbol, "error", f"Symbol processing failed: {exc}")
                    from src.execution.exchange import ExchangeError

                    if isinstance(exc, ExchangeError):
                        self.circuit_breaker.record_api_error()
                    pending = self.circuit_breaker.take_pending_event()
                    if pending is not None:
                        await self.event_bus.publish(pending)
                        await self.notifier.send_alert(
                            f"CIRCUIT BREAKER (API errors): {pending.reason}"
                        )

        await asyncio.gather(*(_scan_pair(pair) for pair in pairs))

    async def start(self) -> None:
        """CLI entry: start engine and block until stopped."""
        await self.start_background()
        if self._task:
            await self._task

    # --- Finding #10: graceful shutdown closes open positions ---
    async def stop(self) -> None:
        logger.info("Shutting down trading engine...")
        if not self.order_manager.paper_mode and self.portfolio.positions:
            logger.warning(
                "Shutdown with %d open position(s) — attempting to close",
                len(self.portfolio.positions),
            )
            await self.notifier.send_alert(
                f"Engine shutting down with {len(self.portfolio.positions)} open position(s) — closing all"
            )
            await self._close_all_positions()
        self._persist_position_state()
        await self.stop_background()
        await self.ws_client.stop()
        if self._event_bus_started:
            await self.event_bus.stop()
            self._event_bus_started = False
        if self._exchange_connected:
            await self.exchange.close()
            self._exchange_connected = False

    def setup_signal_handlers(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))
