"""Train/serve parity: the backtest and the live engine must see identical features.

Two code paths read indicators off a DataFrame:

  live      IndicatorEngine.get_latest_values(df)   -> dict from df.iloc[-1]
  backtest  IndicatorEngine.prepare_arrays(df)      -> dict of numpy columns
            IndicatorEngine.get_values_at(arrays, i)

`prepare_arrays` silently skips non-numeric dtypes. A boolean or string feature
column therefore works live and vanishes in backtests, with no error — the
strategy simply stops seeing it. Combined with `BaseStrategy._get_indicator`
returning 0.0 for a missing key while strategies test `if not x:`, "column
absent" and "feature is zero" become indistinguishable.

That is not hypothetical. `volume_breakout` fired zero trades across seven
symbols for a full year because it built column names like f"DCHU_{period}"
that `compute_all` never emits.

These tests are a permanent CI guard. Any new feature column must pass them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.data.indicators import IndicatorEngine
from src.market.regime import RegimeDetector
from src.market.state import MarketStateBuilder


@pytest.fixture
def computed(synthetic_ohlcv):
    return IndicatorEngine().compute_all(synthetic_ohlcv(600, seed=11).copy())


# ---------------------------------------------------------------------------
# The core guarantee
# ---------------------------------------------------------------------------


def test_live_and_backtest_see_the_same_columns(computed):
    engine = IndicatorEngine()

    live_keys = set(engine.get_latest_values(computed))
    backtest_keys = set(engine.get_values_at(engine.prepare_arrays(computed), len(computed) - 1))

    missing_in_backtest = live_keys - backtest_keys
    assert not missing_in_backtest, (
        f"columns visible live but NOT in backtest: {sorted(missing_in_backtest)}. "
        "prepare_arrays drops non-numeric dtypes — emit these as float, not bool/str."
    )

    missing_in_live = backtest_keys - live_keys
    assert not missing_in_live, f"columns in backtest but not live: {sorted(missing_in_live)}"


def test_live_and_backtest_agree_on_every_value(computed):
    engine = IndicatorEngine()
    last = len(computed) - 1

    live = engine.get_latest_values(computed)
    backtest = engine.get_values_at(engine.prepare_arrays(computed), last)

    for key, value in live.items():
        assert backtest[key] == pytest.approx(value, rel=1e-12, abs=1e-12), (
            f"{key} differs: live={value} backtest={backtest[key]}"
        )


@pytest.mark.parametrize("offset", [0, 1, 7, 50, 199])
def test_parity_holds_at_arbitrary_bars_not_just_the_last(computed, offset):
    """Backtests read every bar, so parity must hold everywhere, not just at -1."""
    engine = IndicatorEngine()
    i = len(computed) - 1 - offset

    live = engine.get_latest_values(computed.iloc[: i + 1])
    backtest = engine.get_values_at(engine.prepare_arrays(computed), i)

    assert set(live) == set(backtest)
    for key, value in live.items():
        assert backtest[key] == pytest.approx(value, rel=1e-12, abs=1e-12)


# ---------------------------------------------------------------------------
# The dtype trap, stated explicitly
# ---------------------------------------------------------------------------


def test_boolean_columns_are_silently_dropped_by_the_backtest_path(computed):
    """Documents the trap this whole module exists to catch.

    If this ever starts failing because prepare_arrays learned to handle bools,
    delete the test — but until then, feature columns must be floats.
    """
    df = computed.copy()
    df["a_bool_flag"] = df["close"] > df["open"]

    engine = IndicatorEngine()
    assert "a_bool_flag" in engine.get_latest_values(df)
    assert "a_bool_flag" not in engine.prepare_arrays(df), (
        "prepare_arrays now keeps bools; the float-only rule for feature "
        "columns can be relaxed and this test removed"
    )


def test_the_same_flag_as_float_survives_both_paths(computed):
    """The correct way to emit a pattern flag."""
    df = computed.copy()
    df["a_float_flag"] = (df["close"] > df["open"]).astype(float)

    engine = IndicatorEngine()
    live = engine.get_latest_values(df)
    backtest = engine.get_values_at(engine.prepare_arrays(df), len(df) - 1)

    assert "a_float_flag" in live
    assert "a_float_flag" in backtest
    assert live["a_float_flag"] == backtest["a_float_flag"]


def test_every_computed_column_is_numeric(computed):
    """compute_all must never emit a dtype the backtest path would drop."""
    non_numeric = [
        col for col in computed.columns
        if not np.issubdtype(computed[col].to_numpy().dtype, np.number)
    ]
    assert not non_numeric, f"non-numeric columns from compute_all: {non_numeric}"


# ---------------------------------------------------------------------------
# MarketState parity
# ---------------------------------------------------------------------------


def test_market_state_indicators_match_across_paths(computed):
    """The level strategies actually consume."""
    engine = IndicatorEngine()
    i = len(computed) - 1

    live_state = MarketStateBuilder(engine, RegimeDetector()).build("BTCUSD", computed)

    fast = MarketStateBuilder(engine, RegimeDetector())
    fast_state = fast.build_at(fast.prepare("BTCUSD", computed), i)

    assert set(live_state.indicators) == set(fast_state.indicators)
    for key, value in live_state.indicators.items():
        assert fast_state.indicators[key] == pytest.approx(value, rel=1e-12, abs=1e-12)
    assert live_state.price == pytest.approx(fast_state.price)


def test_raw_ohlcv_is_reachable_as_indicators(computed):
    """Price-action strategies read bar anatomy through state.indicators."""
    engine = IndicatorEngine()
    values = engine.get_values_at(engine.prepare_arrays(computed), len(computed) - 1)

    for col in ("open", "high", "low", "close", "volume"):
        assert col in values, f"{col} unreachable from a strategy's analyze()"
        assert values[col] == pytest.approx(float(computed[col].iloc[-1]))


# ---------------------------------------------------------------------------
# Known divergences — asserted so they stay known
# ---------------------------------------------------------------------------


def test_multi_timeframe_is_a_known_backtest_gap(computed):
    """build() populates multi_timeframe; build_at() cannot.

    Any strategy reading state.multi_timeframe is scored in the backtest with
    it empty and then runs live with it populated. UniverseManager assignment
    decisions come from those backtests, so this is real train/serve skew.
    Asserted here so it is a documented constraint rather than a surprise.
    """
    engine = IndicatorEngine()
    fast = MarketStateBuilder(engine, RegimeDetector())
    state = fast.build_at(fast.prepare("BTCUSD", computed), len(computed) - 1)

    assert state.multi_timeframe == {}
    assert state.candles == []
