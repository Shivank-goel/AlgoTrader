"""Whole-share, long-only backtest for regime-gated NSE strategy families."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC

import pandas as pd

from src.fyers.costs import FyersCosts
from src.fyers.models import Side
from src.strategies.nse_regime_selector import RegimeAwareSelector, StrategyFamily


@dataclass(frozen=True)
class RegimeBacktestOutput:
    timestamps: list
    net_returns: list[float]
    baseline_returns: list[float]
    turnover: list[float]


def run_regime_backtest(closes: pd.DataFrame, selector: RegimeAwareSelector,
                        family: StrategyFamily, *, rebalance_bars: int,
                        half_spread_bps: float, costs: FyersCosts,
                        opens: pd.DataFrame | None = None,
                        benchmark_close: pd.Series | None = None,
                        eligible_at: Callable[[pd.Timestamp], set[str]] | None = None,
                        signal_closes: pd.DataFrame | None = None,
                        corporate_actions: dict[pd.Timestamp, list[dict]] | None = None,
                        ) -> RegimeBacktestOutput:
    """Signals at close i execute at open i+1; portfolio carries between rebalances."""
    if rebalance_bars < 1 or not 0 <= half_spread_bps <= 100:
        raise ValueError("invalid regime backtest execution settings")
    closes = selector._valid(closes)
    signal_closes = closes if signal_closes is None else selector._valid(signal_closes).reindex_like(closes)
    opens = closes if opens is None else selector._valid(opens).reindex_like(closes)
    if len(closes) < 253 + rebalance_bars + 2 or closes.shape[1] < 10:
        raise ValueError("regime backtest needs 10 symbols and sufficient history")
    cash, holdings = selector.config.capital_inr, {}
    prior_equity = cash
    timestamps, net_returns, baselines, turnovers = [], [], [], []
    action_cursor = -1

    def apply_actions(through: int) -> None:
        nonlocal cash, action_cursor
        if not corporate_actions:
            action_cursor = through
            return
        for index in range(action_cursor + 1, through + 1):
            for action in corporate_actions.get(closes.index[index], []):
                symbol = action["isin"]
                quantity = holdings.get(symbol, 0)
                if quantity:
                    cash += quantity * float(action.get("cash_per_share", 0))
                    holdings[symbol] = quantity * int(action.get("share_multiplier", 1))
        action_cursor = through

    for signal_i in range(252, len(closes) - rebalance_bars - 1, rebalance_bars):
        execution_i = signal_i + 1
        next_i = min(execution_i + rebalance_bars, len(closes) - 1)
        visible = signal_closes.iloc[:signal_i + 1]
        visible_benchmark = benchmark_close.iloc[:signal_i + 1] if benchmark_close is not None else None
        regime, confidence, _ = selector.regime(visible, visible_benchmark)
        allowed = family in selector.ELIGIBLE[regime] and confidence >= selector.config.min_regime_confidence
        scores = selector.scores(family, visible) if allowed else pd.Series(dtype=float)
        if eligible_at is not None:
            scores = scores[scores.index.isin(eligible_at(closes.index[signal_i]))]
        execution_mid = opens.iloc[execution_i]
        apply_actions(execution_i)
        target, _ = selector.whole_share_targets(scores, execution_mid)
        traded = 0.0
        for symbol in sorted(set(holdings) | set(target)):
            held, desired = holdings.get(symbol, 0), target.get(symbol, 0)
            delta = desired - held
            if not delta or not math.isfinite(execution_mid.get(symbol, math.nan)):
                continue
            mid = float(execution_mid[symbol])
            if delta < 0:
                price = mid * (1 - half_spread_bps / 10000)
                notional = -delta * price
                fee = costs.fee(notional, Side.SELL, delivery=True, charge_dp=True)
                cash += notional - fee
            else:
                price = mid * (1 + half_spread_bps / 10000)
                quantity = delta
                while quantity > 0:
                    notional = quantity * price
                    fee = costs.fee(notional, Side.BUY, delivery=True)
                    if notional + fee <= cash:
                        break
                    quantity -= 1
                delta = quantity
                if not delta:
                    continue
                notional = delta * price
                fee = costs.fee(notional, Side.BUY, delivery=True)
                cash -= notional + fee
                desired = held + delta
            traded += notional
            if desired:
                holdings[symbol] = desired
            else:
                holdings.pop(symbol, None)
        apply_actions(next_i)
        mark = closes.iloc[next_i]
        equity = cash + sum(quantity * float(mark[symbol]) for symbol, quantity in holdings.items())
        if equity <= 0:
            raise ValueError("regime backtest became insolvent")
        if benchmark_close is not None:
            baseline = float(benchmark_close.iloc[next_i] / benchmark_close.iloc[execution_i] - 1)
        else:
            baseline = float((mark / execution_mid - 1).replace(
                [math.inf, -math.inf], pd.NA,
            ).dropna().mean())
        net_returns.append(equity / prior_equity - 1)
        baselines.append(baseline)
        turnovers.append(traded / prior_equity)
        timestamp = closes.index[next_i]
        timestamps.append(timestamp.to_pydatetime().replace(tzinfo=UTC)
                          if timestamp.tzinfo is None else timestamp.to_pydatetime().astimezone(UTC))
        prior_equity = equity
    if holdings and net_returns:
        final_mid = closes.iloc[next_i]
        liquidation = cash
        exit_notional = 0.0
        for symbol, quantity in holdings.items():
            price = float(final_mid[symbol]) * (1 - half_spread_bps / 10000)
            notional = quantity * price
            liquidation += notional - costs.fee(notional, Side.SELL, delivery=True, charge_dp=True)
            exit_notional += notional
        net_returns[-1] = (1 + net_returns[-1]) * liquidation / prior_equity - 1
        turnovers[-1] += exit_notional / prior_equity
    return RegimeBacktestOutput(timestamps, net_returns, baselines, turnovers)
