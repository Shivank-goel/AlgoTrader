"""Selector convergence: real edge vs noise, hysteresis, significance floor."""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.backtest.quick import BacktestResult, QuickBacktester
from src.market.universe import UniverseManager


def _make_result(
    symbol: str,
    strategy: str,
    pnls: list[float],
    *,
    max_dd: float = 0.05,
    min_trades: int = 10,
) -> BacktestResult:
    result = BacktestResult(symbol=symbol, strategy=strategy)
    result.trade_pnls = list(pnls)
    result.trades = len(pnls)
    result.wins = sum(1 for p in pnls if p > 0)
    result.losses = result.trades - result.wins
    result.win_rate = result.wins / result.trades if result.trades else 0.0

    arr = np.array(pnls, dtype=float)
    result.avg_return_pct = float(arr.mean()) if len(arr) else 0.0
    result.expectancy_pct = result.avg_return_pct
    result.max_drawdown_pct = max_dd * 100.0
    std = float(arr.std()) if len(arr) > 1 else 0.0
    result.sharpe = (result.avg_return_pct / std) if std > 0 else 0.0
    if std > 0 and len(arr) > 1:
        result.t_stat = float(arr.mean() / (std / math.sqrt(len(arr))))

    backtester = QuickBacktester(min_trades=min_trades)
    result.score = backtester._score(result)
    return result


def _real_edge_pnls(n: int = 100) -> list[float]:
    """Consistent small edge: ~55% win rate, +0.5% wins, -0.4% losses."""
    pnls: list[float] = []
    for i in range(n):
        pnls.append(0.5 if i % 5 != 0 else -0.4)
    return pnls


def _noise_pnls(n: int = 100, seed: int = 42) -> list[float]:
    rng = np.random.default_rng(seed)
    return [float(x) for x in rng.normal(0.0, 1.0, n)]


def test_real_edge_scores_above_noise_with_long_history() -> None:
    real_edge = _make_result("BTCUSD", "real_edge", _real_edge_pnls(100))
    noise = _make_result("BTCUSD", "noise", _noise_pnls(100))

    assert real_edge.score > noise.score
    assert real_edge.t_stat > 1.0


def test_noise_short_streak_does_not_beat_real_edge() -> None:
    real_edge = _make_result("BTCUSD", "real_edge", _real_edge_pnls(100))
    lucky_noise = _make_result("BTCUSD", "noise", [2.0, 2.0, 2.0, 2.0, 2.0])

    assert lucky_noise.score == -1.0
    assert real_edge.score > lucky_noise.score


def test_significance_floor_rejects_small_sample() -> None:
    small_sample = _make_result("BTCUSD", "noise", [2.0, 2.0, 2.0, 2.0, 2.0], min_trades=10)

    assert small_sample.score == -1.0


@pytest.fixture
def universe_manager() -> UniverseManager:
    return UniverseManager(
        scanner=MagicMock(),
        data_manager=MagicMock(),
        strategies={},
        min_backtest_trades=10,
        reassignment_margin=0.2,
    )


def test_hysteresis_keeps_incumbent_within_margin(universe_manager: UniverseManager) -> None:
    incumbent_pnls = _real_edge_pnls(40)
    challenger_pnls = [1.2] * 40

    incumbent = _make_result("BTCUSD", "real_edge", incumbent_pnls)
    challenger = _make_result("BTCUSD", "noise", challenger_pnls)

    universe_manager._previous_assignments = {"BTCUSD": "real_edge"}
    universe_manager._previous_scores = {"BTCUSD": incumbent.score}

    assignment = universe_manager._resolve_symbol_assignment(
        "BTCUSD",
        [incumbent, challenger],
        None,
    )

    assert assignment is not None
    assert assignment.strategy == "real_edge"


def test_hysteresis_reassigns_when_margin_cleared(universe_manager: UniverseManager) -> None:
    incumbent_pnls = [0.1] * 40
    challenger_pnls = [1.5] * 40

    incumbent = _make_result("BTCUSD", "real_edge", incumbent_pnls)
    challenger = _make_result("BTCUSD", "noise", challenger_pnls)

    universe_manager._previous_assignments = {"BTCUSD": "real_edge"}
    universe_manager._previous_scores = {"BTCUSD": incumbent.score}

    assert challenger.score > incumbent.score * 1.2

    assignment = universe_manager._resolve_symbol_assignment(
        "BTCUSD",
        [incumbent, challenger],
        None,
    )

    assert assignment is not None
    assert assignment.strategy == "noise"


def test_keeps_incumbent_when_significance_floor_fails(universe_manager: UniverseManager) -> None:
    incumbent = _make_result("BTCUSD", "real_edge", _real_edge_pnls(40))
    ineligible_challenger = _make_result(
        "BTCUSD",
        "noise",
        [3.0, 3.0, 3.0, 3.0, 3.0],
        min_trades=10,
    )

    universe_manager._previous_assignments = {"BTCUSD": "real_edge"}
    universe_manager._previous_scores = {"BTCUSD": incumbent.score}

    assignment = universe_manager._resolve_symbol_assignment(
        "BTCUSD",
        [incumbent, ineligible_challenger],
        None,
    )

    assert assignment is not None
    assert assignment.strategy == "real_edge"
