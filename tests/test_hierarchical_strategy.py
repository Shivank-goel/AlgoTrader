import numpy as np
import pandas as pd

from src.strategies.hierarchical import (
    Action,
    StockState,
    classify_stock,
    rank_stocks,
    route_candidate,
)
from src.strategies.nse_regime_selector import NseRegime


def series(values):
    return pd.Series(values, index=pd.date_range("2025-01-01", periods=len(values), freq="B"))


def test_stock_state_is_point_in_time_and_interpretable():
    market = series(np.linspace(100, 110, 60))
    stock = series(np.linspace(100, 140, 60))
    result = classify_stock(stock, market)
    assert result["state"] == StockState.STRONG_UPTREND.value
    assert result["relative_strength"] > 0


def test_cross_sectional_ranks_are_deterministic():
    index = pd.date_range("2025-01-01", periods=60, freq="B")
    closes = pd.DataFrame({"A": np.linspace(100, 140, 60), "B": np.linspace(100, 110, 60)}, index=index)
    rows = rank_stocks(closes, pd.Series(np.linspace(100, 110, 60), index=index))
    assert rows["A"]["relative_strength_rank"] == 1
    assert rows["B"]["relative_weakness_rank"] == 1


def test_falling_market_abstains_even_for_strong_stock():
    result = route_candidate(NseRegime.FALLING, {"state": StockState.STRONG_UPTREND.value})
    assert result["action"] == Action.HOLD.value and result["risk_multiplier"] == 0


def test_trending_market_routes_only_eligible_stock_state():
    result = route_candidate(NseRegime.TRENDING_UP, {"state": StockState.STRONG_UPTREND.value})
    assert result["action"] == Action.LONG.value
