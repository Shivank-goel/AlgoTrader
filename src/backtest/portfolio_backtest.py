"""Backtester for cross-sectional (multi-symbol) strategies.

`QuickBacktester` walks one symbol and holds at most one position. A long/short
book needs every symbol advanced together, with costs charged on **turnover**
rather than per trade — rebalancing from 40% long ADA to 45% long ADA should
cost 5% of a leg, not a full round trip.

Lookahead control: weights decided at bar `i` are earned over `(i, i+hold]`.
The strategy only ever sees `panel` up to `i`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from src.backtest.costs import CostModel
from src.strategies.portfolio import PortfolioStrategy, PricePanel

logger = logging.getLogger(__name__)


@dataclass
class PortfolioResult:
    strategy: str
    symbols: list[str]
    rebalances: int = 0
    gross_return_pct: float = 0.0
    net_return_pct: float = 0.0
    costs_pct: float = 0.0
    mean_bps: float = 0.0
    win_rate: float = 0.0
    sharpe_annual: float = 0.0
    max_drawdown_pct: float = 0.0
    t_stat: float = 0.0
    avg_turnover: float = 0.0
    period_returns: list[float] = field(default_factory=list)
    timestamps: list[pd.Timestamp] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "rebalances": self.rebalances,
            "gross_return_pct": round(self.gross_return_pct, 3),
            "net_return_pct": round(self.net_return_pct, 3),
            "costs_pct": round(self.costs_pct, 3),
            "mean_bps": round(self.mean_bps, 2),
            "win_rate": round(self.win_rate, 4),
            "sharpe_annual": round(self.sharpe_annual, 3),
            "max_drawdown_pct": round(self.max_drawdown_pct, 3),
            "t_stat": round(self.t_stat, 3),
            "avg_turnover": round(self.avg_turnover, 3),
            **self.params,
        }


class PortfolioBacktester:
    """Rebalance a weight vector on a fixed cadence and account honestly."""

    def __init__(
        self,
        cost_model: Optional[CostModel] = None,
        *,
        bars_per_year: float = 8760.0,   # 1h bars
        maker_entry: bool = True,
    ) -> None:
        self.cost_model = cost_model if cost_model is not None else CostModel()
        self.bars_per_year = bars_per_year
        # A rebalance is entry-and-exit across the changed fraction of the book.
        self.cost_per_turnover_bps = (
            self.cost_model.entry_exit_fee_bps(maker_entry=maker_entry)
            + self.cost_model.base_slippage_bps
        )

    def run(
        self,
        strategy: PortfolioStrategy,
        panel: PricePanel,
        *,
        hold_bars: int,
        start: Optional[int] = None,
    ) -> PortfolioResult:
        warmup = max(strategy.min_history(), 1)
        start = warmup if start is None else max(start, warmup)

        result = PortfolioResult(
            strategy=strategy.name,
            symbols=panel.symbols,
            params={"hold_bars": hold_bars, **strategy.params},
        )
        if len(panel) <= start + hold_bars:
            return result

        previous = pd.Series(0.0, index=panel.close.columns)
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        nets: list[float] = []
        gross_total = 0.0
        cost_total = 0.0
        turnovers: list[float] = []

        for i in range(start, len(panel) - hold_bars, hold_bars):
            weights = strategy.target_weights(panel, i)
            if weights is None or weights.abs().sum() == 0:
                # Flat book still costs whatever it takes to unwind.
                turnover = float(previous.abs().sum())
                cost = turnover * self.cost_per_turnover_bps / 1e4
                if turnover:
                    nets.append(-cost)
                    cost_total += cost * 100
                    turnovers.append(turnover)
                previous = pd.Series(0.0, index=panel.close.columns)
                continue

            weights = weights.reindex(panel.close.columns).fillna(0.0)

            # Forward return over the holding period, per symbol.
            forward = (
                panel.close.iloc[i + hold_bars] / panel.close.iloc[i] - 1.0
            ).reindex(panel.close.columns).fillna(0.0)

            gross = float((weights * forward).sum())
            turnover = float((weights - previous).abs().sum())
            cost = turnover * self.cost_per_turnover_bps / 1e4
            net = gross - cost

            nets.append(net)
            gross_total += gross * 100
            cost_total += cost * 100
            turnovers.append(turnover)
            result.timestamps.append(panel.close.index[i])

            equity *= 1.0 + net
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)

            previous = weights

        if not nets:
            return result

        arr = np.asarray(nets, dtype=float)
        periods_per_year = self.bars_per_year / hold_bars

        result.period_returns = nets
        result.rebalances = len(arr)
        result.gross_return_pct = gross_total
        result.costs_pct = cost_total
        result.net_return_pct = float(arr.sum() * 100)
        result.mean_bps = float(arr.mean() * 1e4)
        result.win_rate = float((arr > 0).mean())
        result.max_drawdown_pct = float(max_dd * 100)
        result.avg_turnover = float(np.mean(turnovers)) if turnovers else 0.0

        sd = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
        if sd > 0:
            result.sharpe_annual = float(arr.mean() / sd * np.sqrt(periods_per_year))
            result.t_stat = float(arr.mean() / (sd / np.sqrt(len(arr))))

        return result

    def run_split(
        self,
        strategy: PortfolioStrategy,
        panel: PricePanel,
        *,
        hold_bars: int,
        n_splits: int = 2,
    ) -> list[PortfolioResult]:
        """Same run, evaluated over contiguous sub-periods.

        The pass mark requires profitability in both the bull and bear year, so
        the split has to be by time, not random.
        """
        bounds = np.linspace(0, len(panel), n_splits + 1, dtype=int)
        results = []
        for lo, hi in zip(bounds[:-1], bounds[1:]):
            sub = PricePanel(
                close=panel.close.iloc[lo:hi],
                returns=panel.returns.iloc[lo:hi],
            )
            results.append(self.run(strategy, sub, hold_bars=hold_bars))
        return results
