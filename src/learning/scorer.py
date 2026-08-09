"""Strategy performance scoring with composite metrics."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from src.core.models import TradeRecord

logger = logging.getLogger(__name__)

WEIGHTS = {
    "sharpe": 0.30,
    "sortino": 0.20,
    "win_payoff": 0.20,
    "profit_factor": 0.15,
    "max_drawdown": 0.15,
}


class PerformanceScorer:
    """Scores strategies using rolling trade history."""

    def __init__(self, lookback_trades: int = 200, min_trades: int = 10) -> None:
        self.lookback_trades = lookback_trades
        self.min_trades = min_trades

    def score_strategy(self, trades: list[TradeRecord]) -> float:
        if not trades:
            return 0.5

        recent = trades[-self.lookback_trades :]
        if len(recent) < self.min_trades:
            return 0.5

        returns = [t.pnl_pct / 100 for t in recent if t.pnl_pct != 0]

        if not returns:
            return 0.5

        metrics = self._compute_metrics(recent, returns)
        score = sum(metrics[k] * WEIGHTS[k] for k in WEIGHTS)
        return max(0.0, min(1.0, score))

    def score_all(
        self, trades_by_strategy: dict[str, list[TradeRecord]]
    ) -> dict[str, float]:
        return {
            name: self.score_strategy(trades)
            for name, trades in trades_by_strategy.items()
        }

    def _compute_metrics(
        self, trades: list[TradeRecord], returns: list[float]
    ) -> dict[str, float]:
        arr = np.array(returns)
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]

        sharpe = self._sharpe(arr)
        sortino = self._sortino(arr)
        win_rate = len(wins) / len(trades) if trades else 0
        avg_win = np.mean([t.pnl for t in wins]) if wins else 0
        avg_loss = abs(np.mean([t.pnl for t in losses])) if losses else 1
        payoff = avg_win / avg_loss if avg_loss > 0 else 0
        win_payoff = min(1.0, win_rate * payoff / 2)

        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        profit_factor = min(1.0, gross_profit / gross_loss) if gross_loss > 0 else 1.0

        max_dd = self._max_drawdown(arr)
        dd_score = max(0.0, 1.0 - max_dd)

        return {
            "sharpe": self._normalize(sharpe, -1, 3),
            "sortino": self._normalize(sortino, -1, 4),
            "win_payoff": win_payoff,
            "profit_factor": profit_factor,
            "max_drawdown": dd_score,
        }

    @staticmethod
    def _sharpe(returns: np.ndarray, annualize: int = 252) -> float:
        if len(returns) < 2 or returns.std() == 0:
            return 0.0
        return float(returns.mean() / returns.std() * np.sqrt(annualize))

    @staticmethod
    def _sortino(returns: np.ndarray, annualize: int = 252) -> float:
        downside = returns[returns < 0]
        if len(downside) < 1 or downside.std() == 0:
            return 0.0
        return float(returns.mean() / downside.std() * np.sqrt(annualize))

    @staticmethod
    def _max_drawdown(returns: np.ndarray) -> float:
        cumulative = np.cumprod(1 + returns)
        peak = np.maximum.accumulate(cumulative)
        drawdown = (peak - cumulative) / peak
        return float(drawdown.max()) if len(drawdown) > 0 else 0.0

    @staticmethod
    def _normalize(value: float, low: float, high: float) -> float:
        if high == low:
            return 0.5
        return max(0.0, min(1.0, (value - low) / (high - low)))
