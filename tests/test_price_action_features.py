"""Tests for price-action feature columns.

The load-bearing test here is `test_swing_columns_contain_no_lookahead`. A
fractal swing high at bar `i` is only knowable at `i + k`, once k bars have
failed to exceed it. If the pivot is written at bar `i`, a backtest can trade a
level the live engine has not yet seen, and the strategy shows an edge that
cannot exist in production. That is the single most common way a price-action
backtest lies.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.data.indicators import (
    PRICE_ACTION_COLUMNS,
    SWING_CONFIRMATION_BARS,
    IndicatorEngine,
)


@pytest.fixture
def computed(synthetic_ohlcv):
    return IndicatorEngine().compute_all(synthetic_ohlcv(600, seed=5).copy())


# ---------------------------------------------------------------------------
# Lookahead — the one that matters
# ---------------------------------------------------------------------------


def test_swing_columns_contain_no_lookahead(synthetic_ohlcv):
    """Value at bar i must be reproducible from bars <= i alone.

    Recomputing on a truncated frame is the only honest check: if the full-frame
    value at bar i differs from the truncated-frame value at bar i, the column
    is reading the future.
    """
    engine = IndicatorEngine()
    raw = synthetic_ohlcv(400, seed=9)
    full = engine.compute_all(raw.copy())

    for i in (250, 300, 355, 399):
        truncated = engine.compute_all(raw.iloc[: i + 1].copy())
        for col in ("pa_swing_high", "pa_swing_low", "pa_bos_up", "pa_bos_down"):
            assert full[col].iloc[i] == pytest.approx(truncated[col].iloc[i]), (
                f"{col} at bar {i} changed when future bars were removed "
                f"({truncated[col].iloc[i]} -> {full[col].iloc[i]}): it reads ahead"
            )


def test_a_pivot_is_not_visible_before_it_is_confirmed():
    """Construct an unmistakable spike and check when it becomes usable."""
    n = 120
    idx = pd.date_range("2026-01-01", periods=n, freq="1h")
    high = np.full(n, 101.0)
    low = np.full(n, 99.0)
    close = np.full(n, 100.0)

    spike = 60
    high[spike] = 130.0  # a lone, obvious swing high

    df = pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close,
         "volume": np.full(n, 1000.0)},
        index=idx,
    )
    out = IndicatorEngine().compute_all(df)

    k = SWING_CONFIRMATION_BARS
    assert out["pa_swing_high"].iloc[spike] != 130.0, "pivot visible on its own bar"
    assert out["pa_swing_high"].iloc[spike + k - 1] != 130.0, "visible before confirmation"
    assert out["pa_swing_high"].iloc[spike + k] == pytest.approx(130.0), (
        "pivot should become visible exactly k bars after it forms"
    )


# ---------------------------------------------------------------------------
# Parity contract
# ---------------------------------------------------------------------------


def test_all_declared_columns_are_emitted(computed):
    missing = [c for c in PRICE_ACTION_COLUMNS if c not in computed.columns]
    assert not missing, f"declared but not emitted: {missing}"


def test_all_price_action_columns_are_float(computed):
    """Bools are silently dropped by prepare_arrays; floats survive both paths."""
    for col in PRICE_ACTION_COLUMNS:
        assert np.issubdtype(computed[col].to_numpy().dtype, np.floating), (
            f"{col} is {computed[col].dtype}, not float — it will vanish in backtests"
        )


def test_price_action_columns_reach_a_strategy(computed):
    engine = IndicatorEngine()
    values = engine.get_values_at(engine.prepare_arrays(computed), len(computed) - 1)
    for col in PRICE_ACTION_COLUMNS:
        assert col in values, f"{col} unreachable from analyze()"


def test_no_nan_or_inf_in_any_price_action_column(computed):
    for col in PRICE_ACTION_COLUMNS:
        arr = computed[col].to_numpy()
        assert np.isfinite(arr).all(), f"{col} contains NaN or inf"


# ---------------------------------------------------------------------------
# Semantics
# ---------------------------------------------------------------------------


def _one_bar(o, h, l, c, prev=None):
    rows = []
    if prev:
        rows.append(prev)
    rows.append((o, h, l, c))
    # Pad so compute_all's length guard passes.
    pad = [(100, 101, 99, 100)] * 40
    data = pad + rows
    idx = pd.date_range("2026-01-01", periods=len(data), freq="1h")
    df = pd.DataFrame(data, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1000.0
    return IndicatorEngine().compute_all(df)


def test_bull_pin_bar_is_detected():
    # Long lower wick, small body near the top.
    out = _one_bar(o=109, h=110, l=100, c=109.5)
    assert out["pa_bull_pin"].iloc[-1] == 1.0
    assert out["pa_bear_pin"].iloc[-1] == 0.0


def test_bear_pin_bar_is_detected():
    out = _one_bar(o=101, h=110, l=100, c=100.5)
    assert out["pa_bear_pin"].iloc[-1] == 1.0
    assert out["pa_bull_pin"].iloc[-1] == 0.0


def test_bullish_engulfing_is_detected():
    out = _one_bar(o=99, h=106, l=98, c=105, prev=(104, 105, 99, 100))
    assert out["pa_bull_engulfing"].iloc[-1] == 1.0


def test_inside_and_outside_bars_are_mutually_exclusive(computed):
    both = (computed["pa_inside_bar"] > 0) & (computed["pa_outside_bar"] > 0)
    assert not both.any()


def test_body_and_wick_fractions_are_bounded(computed):
    for col in ("pa_body_frac", "pa_upper_wick_frac", "pa_lower_wick_frac"):
        arr = computed[col].to_numpy()
        assert (arr >= -1e-9).all() and (arr <= 1.0 + 1e-9).all(), f"{col} out of [0,1]"


def test_bar_components_sum_to_the_whole_range(computed):
    total = (
        computed["pa_body_frac"]
        + computed["pa_upper_wick_frac"]
        + computed["pa_lower_wick_frac"]
    )
    # Zero-range bars are floored to 0 rather than 1; ignore those.
    nonzero = computed["pa_range_pct"] > 0
    assert total[nonzero].between(0.999, 1.001).all()


def test_break_of_structure_requires_exceeding_the_swing(computed):
    up = computed["pa_bos_up"] > 0
    assert (computed.loc[up, "close"] > computed.loc[up, "pa_swing_high"]).all()

    down = computed["pa_bos_down"] > 0
    assert (computed.loc[down, "close"] < computed.loc[down, "pa_swing_low"]).all()


def test_swing_levels_are_drawn_from_actual_past_bars(computed):
    """Each level must be a real prior high/low, not an interpolation.

    Note there is deliberately no swing_high >= swing_low invariant: the two are
    independent forward-fills confirmed at different bars, so in a sustained
    trend the most recent confirmed low can sit above an older confirmed high.
    """
    highs = set(np.round(computed["high"].to_numpy(), 9))
    lows = set(np.round(computed["low"].to_numpy(), 9))

    tail = slice(100, None)  # past warm-up
    assert set(np.round(computed["pa_swing_high"].to_numpy()[tail], 9)) <= highs
    assert set(np.round(computed["pa_swing_low"].to_numpy()[tail], 9)) <= lows


def test_swing_levels_only_change_at_confirmation(computed):
    """Levels are step functions: they hold until a new pivot confirms."""
    changes = computed["pa_swing_high"].diff().ne(0).sum()
    assert 0 < changes < len(computed) / 2, (
        f"swing_high changed on {changes} of {len(computed)} bars; it should be "
        "a step function, not a rolling maximum"
    )
