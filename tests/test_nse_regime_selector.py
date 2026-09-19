from pathlib import Path

import numpy as np
import pandas as pd

from src.backtest.nse_regime_backtest import run_regime_backtest
from src.fyers.costs import FyersCosts
from src.fyers.universe import ForwardUniverse
from src.strategies.nse_regime_selector import (
    FrozenFamilyEvidence,
    NseRegime,
    RegimeAwareSelector,
    SelectorConfig,
    StrategyFamily,
)


def panel(*, rows=300, columns=20, slope=.001) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=rows, freq="B")
    base = 100 * np.exp(np.arange(rows) * slope)
    return pd.DataFrame({f"NSE:S{i}-EQ": base * (1 + i / 1000) for i in range(columns)}, index=index)


def config(*qualified: StrategyFamily, tie=False) -> SelectorConfig:
    evidence = []
    for family in StrategyFamily:
        lower = .01 if family in qualified else 0
        if tie and family in qualified:
            lower = .02
        evidence.append(FrozenFamilyEvidence(family=family,
                                             lower_confidence_bound=lower,
                                             qualified=family in qualified))
    return SelectorConfig(evidence=evidence)


def test_versioned_forward_universe_has_reviewed_identity():
    universe = ForwardUniverse.load(Path("config/nse_forward_universe.yaml"))
    assert len(universe.members) == 20
    assert len({row.isin for row in universe.members}) == 20
    assert universe.universe_id.endswith("_v1")


def test_trending_regime_selects_only_qualified_frozen_family():
    selector = RegimeAwareSelector(config(StrategyFamily.MOMENTUM))
    result = selector.select(panel())
    assert result["regime"] == NseRegime.TRENDING_UP.value
    assert result["selected_family"] == StrategyFamily.MOMENTUM.value
    assert StrategyFamily.BREAKOUT.value in result["eligible_families"]
    assert not result["live_enabled"]


def test_unqualified_and_tied_families_abstain():
    assert RegimeAwareSelector(config()).select(panel())["reason"] == "no_eligible_family_is_qualified"
    tied = RegimeAwareSelector(config(StrategyFamily.MOMENTUM, StrategyFamily.BREAKOUT, tie=True))
    assert tied.select(panel())["reason"] == "selector_evidence_tie"


def test_unknown_and_falling_regimes_hold_cash():
    selector = RegimeAwareSelector(config(StrategyFamily.MOMENTUM))
    assert selector.select(panel(rows=100))["regime"] == NseRegime.UNKNOWN.value
    falling = selector.select(panel(slope=-.001))
    assert falling["regime"] == NseRegime.FALLING.value
    assert falling["selected_family"] is None


def test_future_prices_cannot_change_earlier_scores():
    closes = panel()
    before = RegimeAwareSelector.scores(StrategyFamily.MOMENTUM, closes.iloc[:-1])
    changed = closes.copy()
    changed.iloc[-1] *= np.arange(1, 21)
    after = RegimeAwareSelector.scores(StrategyFamily.MOMENTUM, changed.iloc[:-1])
    pd.testing.assert_series_equal(before, after)


def test_whole_share_targets_skip_unaffordable_and_cap_positions():
    selector = RegimeAwareSelector(config())
    scores = pd.Series({"EXPENSIVE": 3, "A": 2, "B": 1, "C": .5, "D": .4, "E": .3})
    prices = pd.Series({"EXPENSIVE": 2500, "A": 1000, "B": 500, "C": 400, "D": 200, "E": 100})
    targets, rejected = selector.whole_share_targets(scores, prices)
    assert targets == {"A": 2, "B": 4, "C": 5, "D": 10, "E": 20}
    assert rejected == [{"symbol": "EXPENSIVE", "reason": "unaffordable"}]


def test_regime_backtest_is_whole_share_costed_and_spread_stressed():
    closes = panel(rows=420)
    selector = RegimeAwareSelector(config())
    clean = run_regime_backtest(closes, selector, StrategyFamily.MOMENTUM,
                                rebalance_bars=20, half_spread_bps=0, costs=FyersCosts.load())
    stressed = run_regime_backtest(closes, selector, StrategyFamily.MOMENTUM,
                                   rebalance_bars=20, half_spread_bps=25, costs=FyersCosts.load())
    assert len(clean.net_returns) >= 5
    assert len({len(clean.timestamps), len(clean.net_returns),
                len(clean.baseline_returns), len(clean.turnover)}) == 1
    assert np.prod(np.add(1, stressed.net_returns)) <= np.prod(np.add(1, clean.net_returns))
    assert all(value >= 0 for value in clean.turnover)
