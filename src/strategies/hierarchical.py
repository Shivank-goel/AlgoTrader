"""Interpretable, point-in-time stock states, ranks, and regime routing."""

from __future__ import annotations

import math
from enum import Enum

import pandas as pd

from src.fyers.intraday import IntradayFeatures
from src.strategies.nse_regime_selector import NseRegime, StrategyFamily


class StockState(str, Enum):
    STRONG_UPTREND = "strong_uptrend"
    WEAK_UPTREND = "weak_uptrend"
    RANGE = "range"
    WEAK_DOWNTREND = "weak_downtrend"
    STRONG_DOWNTREND = "strong_downtrend"
    UNSTABLE = "unstable"


class Action(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    HOLD = "HOLD"


class PortfolioAllocator:
    """Whole-share, capped allocation used after signal qualification."""

    def __init__(self, *, capital_inr: float, per_position_inr: float,
                 max_positions: int = 5) -> None:
        if capital_inr <= 0 or per_position_inr <= 0 or max_positions < 1:
            raise ValueError("portfolio limits must be positive")
        self.capital_inr, self.per_position_inr, self.max_positions = capital_inr, per_position_inr, max_positions

    def allocate(self, candidates: list[tuple[str, float, float]]) -> tuple[dict[str, int], list[dict]]:
        """Candidates are (symbol, score, price), sorted deterministically by score."""
        targets, rejected = {}, []
        spent = 0.0
        for symbol, _score, price in sorted(candidates, key=lambda row: (-row[1], row[0])):
            if len(targets) >= self.max_positions:
                rejected.append({"symbol": symbol, "reason": "position_limit"})
                continue
            if not math.isfinite(price) or price <= 0:
                rejected.append({"symbol": symbol, "reason": "invalid_price"})
                continue
            quantity = int(self.per_position_inr // price)
            if quantity < 1 or spent + quantity * price > self.capital_inr:
                rejected.append({"symbol": symbol, "reason": "unaffordable"})
                continue
            targets[symbol] = quantity
            spent += quantity * price
        return targets, rejected


def _clean(values: pd.Series) -> pd.Series:
    result = pd.to_numeric(values, errors="coerce").replace([math.inf, -math.inf], math.nan).dropna()
    if (result <= 0).any():
        raise ValueError("prices must be positive")
    return result


def classify_stock(close: pd.Series, benchmark: pd.Series, *, volatility_limit: float = .08) -> dict:
    """Classify from data through the last supplied completed observation only."""
    if volatility_limit <= 0 or len(close) < 30:
        return {"state": StockState.UNSTABLE.value, "reason": "insufficient_history"}
    prices, market = _clean(close), _clean(benchmark)
    aligned = pd.concat([prices.rename("stock"), market.rename("market")], axis=1).dropna()
    if len(aligned) < 30:
        return {"state": StockState.UNSTABLE.value, "reason": "benchmark_history_unavailable"}
    returns = aligned.stock.pct_change().dropna()
    volatility = float(returns.iloc[-20:].std()) if len(returns) >= 20 else float("inf")
    short = float(aligned.stock.iloc[-1] / aligned.stock.iloc[-10] - 1)
    medium = float(aligned.stock.iloc[-1] / aligned.stock.iloc[-30] - 1)
    relative = float((aligned.stock.iloc[-1] / aligned.stock.iloc[-30]) /
                     (aligned.market.iloc[-1] / aligned.market.iloc[-30]) - 1)
    baseline = float(aligned.stock.iloc[-1] / aligned.stock.iloc[-20:].mean() - 1)
    if not all(math.isfinite(value) for value in (volatility, short, medium, relative, baseline)) or volatility > volatility_limit:
        state = StockState.UNSTABLE
    elif medium > .05 and short > 0 and baseline > 0:
        state = StockState.STRONG_UPTREND
    elif medium > 0 and relative >= 0:
        state = StockState.WEAK_UPTREND
    elif medium < -.05 and short < 0 and baseline < 0:
        state = StockState.STRONG_DOWNTREND
    elif medium < 0 and relative <= 0:
        state = StockState.WEAK_DOWNTREND
    else:
        state = StockState.RANGE
    return {"state": state.value, "short_trend": short, "medium_trend": medium,
            "relative_strength": relative, "volatility": volatility,
            "distance_from_baseline": baseline}


def rank_stocks(closes: pd.DataFrame, benchmark: pd.Series) -> dict[str, dict]:
    """Return stable cross-sectional ranks and scores at the final timestamp."""
    if closes.empty or closes.index.has_duplicates or not closes.index.is_monotonic_increasing:
        raise ValueError("closes must be non-empty, ordered, and unique")
    rows = {}
    for symbol in closes.columns:
        state = classify_stock(closes[symbol], benchmark)
        score = float(state.get("relative_strength", float("nan")))
        rows[symbol] = {**state, "momentum_score": score,
                        "long_score": score, "short_score": -score}
    valid = sorted((symbol for symbol, row in rows.items() if math.isfinite(row["momentum_score"])),
                   key=lambda symbol: (-rows[symbol]["momentum_score"], symbol))
    weak = sorted(valid, key=lambda symbol: (rows[symbol]["momentum_score"], symbol))
    for rank, symbol in enumerate(valid, 1):
        rows[symbol]["relative_strength_rank"] = rank
    for rank, symbol in enumerate(weak, 1):
        rows[symbol]["relative_weakness_rank"] = rank
    for symbol in rows:
        rows[symbol].setdefault("relative_strength_rank", None)
        rows[symbol].setdefault("relative_weakness_rank", None)
    return rows


def route_candidate(market_regime: NseRegime | str, stock: dict,
                    features: IntradayFeatures | None = None) -> dict:
    """Route one stock to an auditable candidate; never submits or schedules orders."""
    regime = NseRegime(market_regime)
    state = StockState(stock["state"])
    if features is not None and (features.spread_bps is None or not features.warm):
        return {"strategy_family": None, "action": Action.HOLD.value,
                "risk_multiplier": 0.0, "reason": "feature_warmup_or_spread_unavailable"}
    if regime in {NseRegime.UNKNOWN, NseRegime.VOLATILE, NseRegime.FALLING}:
        return {"strategy_family": None, "action": Action.HOLD.value,
                "risk_multiplier": 0.0, "reason": "market_regime_blocks_new_entries"}
    if regime is NseRegime.TRENDING_UP and state in {StockState.STRONG_UPTREND, StockState.WEAK_UPTREND}:
        family = StrategyFamily.MOMENTUM.value if state is StockState.STRONG_UPTREND else StrategyFamily.BREAKOUT.value
        return {"strategy_family": family, "action": Action.LONG.value,
                "risk_multiplier": 1.0 if state is StockState.STRONG_UPTREND else .5,
                "reason": "uptrend_with_qualified_regime_candidate"}
    if regime is NseRegime.RANGING and state is StockState.RANGE:
        return {"strategy_family": StrategyFamily.RESIDUAL_REVERSAL.value, "action": Action.LONG.value,
                "risk_multiplier": .5, "reason": "range_reversion_candidate"}
    return {"strategy_family": None, "action": Action.HOLD.value,
            "risk_multiplier": 0.0, "reason": "stock_state_not_eligible"}
