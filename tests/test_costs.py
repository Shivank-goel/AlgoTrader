"""Tests for the trading cost model.

The invariant that matters most: costs are always adverse. A bug that made
slippage favourable in some branch would silently inflate every backtest, and
that is exactly the class of error this whole phase exists to eliminate.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import yaml

from src.backtest.costs import BPS, CostModel
from src.core.models import Direction, OrderSide

CONFIG_RISK = "config/risk.yaml"


@pytest.fixture
def model() -> CostModel:
    return CostModel()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_defaults_include_gst():
    """0.05% taker on Delta India is 0.059% once 18% GST is applied."""
    model = CostModel()
    assert model.taker_fee_bps == pytest.approx(5.9, abs=0.01)
    assert model.maker_fee_bps == pytest.approx(2.36, abs=0.01)


def test_loads_from_the_shipped_risk_config():
    with open(CONFIG_RISK) as fh:
        config = yaml.safe_load(fh)

    model = CostModel.from_config(config)
    assert model.taker_fee_bps == pytest.approx(5.9)
    assert model.funding_hours_utc == (0, 8, 16)


def test_from_config_tolerates_a_missing_costs_block():
    model = CostModel.from_config({})
    assert model.taker_fee_bps == pytest.approx(5.9, abs=0.01)


def test_zero_model_is_frictionless():
    model = CostModel.zero()
    assert model.is_zero
    assert model.fee_usd(10_000) == 0.0
    assert model.entry_fill_price(100.0, Direction.LONG) == 100.0


# ---------------------------------------------------------------------------
# Slippage direction — the load-bearing invariant
# ---------------------------------------------------------------------------


def test_entry_slippage_is_always_adverse(model):
    assert model.entry_fill_price(100.0, Direction.LONG) > 100.0
    assert model.entry_fill_price(100.0, Direction.SHORT) < 100.0


def test_exit_slippage_is_always_adverse(model):
    # Closing a long is a sell, so the adverse direction flips.
    assert model.exit_fill_price(100.0, Direction.LONG) < 100.0
    assert model.exit_fill_price(100.0, Direction.SHORT) > 100.0


def test_fill_price_for_side_matches_direction_semantics(model):
    assert model.fill_price_for_side(100.0, OrderSide.BUY) > 100.0
    assert model.fill_price_for_side(100.0, OrderSide.SELL) < 100.0


def test_a_round_trip_always_loses_to_slippage_alone(model):
    """Enter then immediately exit at an unchanged mid: must lose money."""
    mid = 100.0
    for direction in (Direction.LONG, Direction.SHORT):
        entry = model.entry_fill_price(mid, direction)
        exit_ = model.exit_fill_price(mid, direction)
        gross = (exit_ - entry) if direction == Direction.LONG else (entry - exit_)
        assert gross < 0, f"{direction} round trip at a flat price was profitable"


# ---------------------------------------------------------------------------
# Slippage magnitude
# ---------------------------------------------------------------------------


def test_slippage_has_a_floor_and_a_ceiling(model):
    tiny = model.slippage_bps(price=100.0, notional=1.0, bar_volume_usd=1e12)
    assert tiny == pytest.approx(model.base_slippage_bps)

    enormous = model.slippage_bps(price=100.0, notional=1e9, bar_volume_usd=1.0)
    assert enormous == model.max_slippage_bps


def test_slippage_grows_with_order_size_relative_to_bar_volume(model):
    small = model.slippage_bps(price=100.0, notional=1_000, bar_volume_usd=10_000_000)
    large = model.slippage_bps(price=100.0, notional=500_000, bar_volume_usd=10_000_000)
    assert large > small


def test_slippage_grows_with_volatility(model):
    calm = model.slippage_bps(price=100.0, atr=0.1)
    wild = model.slippage_bps(price=100.0, atr=5.0)
    assert wild > calm


def test_slippage_handles_thin_and_degenerate_inputs(model):
    """Illiquid symbols must not produce NaN or inf."""
    for kwargs in (
        {"price": 100.0, "bar_volume_usd": 0.0, "notional": 1000.0},
        {"price": 0.0, "atr": 0.0},
        {"price": 0.01, "atr": 0.005, "bar_volume_usd": 12.0, "notional": 500.0},
    ):
        value = model.slippage_bps(**kwargs)
        assert value == value  # not NaN
        assert 0 <= value <= model.max_slippage_bps


def test_fill_price_never_goes_negative(model):
    assert model.entry_fill_price(0.0001, Direction.SHORT) >= 0.0


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------


def test_fee_is_proportional_and_never_negative(model):
    assert model.fee_usd(10_000) == pytest.approx(10_000 * 5.9 * BPS)
    assert model.fee_usd(-10_000) > 0


def test_maker_is_cheaper_than_taker(model):
    assert model.fee_usd(10_000, maker=True) < model.fee_usd(10_000)


def test_round_trip_taker_fee_is_about_twelve_bps(model):
    assert model.round_trip_fee_bps() == pytest.approx(11.8, abs=0.05)


# ---------------------------------------------------------------------------
# Funding
# ---------------------------------------------------------------------------


def test_funding_counts_boundary_crossings_only(model):
    day = datetime(2026, 8, 10)

    # Wholly between the 08:00 and 16:00 boundaries.
    assert model.funding_periods(day.replace(hour=9), day.replace(hour=15)) == 0
    # Straddles 08:00 by two minutes.
    assert model.funding_periods(day.replace(hour=7, minute=59), day.replace(hour=8, minute=1)) == 1
    # A full day crosses 00:00, 08:00 and 16:00.
    assert model.funding_periods(day, day + timedelta(days=1)) == 3


def test_funding_boundary_is_half_open(model):
    """Exactly-at-entry does not pay; exactly-at-exit does."""
    day = datetime(2026, 8, 10)
    assert model.funding_periods(day.replace(hour=8), day.replace(hour=15)) == 0
    assert model.funding_periods(day.replace(hour=1), day.replace(hour=8)) == 1


def test_funding_over_multiple_days(model):
    start = datetime(2026, 8, 10, 0, 30)
    assert model.funding_periods(start, start + timedelta(days=7)) == 21


def test_longs_pay_and_shorts_receive_when_funding_is_positive(model):
    entry = datetime(2026, 8, 10, 7, 0)
    exit_ = datetime(2026, 8, 10, 9, 0)

    long_cost = model.funding_usd(10_000, Direction.LONG, entry, exit_)
    short_cost = model.funding_usd(10_000, Direction.SHORT, entry, exit_)

    assert long_cost > 0
    assert short_cost == pytest.approx(-long_cost)


def test_funding_rate_is_capped(model):
    entry = datetime(2026, 8, 10, 7, 0)
    exit_ = datetime(2026, 8, 10, 9, 0)

    absurd = model.funding_usd(10_000, Direction.LONG, entry, exit_, rate_bps=1e9)
    capped = model.funding_usd(
        10_000, Direction.LONG, entry, exit_, rate_bps=model.max_funding_bps_per_period
    )
    assert absurd == pytest.approx(capped)


def test_no_funding_for_a_zero_length_hold(model):
    now = datetime(2026, 8, 10, 12, 0)
    assert model.funding_usd(10_000, Direction.LONG, now, now) == 0.0


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def test_round_trip_cost_is_positive_and_material(model):
    cost_pct = model.round_trip_cost_pct(
        entry_price=100.0, exit_price=100.0, direction=Direction.LONG, size=10.0
    )
    # Two taker legs at 5.9 bps each.
    assert cost_pct == pytest.approx(0.118, abs=0.005)


def test_round_trip_cost_includes_funding_when_timestamps_are_given(model):
    kwargs = dict(
        entry_price=100.0, exit_price=100.0, direction=Direction.LONG, size=10.0
    )
    without = model.round_trip_cost_pct(**kwargs)
    with_funding = model.round_trip_cost_pct(
        **kwargs,
        entry_ts=datetime(2026, 8, 10, 7, 0),
        exit_ts=datetime(2026, 8, 12, 7, 0),
    )
    assert with_funding > without


def test_round_trip_cost_is_safe_on_degenerate_input(model):
    assert model.round_trip_cost_pct(
        entry_price=0.0, exit_price=0.0, direction=Direction.LONG, size=0.0
    ) == 0.0


def test_fingerprint_changes_with_parameters_and_is_stable():
    a = CostModel()
    b = CostModel()
    c = CostModel(taker_fee_bps=99.0)

    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()
