"""Walk-forward optimization for strategy parameters."""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import pandas as pd

from src.learning.scorer import PerformanceScorer
from src.market.state import MarketStateBuilder
from src.strategies.selector import StrategySelector

logger = logging.getLogger(__name__)


class WalkForwardOptimizer:
    """Grid search with in-sample / out-of-sample validation."""

    def __init__(
        self,
        strategy_config: dict[str, Any],
        is_window_days: int = 90,
        oos_window_days: int = 30,
        min_oos_sharpe_ratio: float = 0.5,
        max_param_change_pct: float = 0.30,
    ) -> None:
        self.strategy_config = strategy_config
        self.is_window_days = is_window_days
        self.oos_window_days = oos_window_days
        self.min_oos_sharpe_ratio = min_oos_sharpe_ratio
        self.max_param_change_pct = max_param_change_pct
        self.scorer = PerformanceScorer()
        self._previous_params: dict[str, dict] = {}

    def optimize(
        self,
        strategy_name: str,
        df: pd.DataFrame,
        param_grid: dict[str, list[Any]],
        backtest_fn: Callable,
    ) -> dict[str, Any]:
        if len(df) < self.is_window_days + self.oos_window_days:
            logger.warning("Insufficient data for walk-forward optimization")
            return self.strategy_config.get("parameters", {}).get(strategy_name, {}).get("default", {})

        split_idx = len(df) - self.oos_window_days
        is_df = df.iloc[:split_idx]
        oos_df = df.iloc[split_idx:]

        best_params: dict[str, Any] = {}
        best_is_score = -float("inf")

        keys = list(param_grid.keys())
        values = list(param_grid.values())

        for combo in itertools.product(*values):
            params = dict(zip(keys, combo))
            is_score = backtest_fn(strategy_name, is_df, params)

            if is_score > best_is_score:
                best_is_score = is_score
                best_params = params

        oos_score = backtest_fn(strategy_name, oos_df, best_params)

        if best_is_score > 0 and oos_score < best_is_score * self.min_oos_sharpe_ratio:
            logger.warning(
                "OOS score (%.3f) below threshold for %s, keeping previous params",
                oos_score,
                strategy_name,
            )
            return self._previous_params.get(strategy_name, best_params)

        prev = self._previous_params.get(strategy_name, {})
        if prev and not self._params_stable(prev, best_params):
            logger.warning("Parameter change too large for %s", strategy_name)
            return prev

        self._previous_params[strategy_name] = best_params
        logger.info("Optimized %s: %s (IS=%.3f, OOS=%.3f)", strategy_name, best_params, best_is_score, oos_score)
        return best_params

    def _params_stable(self, old: dict, new: dict) -> bool:
        for key in new:
            if key in old and old[key] != 0:
                change = abs(new[key] - old[key]) / abs(old[key])
                if change > self.max_param_change_pct:
                    return False
        return True

    def should_reoptimize(self, last_run: Optional[datetime]) -> bool:
        if last_run is None:
            return True
        return datetime.utcnow() - last_run > timedelta(days=self.oos_window_days)
