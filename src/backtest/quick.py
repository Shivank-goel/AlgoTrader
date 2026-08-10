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

from src.backtest.costs import CostModel
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
    # Cost attribution — the difference between these two is the whole point
    # of modelling costs at all.
    gross_return_pct: float = 0.0
    costs_pct: float = 0.0
    # Signals whose passive entry never filled. High counts mean the maker-fee
    # saving is being paid for in missed participation.
    missed_entries: int = 0
    # Signals rejected because the requested stop was already through the fill.
    unfillable_stops: int = 0
    trade_log: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "trades": int(self.trades),
            "wins": int(self.wins),
            "losses": int(self.losses),
            "win_rate": round(float(self.win_rate), 4),
            "gross_return_pct": round(float(self.gross_return_pct), 3),
            "total_return_pct": round(float(self.total_return_pct), 3),
            "costs_pct": round(float(self.costs_pct), 3),
            "avg_return_pct": round(float(self.avg_return_pct), 4),
            "expectancy_pct": round(float(self.expectancy_pct), 4),
            "sharpe": round(float(self.sharpe), 3),
            "max_drawdown_pct": round(float(self.max_drawdown_pct), 3),
            "t_stat": round(float(self.t_stat), 3),
            "score": round(float(self.score), 4),
            "missed_entries": int(self.missed_entries),
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
        cost_model: Optional[CostModel] = None,
        notional_usd: float = 1_000.0,
        maker_entry: bool = False,
        honor_signal_levels: bool = False,
    ) -> None:
        self.indicators = indicators or IndicatorEngine()
        self.min_bars = min_bars
        self.step = step
        self.atr_stop_mult = atr_stop_mult
        self.atr_target_mult = atr_target_mult
        self.max_hold_bars = max_hold_bars
        self.risk_per_trade_pct = risk_per_trade_pct
        self.min_trades = min_trades
        # Same model the live paper path uses. Pass CostModel.zero() to measure
        # the gross strategy signal in isolation.
        self.cost_model = cost_model if cost_model is not None else CostModel()
        # Reference order size for the size-vs-liquidity slippage term.
        self.notional_usd = notional_usd
        # Passive (post-only) entries pay the maker fee and no slippage, but
        # only fill if price comes back to them. Exits stay aggressive.
        self.maker_entry = maker_entry
        # Off by default: turning it on changes every historical baseline,
        # because BaseStrategy.get_take_profit is hardcoded to 2x the stop
        # multiplier and so moves the target from 3xATR to 4xATR. Required for
        # any strategy whose stop placement is the hypothesis under test.
        self.honor_signal_levels = honor_signal_levels

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
        # One pass over the frame instead of rebuilding state per bar.
        prepared = builder.prepare(symbol, df)

        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)
        atrs = df["atr_14"].to_numpy(dtype=float) if "atr_14" in df.columns else np.full(len(df), np.nan)
        volumes = (
            df["volume"].to_numpy(dtype=float)
            if "volume" in df.columns
            else np.full(len(df), np.nan)
        )
        timestamps = df.index.to_pydatetime() if isinstance(df.index, pd.DatetimeIndex) else None

        in_pos = False
        pos_side: Optional[Direction] = None
        entry_price = 0.0
        entry_idx = 0
        stop = 0.0
        target = 0.0
        bars_held = 0
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        returns: list[float] = []
        gross_returns: list[float] = []

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
                    # Slip the exit too. Treating a target as an exact limit
                    # fill is the optimism bias most backtests leave in: a stop
                    # or target is a market exit in practice, and pretending
                    # otherwise credits the strategy with free execution.
                    filled_exit = self.cost_model.exit_fill_price(
                        exit_price,
                        pos_side,
                        atr=None if math.isnan(atrs[i]) else atrs[i],
                        bar_volume_usd=self._bar_volume_usd(volumes, closes, i),
                        notional=self.notional_usd,
                    )
                    gross_pct = self._pnl_pct(entry_price, filled_exit, pos_side)

                    cost_pct = self.cost_model.round_trip_cost_pct(
                        entry_price=entry_price,
                        exit_price=filled_exit,
                        direction=pos_side,
                        entry_ts=timestamps[entry_idx] if timestamps is not None else None,
                        exit_ts=timestamps[i] if timestamps is not None else None,
                        size=self.notional_usd / entry_price if entry_price > 0 else 1.0,
                        maker_entry=self.maker_entry,
                    )
                    pnl_pct = gross_pct - cost_pct

                    equity *= 1.0 + (pnl_pct / 100.0) * (self.risk_per_trade_pct / 100.0)
                    peak = max(peak, equity)
                    dd = (peak - equity) / peak if peak > 0 else 0.0
                    max_dd = max(max_dd, dd)
                    returns.append(pnl_pct)
                    gross_returns.append(gross_pct)
                    result.trade_pnls.append(pnl_pct)
                    result.trades += 1
                    if pnl_pct > 0:
                        result.wins += 1
                    else:
                        result.losses += 1
                    result.trade_log.append(
                        {
                            "entry_idx": entry_idx,
                            "exit_idx": i,
                            "entry_ts": timestamps[entry_idx] if timestamps is not None else None,
                            "exit_ts": timestamps[i] if timestamps is not None else None,
                            "side": pos_side.value,
                            "entry_price": entry_price,
                            "exit_price": filled_exit,
                            "gross_pct": gross_pct,
                            "cost_pct": cost_pct,
                            "net_pct": pnl_pct,
                            "reason": exit_reason,
                        }
                    )
                    in_pos = False
                    pos_side = None
                    bars_held = 0
                    continue

            if in_pos:
                continue

            try:
                state = builder.build_at(prepared, i)
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

            pos_side = signal.direction

            if self.maker_entry:
                # Post-only limit resting at this bar's close, checked against
                # the NEXT bar. It fills only if price trades back through the
                # level; otherwise the trade is missed.
                #
                # Modelling the miss is the whole point. Booking a maker fee
                # while assuming every entry fills would hand the strategy the
                # cheaper fee AND perfect participation, which is precisely the
                # adverse selection that makes real passive entries underperform
                # the naive fee arithmetic: the trades you miss are the ones
                # that ran away in your favour.
                if i + 1 >= n:
                    continue
                limit_price = price
                if pos_side == Direction.LONG:
                    filled = lows[i + 1] <= limit_price
                else:
                    filled = highs[i + 1] >= limit_price
                if not filled:
                    result.missed_entries += 1
                    continue
                entry_price = limit_price
                entry_idx = i + 1
            else:
                # Aggressive entry: slipped, and the stop/target are set from
                # the actual fill rather than the unattainable mid.
                entry_price = self.cost_model.entry_fill_price(
                    price,
                    pos_side,
                    atr=atr,
                    bar_volume_usd=self._bar_volume_usd(volumes, closes, i),
                    notional=self.notional_usd,
                )
                entry_idx = i
            if pos_side not in (Direction.LONG, Direction.SHORT):
                continue

            stop, target = self._levels_for(signal, entry_price, pos_side, atr)

            # A structural stop can sit inside the slippage the entry just paid.
            # Booking an instant stop-out would credit the strategy with a loss
            # it never had the chance to avoid; skip the trade instead.
            if (pos_side == Direction.LONG and stop >= entry_price) or (
                pos_side == Direction.SHORT and stop <= entry_price
            ):
                result.unfillable_stops += 1
                continue

            in_pos = True
            bars_held = 0

        if result.trades > 0:
            arr = np.array(returns)
            result.gross_return_pct = float(np.array(gross_returns).sum())
            result.costs_pct = result.gross_return_pct - float(arr.sum())
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

    def _levels_for(
        self,
        signal,
        entry_price: float,
        side: Direction,
        atr: float,
    ) -> tuple[float, float]:
        """Stop and target for a new position.

        With `honor_signal_levels`, a strategy's own stop/target win — which is
        the only way to evaluate a strategy whose stop placement *is* its edge.
        Levels are re-anchored to the actual fill so entry slippage does not
        silently tighten longs and loosen shorts, and each falls back to the ATR
        multiple when the strategy leaves it unset.

        Without the flag this reproduces the historical behaviour exactly: a
        uniform ATR trade regardless of what the strategy asked for.
        """
        default_stop = (
            entry_price - self.atr_stop_mult * atr
            if side == Direction.LONG
            else entry_price + self.atr_stop_mult * atr
        )
        default_target = (
            entry_price + self.atr_target_mult * atr
            if side == Direction.LONG
            else entry_price - self.atr_target_mult * atr
        )

        if not self.honor_signal_levels:
            return default_stop, default_target

        reference = signal.entry_price or entry_price
        shift = entry_price - reference

        stop = default_stop
        if signal.stop_loss:
            stop = signal.stop_loss + shift

        target = default_target
        if signal.take_profit:
            target = signal.take_profit + shift

        return stop, target

    @staticmethod
    def _bar_volume_usd(volumes, closes, i: int) -> Optional[float]:
        """Approximate USD traded in bar i, for the slippage impact term."""
        vol = volumes[i]
        if vol is None or math.isnan(vol) or vol <= 0:
            return None
        return float(vol) * float(closes[i])

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
