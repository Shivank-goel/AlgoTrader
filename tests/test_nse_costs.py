"""Tests for NSE cash-equity intraday costs.

Two properties the crypto model does not have, both load-bearing:

  1. Cost in bps is NOT scale-invariant — Dhan's ₹20 per-order cap means the
     effective rate falls above ₹66,667 of notional. A backtester blind to this
     misprices every large order.
  2. Legs are asymmetric — STT is sell-side only, stamp duty buy-side only. A
     round trip is not two identical halves.
"""

from __future__ import annotations

import pytest

from src.backtest.nse_costs import NSEEquityCostModel
from src.core.models import Direction, OrderSide

# Dhan intraday equity, published 2026. A ₹10,000 round trip:
#   brokerage 2 x min(₹20, 0.03%)  = 6.00
#   STT       0.025% sell only     = 2.50
#   exchange  0.00297% x 2         = 0.594
#   SEBI      0.0001% x 2          = 0.02
#   stamp     0.003% buy only      = 0.30
#   GST       18% of (brok+exch+sebi) = 1.1905
#                                  -------
#                                    10.6045
EXPECTED_10K_ROUND_TRIP_INR = 10.6045


@pytest.fixture
def model() -> NSEEquityCostModel:
    return NSEEquityCostModel()


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------


def test_round_trip_matches_published_rates(model):
    """Checked against Dhan's own brokerage calculator."""
    assert model.round_trip_fee_inr(10_000) == pytest.approx(
        EXPECTED_10K_ROUND_TRIP_INR, abs=0.001
    )
    assert model.round_trip_fee_bps_at(10_000) == pytest.approx(10.6045, abs=0.001)


def test_breakdown_components_sum_to_the_total(model):
    b = model.breakdown(10_000)
    parts = b["brokerage"] + b["stt"] + b["exchange_txn"] + b["sebi"] + b["stamp_duty"] + b["gst"]
    assert parts == pytest.approx(b["total_inr"], abs=0.001)


@pytest.mark.parametrize(
    ("component", "expected"),
    [("brokerage", 6.0), ("stt", 2.5), ("stamp_duty", 0.3), ("gst", 1.1905)],
)
def test_individual_components_are_right(model, component, expected):
    assert model.breakdown(10_000)[component] == pytest.approx(expected, abs=0.001)


# ---------------------------------------------------------------------------
# The brokerage cap — cost is not scale-invariant
# ---------------------------------------------------------------------------


def test_brokerage_is_capped_per_order(model):
    # 0.03% of ₹66,667 is ₹20, the cap.
    assert model.brokerage_inr(10_000) == pytest.approx(3.0)
    assert model.brokerage_inr(66_667) == pytest.approx(20.0, abs=0.01)
    assert model.brokerage_inr(500_000) == pytest.approx(20.0)
    assert model.brokerage_inr(10_000_000) == pytest.approx(20.0)


def test_effective_rate_falls_above_the_cap(model):
    """The property the crypto model cannot express."""
    small = model.round_trip_fee_bps_at(10_000)
    large = model.round_trip_fee_bps_at(500_000)

    assert small == pytest.approx(10.6045, abs=0.01)
    assert large < small / 2, "brokerage cap should more than halve the effective rate"


def test_rate_is_flat_below_the_cap(model):
    """Below ₹66,667, position count is free — only turnover costs."""
    rates = [model.round_trip_fee_bps_at(n) for n in (1_000, 5_000, 10_000, 40_000)]
    assert max(rates) - min(rates) < 0.001


def test_twenty_small_orders_cost_the_same_as_one_large_one_below_the_cap(model):
    """Directly justifies holding 20 names on a ₹10,000 account."""
    twenty = sum(model.round_trip_fee_inr(2_000) for _ in range(20))
    one = model.round_trip_fee_inr(40_000)
    assert twenty == pytest.approx(one, abs=0.01)


def test_above_the_cap_concentration_becomes_cheaper(model):
    """And the inverse, once the cap binds."""
    spread = sum(model.round_trip_fee_inr(50_000) for _ in range(10))
    concentrated = model.round_trip_fee_inr(500_000)
    assert concentrated < spread


# ---------------------------------------------------------------------------
# Asymmetry
# ---------------------------------------------------------------------------


def test_sell_leg_costs_more_than_buy_leg(model):
    """STT is sell-side only; stamp duty is buy-side only. STT is larger."""
    buy = model.leg_fee_inr(10_000, OrderSide.BUY)
    sell = model.leg_fee_inr(10_000, OrderSide.SELL)

    assert sell > buy
    assert sell - buy == pytest.approx(2.5 - 0.3, abs=0.001)


def test_legs_sum_to_the_round_trip(model):
    total = model.leg_fee_inr(10_000, OrderSide.BUY) + model.leg_fee_inr(10_000, OrderSide.SELL)
    assert total == pytest.approx(model.round_trip_fee_inr(10_000))


def test_stt_appears_only_on_sells(model):
    buy = model.breakdown(10_000)
    assert buy["stt"] > 0  # breakdown covers a full round trip
    # But an individual buy leg carries none.
    no_stt = model.leg_fee_inr(10_000, OrderSide.BUY)
    with_stt = model.leg_fee_inr(10_000, OrderSide.SELL)
    assert with_stt - no_stt > 0


def test_long_and_short_round_trips_cost_the_same(model):
    """Both directions pay one buy and one sell; only the order differs."""
    kw = dict(entry_price=100.0, exit_price=100.0, size=100)
    long_pct = model.round_trip_cost_pct(direction=Direction.LONG, **kw)
    short_pct = model.round_trip_cost_pct(direction=Direction.SHORT, **kw)
    assert long_pct == pytest.approx(short_pct)


# ---------------------------------------------------------------------------
# Backtester interface compatibility
# ---------------------------------------------------------------------------


def test_is_usable_as_a_cost_model(model):
    """Must drop into QuickBacktester and PortfolioBacktester unchanged."""
    from src.backtest.costs import CostModel

    assert isinstance(model, CostModel)
    assert model.entry_exit_fee_bps() > 0
    assert model.fee_usd(10_000) > 0


def test_portfolio_backtester_rate_matches_the_round_trip(model):
    """PortfolioBacktester multiplies this by turnover, so it must be the real rate."""
    assert model.entry_exit_fee_bps() == pytest.approx(
        model.round_trip_fee_bps_at(model.reference_notional_inr), abs=1e-9
    )


def test_no_funding_on_intraday_equity(model):
    """MIS is squared off the same session; there is nothing to fund."""
    from datetime import datetime

    assert model.funding_bps_per_period == 0.0
    assert model.funding_usd(
        100_000, Direction.LONG, datetime(2026, 1, 1), datetime(2026, 1, 5)
    ) == 0.0


def test_round_trip_cost_pct_ignores_funding_arguments(model):
    from datetime import datetime

    kw = dict(entry_price=100.0, exit_price=100.0, direction=Direction.LONG, size=100)
    without = model.round_trip_cost_pct(**kw)
    with_ts = model.round_trip_cost_pct(
        **kw, entry_ts=datetime(2026, 1, 1), exit_ts=datetime(2026, 3, 1)
    )
    assert without == pytest.approx(with_ts)


# ---------------------------------------------------------------------------
# Degenerate input
# ---------------------------------------------------------------------------


def test_zero_and_negative_notional_are_safe(model):
    assert model.round_trip_fee_bps_at(0) == 0.0
    assert model.leg_fee_inr(-10_000, OrderSide.BUY) > 0  # magnitude is used
    assert model.round_trip_cost_pct(
        entry_price=0.0, exit_price=0.0, direction=Direction.LONG, size=0
    ) == 0.0


def test_delivery_brokerage_is_free(model):
    """Dhan charges zero brokerage on equity delivery; only statutory costs apply."""
    intraday = model.round_trip_fee_inr(10_000)
    delivery = model.round_trip_fee_inr(10_000, delivery=True)
    assert delivery < intraday
    assert model.brokerage_inr(10_000, delivery=True) == 0.0


# ---------------------------------------------------------------------------
# Spread
# ---------------------------------------------------------------------------


def test_spread_defaults_to_zero_so_fee_arithmetic_stays_auditable(model):
    assert model.half_spread_bps == 0.0
    assert model.round_trip_fee_inr(10_000) == pytest.approx(EXPECTED_10K_ROUND_TRIP_INR, abs=0.001)


def test_spread_adds_to_every_leg():
    with_spread = NSEEquityCostModel(half_spread_bps=5.0)
    without = NSEEquityCostModel()
    # 5 bps per leg, two legs, on ₹10,000 = ₹10 extra.
    delta = with_spread.round_trip_fee_inr(10_000) - without.round_trip_fee_inr(10_000)
    assert delta == pytest.approx(10.0, abs=0.01)


def test_spread_is_not_taxed(model):
    """GST applies to brokerage and exchange fees, not to crossing the spread."""
    a = NSEEquityCostModel(half_spread_bps=0.0).breakdown(10_000)["gst"]
    b = NSEEquityCostModel(half_spread_bps=50.0).breakdown(10_000)["gst"]
    assert a == pytest.approx(b)


def test_breakeven_half_spread_matches_the_measured_case(model):
    """The overnight-gap sleeve: 8.96 bps/day gross, 1.65 turnover, ₹1,000/order."""
    be = model.breakeven_half_spread_bps(
        gross_bps_per_period=8.96, turnover_per_period=1.65, notional_per_order=1_000
    )
    assert be == pytest.approx(0.12, abs=0.05), "should be far below any real NSE spread"


def test_breakeven_improves_with_order_size(model):
    """Above the ₹20 cap the fee rate falls, so the strategy tolerates more spread."""
    small = model.breakeven_half_spread_bps(8.96, 1.65, 1_000)
    large = model.breakeven_half_spread_bps(8.96, 1.65, 300_000)
    assert large > small
    assert large > 2.0
