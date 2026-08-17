"""Backtester for daily-rebalanced, open-to-close intraday books.

Distinct from `PortfolioBacktester`, which holds across N bars on a close-price
panel. Here every position opens at today's open and closes at today's close, so
the return is open-to-close and turnover is charged against the *previous*
session's book — the previous day's positions are liquidated at that day's close
and today's are established at today's open.

Costs use `NSEEquityCostModel`, which is asymmetric (STT on sells, stamp duty on
buys) and capped per order, so a turnover figure alone is not enough: cost is
computed per leg on actual rupee notional.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from src.backtest.nse_costs import NSEEquityCostModel
from src.core.models import OrderSide
from src.strategies.nse_intraday_xs import IntradayCrossSectional

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252


@dataclass
class IntradayResult:
    strategy: str = "nse_intraday_xs"
    sessions: int = 0
    gross_return_pct: float = 0.0
    net_return_pct: float = 0.0
    costs_pct: float = 0.0
    mean_bps: float = 0.0
    win_rate: float = 0.0
    sharpe_annual: float = 0.0
    t_stat: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_turnover: float = 0.0
    avg_names_traded: float = 0.0
    daily_returns: list[float] = field(default_factory=list)
    dates: list[pd.Timestamp] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "sessions": self.sessions,
            "gross_return_pct": round(self.gross_return_pct, 3),
            "net_return_pct": round(self.net_return_pct, 3),
            "costs_pct": round(self.costs_pct, 3),
            "mean_bps": round(self.mean_bps, 3),
            "win_rate": round(self.win_rate, 4),
            "sharpe_annual": round(self.sharpe_annual, 3),
            "t_stat": round(self.t_stat, 3),
            "max_drawdown_pct": round(self.max_drawdown_pct, 3),
            "avg_turnover": round(self.avg_turnover, 4),
            **self.params,
        }


class IntradayCrossSectionalBacktester:
    """Open-to-close simulation with per-leg NSE costs."""

    def __init__(
        self,
        cost_model: Optional[NSEEquityCostModel] = None,
        *,
        capital_inr: float = 10_000.0,
        leverage: float = 4.0,
        squares_off_daily: bool = True,
    ) -> None:
        self.cost_model = cost_model or NSEEquityCostModel()
        self.capital_inr = capital_inr
        self.leverage = leverage
        # MIS positions CANNOT be carried overnight (K-10): the broker squares
        # them off before the close. So every position is a full round trip
        # every session, whether or not the name stays in the book.
        #
        # Charging only the change in the book — `book - held` — is correct for
        # a positional strategy that carries inventory, and badly wrong here: it
        # made a book that keeps 18% of its names overnight look 18% cheaper
        # than it can possibly be. Set False only for a strategy that genuinely
        # holds (delivery/CNC), which for a long/short book is impossible in
        # Indian cash equity.
        self.squares_off_daily = squares_off_daily

    @property
    def gross_notional(self) -> float:
        return self.capital_inr * self.leverage

    def _turnover_cost_pct(self, weight_delta: pd.Series) -> float:
        """Cost of moving the book by `weight_delta`, as a % of gross notional.

        Charged per leg on real rupee amounts so the per-order brokerage cap and
        the buy/sell asymmetry both apply. Increasing a long and decreasing a
        short are both buys; the sign of the delta gives the side.
        """
        if weight_delta.abs().sum() == 0:
            return 0.0

        total_inr = 0.0
        for delta in weight_delta:
            if delta == 0:
                continue
            notional = abs(delta) * self.gross_notional
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            total_inr += self.cost_model.leg_fee_inr(notional, side)

        return total_inr / self.gross_notional * 100.0

    def run(
        self,
        strategy: IntradayCrossSectional,
        opens: pd.DataFrame,
        closes: pd.DataFrame,
        *,
        start: Optional[int] = None,
    ) -> IntradayResult:
        result = IntradayResult(
            params={
                "signal": strategy.signal.value,
                "tilt": strategy.tilt.value,
                "n_legs": strategy.n_legs,
                "formation_days": strategy.formation_days,
                "rebalance_band": strategy.rebalance_band,
            }
        )

        warmup = max(strategy.min_history(), 2)
        start = warmup if start is None else max(start, warmup)
        if len(closes) <= start + 1:
            return result

        held = pd.Series(0.0, index=closes.columns)
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        nets: list[float] = []
        gross_total = 0.0
        cost_total = 0.0
        turnovers: list[float] = []
        names_traded: list[int] = []

        for i in range(start, len(closes)):
            target = strategy.target_weights(opens, closes, i)
            book = strategy.apply_rebalance_band(target, held)

            if book.abs().sum() == 0:
                # Unwind whatever is open; that still costs.
                delta = -held
                cost = self._turnover_cost_pct(delta)
                if cost:
                    nets.append(-cost / 100.0)
                    cost_total += cost
                held = pd.Series(0.0, index=closes.columns)
                continue

            # Open-to-close return per name for this session.
            session = (closes.iloc[i] / opens.iloc[i] - 1.0).reindex(book.index).fillna(0.0)
            session = session.replace([np.inf, -np.inf], 0.0)

            gross = float((book * session).sum())

            if self.squares_off_daily:
                # Open the whole book at the open and close it at the close:
                # one full round trip on every position, every session.
                cost_pct = self._turnover_cost_pct(book) + self._turnover_cost_pct(-book)
                delta = book * 2.0   # for reporting: gross traded, both legs
            else:
                delta = book - held
                cost_pct = self._turnover_cost_pct(delta)

            net = gross - cost_pct / 100.0

            nets.append(net)
            gross_total += gross * 100
            cost_total += cost_pct
            turnovers.append(float(delta.abs().sum()))
            names_traded.append(int((delta.abs() > 0).sum()))
            result.dates.append(closes.index[i])

            equity *= 1.0 + net
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)

            # MIS squares off at the close, so tomorrow starts flat. The band
            # still compares against today's book: keeping a name means
            # re-establishing it, which is what `delta` prices.
            held = book

        if not nets:
            return result

        arr = np.asarray(nets, dtype=float)
        result.daily_returns = nets
        result.sessions = len(arr)
        result.gross_return_pct = gross_total
        result.costs_pct = cost_total
        result.net_return_pct = float(arr.sum() * 100)
        result.mean_bps = float(arr.mean() * 1e4)
        result.win_rate = float((arr > 0).mean())
        result.max_drawdown_pct = float(max_dd * 100)
        result.avg_turnover = float(np.mean(turnovers)) if turnovers else 0.0
        result.avg_names_traded = float(np.mean(names_traded)) if names_traded else 0.0

        sd = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
        if sd > 0:
            result.sharpe_annual = float(arr.mean() / sd * np.sqrt(TRADING_DAYS_PER_YEAR))
            result.t_stat = float(arr.mean() / (sd / np.sqrt(len(arr))))

        return result

    def run_split(
        self,
        strategy: IntradayCrossSectional,
        opens: pd.DataFrame,
        closes: pd.DataFrame,
        *,
        n_splits: int = 2,
    ) -> list[IntradayResult]:
        """Contiguous sub-periods — the pass mark needs both halves positive."""
        bounds = np.linspace(0, len(closes), n_splits + 1, dtype=int)
        return [
            self.run(strategy, opens.iloc[lo:hi], closes.iloc[lo:hi])
            for lo, hi in zip(bounds[:-1], bounds[1:])
        ]
