"""Lightweight bar-by-bar backtester for scoring (symbol, strategy) fit.

Purpose: rapidly evaluate how each strategy would have performed on a symbol
over recent history so the engine can adaptively pick the best strategy per
symbol. This is *not* a full-fidelity simulator — it uses close-price fills,
ATR-multiple stops/targets, single position per symbol, no fees/slippage
sophistication. Optimized for speed so we can score ~50 symbols × 7 strategies
in a reasonable time.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from src.core.models import Direction, MarketState, Regime
from src.data.indicators import IndicatorEngine
from src.market.regime import RegimeDetector
from src.market.state import MarketStateBuilder
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_return_pct: float = 0.0
    avg_return_pct: float = 0.0
    win_rate: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    expectancy_pct: float = 0.0
    score: float = 0.0
    t_stat: float = 0.0
    trade_pnls: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "trades": int(self.trades),
            "wins": int(self.wins),
            "losses": int(self.losses),
            "win_rate": round(float(self.win_rate), 4),
            "total_return_pct": round(float(self.total_return_pct), 3),
            "avg_return_pct": round(float(self.avg_return_pct), 4),
            "expectancy_pct": round(float(self.expectancy_pct), 4),
            "sharpe": round(float(self.sharpe), 3),
            "max_drawdown_pct": round(float(self.max_drawdown_pct), 3),
            "t_stat": round(float(self.t_stat), 3),
            "score": round(float(self.score), 4),
        }


class QuickBacktester:
    """Run a single strategy over historical candles and score the fit."""

    def __init__(
        self,
        indicators: Optional[IndicatorEngine] = None,
        min_bars: int = 200,
        step: int = 1,
        atr_stop_mult: float = 2.0,
        atr_target_mult: float = 3.0,
        max_hold_bars: int = 96,  # ~24h on 15m
        risk_per_trade_pct: float = 1.0,
        min_trades: int = 10,
    ) -> None:
        self.indicators = indicators or IndicatorEngine()
        self.min_bars = min_bars
        self.step = step
        self.atr_stop_mult = atr_stop_mult
        self.atr_target_mult = atr_target_mult
        self.max_hold_bars = max_hold_bars
        self.risk_per_trade_pct = risk_per_trade_pct
        self.min_trades = min_trades

    def run(
        self,
        symbol: str,
        strategy: BaseStrategy,
        df: pd.DataFrame,
    ) -> BacktestResult:
        """Iterate bar-by-bar and simulate the strategy's entries/exits."""
        result = BacktestResult(symbol=symbol, strategy=strategy.name)
        if df is None or df.empty or len(df) < self.min_bars + 20:
            return result

        if "ADX_14" not in df.columns:
            df = self.indicators.compute_all(df.copy())

        regime_detector = RegimeDetector()
        builder = MarketStateBuilder(self.indicators, regime_detector)

        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)
        atrs = df["atr_14"].to_numpy(dtype=float) if "atr_14" in df.columns else np.full(len(df), np.nan)

        in_pos = False
        pos_side: Optional[Direction] = None
        entry_price = 0.0
        stop = 0.0
        target = 0.0
        bars_held = 0
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        returns: list[float] = []

        n = len(df)
        for i in range(self.min_bars, n, self.step):
            price = closes[i]

            if in_pos:
                bars_held += 1
                hit_stop = (pos_side == Direction.LONG and lows[i] <= stop) or (
                    pos_side == Direction.SHORT and highs[i] >= stop
                )
                hit_target = (pos_side == Direction.LONG and highs[i] >= target) or (
                    pos_side == Direction.SHORT and lows[i] <= target
                )
                exit_reason = None
                exit_price = price
                if hit_stop:
                    exit_price = stop
                    exit_reason = "stop"
                elif hit_target:
                    exit_price = target
                    exit_reason = "target"
                elif bars_held >= self.max_hold_bars:
                    exit_reason = "time"

                if exit_reason:
                    pnl_pct = self._pnl_pct(entry_price, exit_price, pos_side)
                    equity *= 1.0 + (pnl_pct / 100.0) * (self.risk_per_trade_pct / 100.0)
                    peak = max(peak, equity)
                    dd = (peak - equity) / peak if peak > 0 else 0.0
                    max_dd = max(max_dd, dd)
                    returns.append(pnl_pct)
                    result.trade_pnls.append(pnl_pct)
                    result.trades += 1
                    if pnl_pct > 0:
                        result.wins += 1
                    else:
                        result.losses += 1
                    in_pos = False
                    pos_side = None
                    bars_held = 0
                    continue

            if in_pos:
                continue

            try:
                window = df.iloc[: i + 1]
                state = builder.build(symbol, window)
            except Exception:
                continue

            if state.regime == Regime.UNKNOWN:
                continue

            try:
                signal = strategy.analyze(state)
            except Exception:
                continue

            if not signal or signal.action.value != "enter":
                continue

            atr = atrs[i] if not math.isnan(atrs[i]) else price * 0.01
            if atr <= 0:
                continue

            entry_price = price
            pos_side = signal.direction
            if pos_side == Direction.LONG:
                stop = entry_price - self.atr_stop_mult * atr
                target = entry_price + self.atr_target_mult * atr
            elif pos_side == Direction.SHORT:
                stop = entry_price + self.atr_stop_mult * atr
                target = entry_price - self.atr_target_mult * atr
            else:
                continue
            in_pos = True
            bars_held = 0

        if result.trades > 0:
            arr = np.array(returns)
            result.total_return_pct = float(arr.sum())
            result.avg_return_pct = float(arr.mean())
            result.win_rate = result.wins / result.trades
            std = float(arr.std()) if len(arr) > 1 else 0.0
            result.sharpe = (result.avg_return_pct / std) if std > 0 else 0.0
            wins_arr = arr[arr > 0]
            losses_arr = arr[arr <= 0]
            avg_win = float(wins_arr.mean()) if len(wins_arr) else 0.0
            avg_loss = float(losses_arr.mean()) if len(losses_arr) else 0.0
            result.expectancy_pct = (
                result.win_rate * avg_win + (1 - result.win_rate) * avg_loss
            )
            result.max_drawdown_pct = max_dd * 100.0
            if std > 0 and len(arr) > 1:
                result.t_stat = float(arr.mean() / (std / math.sqrt(len(arr))))

        result.score = self._score(result)
        return result

    @staticmethod
    def _pnl_pct(entry: float, exit_price: float, side: Direction) -> float:
        if entry <= 0:
            return 0.0
        if side == Direction.LONG:
            return (exit_price - entry) / entry * 100.0
        return (entry - exit_price) / entry * 100.0

    def _score(self, r: BacktestResult) -> float:
        """Composite score: expectancy weighted by trade count, penalized by DD.

        Ranges roughly [-5, +5]. Pairs below min_trades are ineligible (-1.0).
        """
        if r.trades == 0 or r.trades < self.min_trades:
            return -1.0
        sample_conf = min(1.0, r.trades / 30.0)
        exp_component = r.expectancy_pct * sample_conf
        dd_penalty = min(2.0, r.max_drawdown_pct / 20.0)
        sharpe_bonus = max(-1.0, min(1.0, r.sharpe))
        raw = exp_component + sharpe_bonus - dd_penalty
        if r.t_stat < 1.0:
            return min(0.0, raw)
        return raw
