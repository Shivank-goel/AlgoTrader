"""Tests for the swing-rejection price-action strategy.

The behaviour worth pinning is the sizing clamp. At ₹10,000 a stop tighter than
0.57% of price is silently under-risked by `cap_to_available_balance`, and one
wider than 23.6% makes `PositionSizer.calculate` return 0 so the trade
disappears with no warning. Structural stops naturally land outside that window,
so the clamp is load-bearing rather than defensive.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.core.models import Direction, MarketState, Regime
from src.data.indicators import PRICE_ACTION_COLUMNS, IndicatorEngine
from src.strategies.price_action.swing_reversal import (
    MAX_STOP_PCT,
    MIN_STOP_PCT,
    SwingRejectionStrategy,
)


def _state(price: float = 100.0, **indicators) -> MarketState:
    base = {c: 0.0 for c in PRICE_ACTION_COLUMNS}
    base.update({"pa_swing_low": 99.0, "pa_swing_high": 101.0, "atr_14": 1.0})
    base.update(indicators)
    return MarketState(
        symbol="ADAUSD",
        timestamp=pd.Timestamp("2026-01-01"),
        price=price,
        regime=Regime.RANGING,
        regime_confidence=0.7,
        indicators=base,
    )


@pytest.fixture
def strategy():
    return SwingRejectionStrategy({"proximity_pct": 1.0, "reward_risk": 2.0})


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------


def test_bullish_rejection_at_a_swing_low_goes_long(strategy):
    signal = strategy.analyze(
        _state(price=100.0, pa_bull_pin=1.0, pa_swing_low=99.5, pa_dist_to_swing_low_pct=0.5)
    )
    assert signal is not None
    assert signal.direction == Direction.LONG
    assert signal.stop_loss < 99.5, "stop must sit beyond the swing low"
    assert signal.take_profit > 100.0


def test_bearish_rejection_at_a_swing_high_goes_short(strategy):
    signal = strategy.analyze(
        _state(price=100.0, pa_bear_pin=1.0, pa_swing_high=100.5, pa_dist_to_swing_high_pct=0.5)
    )
    assert signal is not None
    assert signal.direction == Direction.SHORT
    assert signal.stop_loss > 100.5
    assert signal.take_profit < 100.0


def test_engulfing_also_triggers(strategy):
    assert strategy.analyze(
        _state(pa_bull_engulfing=1.0, pa_swing_low=99.5, pa_dist_to_swing_low_pct=0.5)
    ) is not None


def test_no_signal_without_a_rejection_candle(strategy):
    assert strategy.analyze(_state(pa_dist_to_swing_low_pct=0.2)) is None


def test_no_signal_when_price_is_far_from_the_level(strategy):
    assert strategy.analyze(
        _state(pa_bull_pin=1.0, pa_dist_to_swing_low_pct=8.0)
    ) is None


def test_missing_feature_columns_produce_no_signal():
    """Absent columns must mean 'no signal', never 'feature is zero'."""
    strategy = SwingRejectionStrategy()
    bare = MarketState(
        symbol="ADAUSD",
        timestamp=pd.Timestamp("2026-01-01"),
        price=100.0,
        regime=Regime.RANGING,
        indicators={"atr_14": 1.0},
    )
    assert strategy.analyze(bare) is None


# ---------------------------------------------------------------------------
# The sizing clamp
# ---------------------------------------------------------------------------


def test_a_too_tight_stop_is_widened_to_the_sizable_floor(strategy):
    """Below 0.57% the balance cap silently cuts risk; widen instead."""
    signal = strategy.analyze(
        _state(price=100.0, pa_bull_pin=1.0, pa_swing_low=99.99,
               pa_dist_to_swing_low_pct=0.01)
    )
    assert signal is not None
    stop_pct = (100.0 - signal.stop_loss) / 100.0 * 100
    assert stop_pct == pytest.approx(MIN_STOP_PCT, abs=0.01)


def test_a_too_wide_stop_is_skipped_rather_than_silently_dropped(strategy):
    """Above 23.6% the sizer returns 0; refuse the signal explicitly."""
    signal = strategy.analyze(
        _state(price=100.0, pa_bull_pin=1.0, pa_swing_low=50.0,
               pa_dist_to_swing_low_pct=0.5)
    )
    assert signal is None


def test_every_emitted_stop_is_inside_the_sizable_window(strategy):
    for swing_low, dist in ((99.99, 0.01), (99.5, 0.5), (99.0, 1.0), (95.0, 0.9)):
        signal = strategy.analyze(
            _state(price=100.0, pa_bull_pin=1.0, pa_swing_low=swing_low,
                   pa_dist_to_swing_low_pct=dist)
        )
        if signal is None:
            continue
        stop_pct = abs(100.0 - signal.stop_loss) / 100.0 * 100
        assert MIN_STOP_PCT - 1e-6 <= stop_pct <= MAX_STOP_PCT + 1e-6


# ---------------------------------------------------------------------------
# Reward:risk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rr", [1.0, 2.0, 3.0])
def test_target_respects_the_configured_reward_risk(rr):
    strategy = SwingRejectionStrategy({"proximity_pct": 1.0, "reward_risk": rr})
    signal = strategy.analyze(
        _state(price=100.0, pa_bull_pin=1.0, pa_swing_low=99.0,
               pa_dist_to_swing_low_pct=1.0)
    )
    assert signal is not None
    risk = signal.entry_price - signal.stop_loss
    reward = signal.take_profit - signal.entry_price
    assert reward / risk == pytest.approx(rr, rel=1e-6)


def test_signal_metadata_records_the_structural_intent(strategy):
    signal = strategy.analyze(
        _state(pa_bull_pin=1.0, pa_swing_low=99.0, pa_dist_to_swing_low_pct=1.0)
    )
    assert signal.metadata["structural"] is True
    assert signal.metadata["stop_pct"] > 0


# ---------------------------------------------------------------------------
# Integration with real data
# ---------------------------------------------------------------------------


def test_runs_against_real_computed_features(synthetic_ohlcv):
    """A smoke test that the column contract actually lines up end to end."""
    df = IndicatorEngine().compute_all(synthetic_ohlcv(500, seed=3).copy())
    from src.market.regime import RegimeDetector
    from src.market.state import MarketStateBuilder

    builder = MarketStateBuilder(IndicatorEngine(), RegimeDetector())
    prepared = builder.prepare("ADAUSD", df)
    strategy = SwingRejectionStrategy()

    # Must not raise on any bar, and must produce at least some signals.
    signals = [strategy.analyze(builder.build_at(prepared, i)) for i in range(250, len(df))]
    assert any(s is not None for s in signals), "strategy never fires on real features"
