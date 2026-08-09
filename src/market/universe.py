"""Adaptive universe manager.

Combines the SymbolScanner (finds top-N liquid perpetuals) with the
QuickBacktester (scores every strategy on each candidate) and produces:

    top_symbols   : the top-K symbols the engine should actively trade
    assignments   : {symbol -> best strategy name for the current market}
    scoreboard    : full grid of (symbol, strategy) scores for the UI

The manager runs on demand at engine start and re-runs every
`rescan_interval_hours` (default 6).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from src.backtest.quick import BacktestResult, QuickBacktester
from src.core.models import Regime
from src.data.manager import DataManager
from src.market.scanner import SymbolInfo, SymbolScanner
from src.portfolio.journal import TradeJournal
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

# Fallback when exchange is unreachable — common Delta India perpetuals
DEFAULT_PERPETUAL_SYMBOLS = (
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD",
    "DOGEUSD", "ADAUSD", "AVAXUSD", "LINKUSD", "DOTUSD",
    "MATICUSD", "LTCUSD", "UNIUSD", "ATOMUSD", "NEARUSD",
    "APTUSD", "ARBUSD", "OPUSD", "INJUSD", "SUIUSD",
    "FILUSD", "AAVEUSD", "TRXUSD", "BCHUSD", "ETCUSD",
)


@dataclass
class SymbolAssignment:
    symbol: str
    strategy: str
    score: float
    regime: str = "unknown"
    turnover_usd_24h: float = 0.0
    trades: int = 0
    win_rate: float = 0.0
    expectancy_pct: float = 0.0
    sharpe: float = 0.0
    all_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "score": round(float(self.score), 3),
            "regime": self.regime,
            "turnover_usd_24h": round(float(self.turnover_usd_24h), 2),
            "trades": int(self.trades),
            "win_rate": round(float(self.win_rate), 3),
            "expectancy_pct": round(float(self.expectancy_pct), 4),
            "sharpe": round(float(self.sharpe), 3),
            "all_scores": {k: round(float(v), 3) for k, v in self.all_scores.items()},
        }


class UniverseManager:
    """Owns symbol discovery, backtesting and adaptive strategy assignment."""

    def __init__(
        self,
        scanner: SymbolScanner,
        data_manager: DataManager,
        strategies: dict[str, BaseStrategy],
        backtester: Optional[QuickBacktester] = None,
        top_active: int = 10,
        history_timeframe: str = "15m",
        history_bars: int = 500,
        rescan_interval_hours: float = 6.0,
        max_concurrent_backtests: int = 4,
        min_backtest_trades: int = 10,
        reassignment_margin: float = 0.2,
        journal: Optional[TradeJournal] = None,
    ) -> None:
        self.scanner = scanner
        self.data_manager = data_manager
        self.strategies = strategies
        self.backtester = backtester or QuickBacktester(min_trades=min_backtest_trades)
        self.top_active = top_active
        self.history_timeframe = history_timeframe
        self.history_bars = history_bars
        self.rescan_interval_hours = rescan_interval_hours
        self.min_backtest_trades = min_backtest_trades
        self.reassignment_margin = reassignment_margin
        self.journal = journal
        self._sem = asyncio.Semaphore(max_concurrent_backtests)

        self._top_symbols: list[SymbolAssignment] = []
        self._universe: list[SymbolInfo] = []
        self._previous_assignments: dict[str, str] = {}
        self._previous_scores: dict[str, float] = {}
        self._last_scan_at: Optional[datetime] = None
        self._last_scan_duration: float = 0.0
        self._last_error: Optional[str] = None
        self._scoreboard: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    @property
    def top_symbols(self) -> list[SymbolAssignment]:
        return list(self._top_symbols)

    @property
    def universe(self) -> list[SymbolInfo]:
        return list(self._universe)

    @property
    def last_scan_at(self) -> Optional[datetime]:
        return self._last_scan_at

    def get_active_symbols(self) -> list[str]:
        return [a.symbol for a in self._top_symbols]

    def get_strategy_for(self, symbol: str) -> Optional[str]:
        symbol = symbol.upper()
        for a in self._top_symbols:
            if a.symbol == symbol:
                return a.strategy
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "last_scan_at": self._last_scan_at.isoformat() if self._last_scan_at else None,
            "last_scan_duration_seconds": round(self._last_scan_duration, 2),
            "rescan_interval_hours": self.rescan_interval_hours,
            "universe_size": len(self._universe),
            "top_active": [a.to_dict() for a in self._top_symbols],
            "scoreboard": self._scoreboard,
            "last_error": self._last_error,
        }

    async def rescan(self, fallback_symbols: Optional[list[str]] = None) -> dict[str, Any]:
        """Run a full rescan: discover universe, backtest, pick top-K.

        If the scanner fails (e.g. exchange unreachable), the manager falls back
        to `fallback_symbols` so the dashboard is never empty.
        """
        async with self._lock:
            started = datetime.utcnow()
            logger.info("Universe rescan started")
            self._last_error = None

            universe = await self.scanner.scan()
            self._universe = universe

            if not universe:
                logger.warning("Universe rescan produced no symbols (scanner returned empty)")
                self._last_error = "scanner_empty"
                seeds = fallback_symbols or list(DEFAULT_PERPETUAL_SYMBOLS[: self.top_active])
                self._top_symbols = [
                    SymbolAssignment(
                        symbol=s,
                        strategy="auto",
                        score=0.0,
                        regime="unknown",
                    )
                    for s in seeds[: self.top_active]
                ]
                self._scoreboard = []
                self._last_scan_at = datetime.utcnow()
                self._last_scan_duration = (self._last_scan_at - started).total_seconds()
                return self.snapshot()

            results = await self._backtest_universe(universe)
            self._scoreboard = [r.to_dict() for r in results]
            self._top_symbols = self._pick_top_symbols(universe, results)
            self._previous_assignments = {a.symbol: a.strategy for a in self._top_symbols}
            self._previous_scores = {a.symbol: a.score for a in self._top_symbols}

            if not self._top_symbols and fallback_symbols:
                logger.warning("Backtest produced no assignments; falling back to seed symbols")
                self._top_symbols = [
                    SymbolAssignment(
                        symbol=s,
                        strategy="auto",
                        score=0.0,
                        regime="unknown",
                    )
                    for s in fallback_symbols
                ]

            self._last_scan_at = datetime.utcnow()
            self._last_scan_duration = (self._last_scan_at - started).total_seconds()
            logger.info(
                "Universe rescan done in %.1fs: %d symbols, %d top-active",
                self._last_scan_duration,
                len(universe),
                len(self._top_symbols),
            )
            return self.snapshot()

    async def _backtest_universe(self, universe: list[SymbolInfo]) -> list[BacktestResult]:
        async def _run_symbol(info: SymbolInfo) -> list[BacktestResult]:
            async with self._sem:
                try:
                    df = await self.data_manager.get_dataframe(
                        info.symbol,
                        self.history_timeframe,
                        limit=self.history_bars,
                    )
                except Exception as exc:
                    logger.debug("Historical fetch failed for %s: %s", info.symbol, exc)
                    return []

                if df is None or df.empty or len(df) < 220:
                    return []

                out: list[BacktestResult] = []
                for name, strategy in self.strategies.items():
                    try:
                        r = self.backtester.run(info.symbol, strategy, df)
                    except Exception as exc:
                        logger.debug("Backtest %s/%s failed: %s", info.symbol, name, exc)
                        continue
                    out.append(r)
                return out

        gathered = await asyncio.gather(*[_run_symbol(s) for s in universe], return_exceptions=True)
        flat: list[BacktestResult] = []
        for res in gathered:
            if isinstance(res, list):
                flat.extend(res)
        return flat

    def _eligible_results(self, res_list: list[BacktestResult]) -> list[BacktestResult]:
        return [
            r
            for r in res_list
            if r.score > -0.5 and r.trades >= self.min_backtest_trades
        ]

    def _assignment_from_result(
        self,
        symbol: str,
        result: BacktestResult,
        res_list: list[BacktestResult],
        info: Optional[SymbolInfo],
    ) -> SymbolAssignment:
        return SymbolAssignment(
            symbol=symbol,
            strategy=result.strategy,
            score=result.score,
            regime=self._infer_regime(res_list),
            turnover_usd_24h=info.turnover_usd_24h if info else 0.0,
            trades=result.trades,
            win_rate=result.win_rate,
            expectancy_pct=result.expectancy_pct,
            sharpe=result.sharpe,
            all_scores={r.strategy: r.score for r in res_list},
        )

    def _incumbent_assignment(
        self,
        symbol: str,
        incumbent: str,
        incumbent_score: float,
        res_list: list[BacktestResult],
        info: Optional[SymbolInfo],
    ) -> SymbolAssignment:
        incumbent_result = next((r for r in res_list if r.strategy == incumbent), None)
        return SymbolAssignment(
            symbol=symbol,
            strategy=incumbent,
            score=incumbent_score,
            regime=self._infer_regime(res_list),
            turnover_usd_24h=info.turnover_usd_24h if info else 0.0,
            trades=incumbent_result.trades if incumbent_result else 0,
            win_rate=incumbent_result.win_rate if incumbent_result else 0.0,
            expectancy_pct=incumbent_result.expectancy_pct if incumbent_result else 0.0,
            sharpe=incumbent_result.sharpe if incumbent_result else 0.0,
            all_scores={r.strategy: r.score for r in res_list},
        )

    def _log_reassignment(
        self,
        *,
        symbol: str,
        old_strategy: str,
        new_strategy: str,
        old_score: float,
        new_score: float,
        old_trades: int,
        new_trades: int,
        new_t_stat: float,
        significance_met: bool,
        margin_met: bool,
        reason: str,
    ) -> None:
        logger.info(
            "Symbol %s: %s -> %s (score %.3f -> %.3f, %d trades, t=%.2f, margin=%s)",
            symbol,
            old_strategy,
            new_strategy,
            old_score,
            new_score,
            new_trades,
            new_t_stat,
            margin_met,
        )
        if self.journal is None:
            return
        self.journal.record_reassignment(
            symbol=symbol,
            old_strategy=old_strategy,
            new_strategy=new_strategy,
            old_score=old_score,
            new_score=new_score,
            old_trades=old_trades,
            new_trades=new_trades,
            new_t_stat=new_t_stat,
            significance_met=significance_met,
            margin_met=margin_met,
            reason=reason,
        )

    def _resolve_symbol_assignment(
        self,
        symbol: str,
        res_list: list[BacktestResult],
        info: Optional[SymbolInfo],
    ) -> Optional[SymbolAssignment]:
        eligible = self._eligible_results(res_list)
        incumbent = self._previous_assignments.get(symbol)
        incumbent_score = self._previous_scores.get(symbol, 0.0)
        incumbent_result = (
            next((r for r in res_list if r.strategy == incumbent), None)
            if incumbent
            else None
        )
        old_trades = incumbent_result.trades if incumbent_result else 0

        if not eligible:
            if incumbent and incumbent != "auto":
                self._log_reassignment(
                    symbol=symbol,
                    old_strategy=incumbent,
                    new_strategy=incumbent,
                    old_score=incumbent_score,
                    new_score=incumbent_score,
                    old_trades=old_trades,
                    new_trades=old_trades,
                    new_t_stat=incumbent_result.t_stat if incumbent_result else 0.0,
                    significance_met=False,
                    margin_met=False,
                    reason="kept_incumbent",
                )
                return self._incumbent_assignment(
                    symbol, incumbent, incumbent_score, res_list, info
                )
            return None

        best = max(eligible, key=lambda x: x.score)
        significance_met = best.trades >= self.min_backtest_trades and best.t_stat >= 1.0

        if incumbent and incumbent != "auto":
            margin_met = best.score > incumbent_score * (1.0 + self.reassignment_margin)
            if best.strategy == incumbent:
                self._log_reassignment(
                    symbol=symbol,
                    old_strategy=incumbent,
                    new_strategy=incumbent,
                    old_score=incumbent_score,
                    new_score=best.score,
                    old_trades=old_trades,
                    new_trades=best.trades,
                    new_t_stat=best.t_stat,
                    significance_met=significance_met,
                    margin_met=True,
                    reason="kept_incumbent",
                )
                return self._assignment_from_result(symbol, best, res_list, info)

            if not margin_met:
                self._log_reassignment(
                    symbol=symbol,
                    old_strategy=incumbent,
                    new_strategy=incumbent,
                    old_score=incumbent_score,
                    new_score=best.score,
                    old_trades=old_trades,
                    new_trades=best.trades,
                    new_t_stat=best.t_stat,
                    significance_met=significance_met,
                    margin_met=False,
                    reason="kept_incumbent",
                )
                return self._incumbent_assignment(
                    symbol, incumbent, incumbent_score, res_list, info
                )

            self._log_reassignment(
                symbol=symbol,
                old_strategy=incumbent,
                new_strategy=best.strategy,
                old_score=incumbent_score,
                new_score=best.score,
                old_trades=old_trades,
                new_trades=best.trades,
                new_t_stat=best.t_stat,
                significance_met=significance_met,
                margin_met=True,
                reason="reassigned",
            )
            return self._assignment_from_result(symbol, best, res_list, info)

        self._log_reassignment(
            symbol=symbol,
            old_strategy=incumbent or "",
            new_strategy=best.strategy,
            old_score=incumbent_score,
            new_score=best.score,
            old_trades=old_trades,
            new_trades=best.trades,
            new_t_stat=best.t_stat,
            significance_met=significance_met,
            margin_met=True,
            reason="initial",
        )
        return self._assignment_from_result(symbol, best, res_list, info)

    def _pick_top_symbols(
        self,
        universe: list[SymbolInfo],
        results: list[BacktestResult],
    ) -> list[SymbolAssignment]:
        by_symbol: dict[str, list[BacktestResult]] = {}
        for r in results:
            by_symbol.setdefault(r.symbol, []).append(r)

        info_by_symbol = {info.symbol: info for info in universe}

        assignments: list[SymbolAssignment] = []
        for symbol, res_list in by_symbol.items():
            if not res_list:
                continue
            assignment = self._resolve_symbol_assignment(
                symbol,
                res_list,
                info_by_symbol.get(symbol),
            )
            if assignment is not None:
                assignments.append(assignment)

        assignments.sort(key=lambda a: (a.score, a.turnover_usd_24h), reverse=True)

        if len(assignments) < self.top_active:
            ranked = sorted(universe, key=lambda s: (s.turnover_usd_24h, s.volume_24h), reverse=True)
            existing = {a.symbol for a in assignments}
            for info in ranked:
                if info.symbol in existing:
                    continue
                res_list = by_symbol.get(info.symbol, [])
                if res_list:
                    assignment = self._resolve_symbol_assignment(info.symbol, res_list, info)
                    if assignment is not None:
                        assignments.append(assignment)
                else:
                    assignments.append(
                        SymbolAssignment(
                            symbol=info.symbol,
                            strategy="auto",
                            score=0.0,
                            regime=Regime.UNKNOWN.value,
                            turnover_usd_24h=info.turnover_usd_24h,
                        )
                    )
                existing.add(info.symbol)
                if len(assignments) >= self.top_active:
                    break

        return assignments[: self.top_active]

    @staticmethod
    def _infer_regime(results: list[BacktestResult]) -> str:
        """Very rough regime hint from which strategy fits best."""
        if not results:
            return Regime.UNKNOWN.value
        best = max(results, key=lambda x: x.score)
        trend_names = {"ema_crossover", "supertrend", "macd", "donchian"}
        range_names = {"rsi_reversion", "bollinger"}
        breakout_names = {"volume_breakout"}
        if best.strategy in trend_names:
            return Regime.TRENDING.value
        if best.strategy in range_names:
            return Regime.RANGING.value
        if best.strategy in breakout_names:
            return Regime.VOLATILE.value
        return Regime.UNKNOWN.value

    async def start_periodic_rescan(
        self,
        stop_event: asyncio.Event,
        fallback_provider: Optional[Any] = None,
    ) -> None:
        """Background loop: rescan every `rescan_interval_hours`.

        `fallback_provider` is a zero-arg callable returning a list of symbols
        used when the scanner fails so the top-active never becomes empty.
        """
        interval_seconds = max(60.0, self.rescan_interval_hours * 3600.0)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                if stop_event.is_set():
                    break
            except asyncio.TimeoutError:
                pass
            try:
                fallback = fallback_provider() if fallback_provider else None
                await self.rescan(fallback_symbols=fallback)
            except Exception:
                logger.exception("Periodic universe rescan failed")
