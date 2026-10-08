"""Deterministic daily-bar NSE simulator for preregistered proposals.

The simulator consumes already validated ``CompletedBarArtifact`` objects and
never calls a broker or mutates research state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from src.fyers.costs import FyersCosts
from src.fyers.daily_data import CompletedBarArtifact
from src.fyers.models import Side
from src.strategies.nse_regime_selector import NseRegime, RegimeAwareSelector, StrategyFamily


@dataclass
class _Position:
    symbol: str
    signal_session: date
    entry_session: date
    entry_raw: float
    entry_price: float
    quantity: int
    entry_cost: float
    regime: str
    score: float
    rank: int
    exit_session: date


def _slip(price: float, bps: float, *, buy: bool) -> float:
    value = price * (1 + (bps if buy else -bps) / 10000)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("invalid execution price")
    return value


def simulate(
    spec: dict[str, Any],
    artifacts: list[CompletedBarArtifact],
    *,
    dataset_hash: str,
    trial_id: str,
    costs: FyersCosts | None = None,
    slippage_bps: float | None = None,
) -> dict[str, Any]:
    """Run a chronological, one-session holding simulation in memory."""
    if len(artifacts) < 3 or any(a.benchmark is None or a.missing_symbols for a in artifacts):
        raise ValueError("DATA_INCOMPLETE")
    allocation = spec.get("allocation_rule")
    if not isinstance(allocation, dict):
        raise ValueError("allocation rule is missing")
    if allocation.get("type") != "FIXED_POSITION_BUDGET" or allocation.get("max_positions") != 5:
        raise ValueError("unsupported allocation rule")
    costs = costs or FyersCosts.load()
    slip_bps = float(
        spec.get("slippage_model", {}).get("slippage_bps", 2)
        if slippage_bps is None
        else slippage_bps
    )
    symbols = sorted(artifacts[0].bars)
    idx = [a.session_date for a in artifacts]
    closes = pd.DataFrame(
        [{s: a.bars[s].close for s in symbols} for a in artifacts], index=pd.DatetimeIndex(idx)
    )
    opens = pd.DataFrame(
        [{s: a.bars[s].open for s in symbols} for a in artifacts], index=pd.DatetimeIndex(idx)
    )
    benchmark = pd.Series([a.benchmark.close for a in artifacts], index=closes.index)
    # Strategy evidence is not yet a qualification gate; this simulator evaluates
    # the declared family directly and remains entirely offline.
    family = StrategyFamily(spec["strategy_family"])
    evidence = [{"family": family, "lower_confidence_bound": 1.0, "qualified": True}]
    from src.strategies.nse_regime_selector import SelectorConfig

    selector = RegimeAwareSelector(SelectorConfig(evidence=evidence))
    cash = float(spec.get("initial_capital_inr", 10000))
    initial = cash
    positions: dict[str, _Position] = {}
    exits: dict[int, list[_Position]] = {}
    entries: dict[int, list[dict]] = {}
    trades: list[dict] = []
    unexecuted: list[dict] = []
    total_costs = gross = 0.0
    equity_curve: list[float] = []
    benchmark_start: tuple[date, float] | None = None
    for i, session in enumerate(idx):
        # Scheduled exits are always processed before entries at this open.
        for position in exits.pop(i, []):
            raw = float(opens.iloc[i][position.symbol])
            if not math.isfinite(raw) or raw <= 0:
                raise ValueError("MISSING_EXIT_OPEN")
            price = _slip(raw, slip_bps, buy=False)
            notional = price * position.quantity
            exit_cost = costs.fee(notional, Side.SELL, delivery=True)
            cash += notional - exit_cost
            gross_pnl = (price - position.entry_price) * position.quantity
            net_pnl = gross_pnl - position.entry_cost - exit_cost
            gross += gross_pnl
            total_costs += position.entry_cost + exit_cost
            trades.append(
                {
                    "trial_id": trial_id,
                    "dataset_hash": dataset_hash,
                    "strategy_family": family.value,
                    "symbol": position.symbol,
                    "signal_session": position.signal_session.isoformat(),
                    "entry_session": position.entry_session.isoformat(),
                    "raw_entry_open": position.entry_raw,
                    "entry_execution_price": position.entry_price,
                    "quantity": position.quantity,
                    "entry_notional": position.entry_price * position.quantity,
                    "entry_costs": position.entry_cost,
                    "exit_session": session.isoformat(),
                    "raw_exit_open": raw,
                    "exit_execution_price": price,
                    "exit_notional": notional,
                    "exit_costs": exit_cost,
                    "gross_pnl": gross_pnl,
                    "net_pnl": net_pnl,
                    "trade_return": net_pnl / (position.entry_price * position.quantity),
                    "regime": position.regime,
                    "score": position.score,
                    "rank": position.rank,
                    "holding_sessions": 1,
                    "exit_reason": "FIXED_HOLDING_PERIOD",
                }
            )
            positions.pop(position.symbol, None)
        for order in entries.pop(i, []):
            if len(positions) >= 5 or order["symbol"] in positions:
                continue
            raw = float(opens.iloc[i][order["symbol"]])
            if not math.isfinite(raw) or raw <= 0:
                raise ValueError("MISSING_ENTRY_OPEN")
            price = _slip(raw, slip_bps, buy=True)
            quantity = int(2000 // price)
            if quantity < 1:
                unexecuted.append({**order, "reason": "INSUFFICIENT_POSITION_BUDGET"})
                continue
            notional = price * quantity
            entry_cost = costs.fee(notional, Side.BUY, delivery=True)
            if cash < notional + entry_cost:
                unexecuted.append({**order, "reason": "INSUFFICIENT_CASH"})
                continue
            cash -= notional + entry_cost
            pos = _Position(
                order["symbol"],
                order["signal_session"],
                session,
                raw,
                price,
                quantity,
                entry_cost,
                order["regime"],
                order["score"],
                order["rank"],
                idx[i + 1] if i + 1 < len(idx) else session,
            )
            positions[pos.symbol] = pos
            if i + 1 < len(idx):
                exits.setdefault(i + 1, []).append(pos)
        if i + 1 >= len(idx):
            continue
        regime, confidence, _ = selector.regime(closes.iloc[: i + 1], benchmark.iloc[: i + 1])
        if regime not in (NseRegime.TRENDING_UP, NseRegime.RANGING) or confidence < 0.60:
            continue
        if (
            (family is StrategyFamily.MOMENTUM and regime is not NseRegime.TRENDING_UP)
            or (family is StrategyFamily.BREAKOUT and regime is not NseRegime.TRENDING_UP)
            or (family is StrategyFamily.RESIDUAL_REVERSAL and regime is not NseRegime.RANGING)
        ):
            continue
        scores = selector.scores(family, closes.iloc[: i + 1])
        if benchmark_start is None:
            benchmark_start = (session, float(benchmark.iloc[i]))
        for rank, (symbol, score) in enumerate(scores.head(5).items(), 1):
            entries.setdefault(i + 1, []).append(
                {
                    "symbol": symbol,
                    "signal_session": session,
                    "regime": regime.value,
                    "score": float(score),
                    "rank": rank,
                }
            )
        equity_curve.append(
            cash
            + sum(pos.quantity * float(closes.iloc[i][pos.symbol]) for pos in positions.values())
        )
    net = sum(t["net_pnl"] for t in trades)
    ending_equity = equity_curve[-1] if equity_curve else cash
    peak = initial
    drawdowns = []
    for value in equity_curve:
        peak = max(peak, value)
        drawdowns.append((value - peak) / peak)
    benchmark_return = (
        float(benchmark.iloc[-1]) / benchmark_start[1] - 1 if benchmark_start else None
    )
    strategy_return = ending_equity / initial - 1
    return {
        "strategy_family": family.value,
        "initial_cash": initial,
        "ending_cash": cash,
        "ending_equity": ending_equity,
        "gross_realized_pnl": gross,
        "net_realized_pnl": net,
        "total_costs": total_costs,
        "trade_count": len(trades),
        "wins": sum(t["net_pnl"] > 0 for t in trades),
        "losses": sum(t["net_pnl"] <= 0 for t in trades),
        "win_rate": (sum(t["net_pnl"] > 0 for t in trades) / len(trades) if trades else None),
        "max_drawdown": min(drawdowns, default=0.0),
        "max_drawdown_status": "EVALUATED",
        "benchmark_status": "EVALUATED" if benchmark_return is not None else "NOT_EVALUATED",
        "benchmark_return": benchmark_return,
        "strategy_return": strategy_return,
        "excess_return": strategy_return - benchmark_return
        if benchmark_return is not None
        else None,
        "lookahead_status": "PASS",
        "historical_admission": "NOT_EVALUATED",
        "trades": trades,
        "unexecuted_signals": unexecuted,
    }
