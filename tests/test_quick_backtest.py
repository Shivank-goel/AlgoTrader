"""Tests for QuickBacktester — which had none, despite driving strategy selection.

Its scores decide which strategy trades which symbol via UniverseManager, so a
silent error here propagates straight into live trading.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtest.costs import CostModel
from src.backtest.quick import QuickBacktester
from src.core.models import Direction, Regime
from src.data.indicators import IndicatorEngine
from src.market.regime import RegimeDetector
from src.market.state import MarketStateBuilder
from src.strategies.base import BaseStrategy


class _AlwaysLong(BaseStrategy):
    """Enters long on every bar it is asked about."""

    name = "always_long"

    def analyze(self, market_state):
        return self._make_signal(market_state, Direction.LONG)

    def get_preferred_regimes(self):
        return [Regime.TRENDING, Regime.RANGING, Regime.VOLATILE, Regime.QUIET]


class _NeverTrades(BaseStrategy):
    name = "never"

    def analyze(self, market_state):
        return None

    def get_preferred_regimes(self):
        return [Regime.TRENDING]


@pytest.fixture
def priced(synthetic_ohlcv):
    return IndicatorEngine().compute_all(synthetic_ohlcv(1200, trend=0.0005, seed=7).copy())


# ---------------------------------------------------------------------------
# The fast path must match the reference implementation
# ---------------------------------------------------------------------------


def test_build_at_matches_expanding_prefix_build(priced):
    """The safety net for the O(n^2) -> O(n) rewrite.

    build() used to be called on df.iloc[:i+1] for every bar, recomputing
    rolling statistics over the whole prefix each time. build_at must be
    indistinguishable from that, or every backtest silently changes meaning.
    """
    indicators = IndicatorEngine()

    reference = MarketStateBuilder(indicators, RegimeDetector())
    ref_states = [reference.build("BTCUSD", priced.iloc[: i + 1]) for i in range(49, len(priced))]

    fast = MarketStateBuilder(indicators, RegimeDetector())
    prepared = fast.prepare("BTCUSD", priced)
    fast_states = [fast.build_at(prepared, i) for i in range(49, len(priced))]

    assert [s.regime for s in ref_states] == [s.regime for s in fast_states]
    for a, b in zip(ref_states, fast_states):
        assert a.price == pytest.approx(b.price)
        assert a.regime_confidence == pytest.approx(b.regime_confidence)

    # Indicator payloads must agree key for key on the final bar.
    assert set(ref_states[-1].indicators) == set(fast_states[-1].indicators)
    for key, value in ref_states[-1].indicators.items():
        assert fast_states[-1].indicators[key] == pytest.approx(value)


def test_detect_series_leaves_the_same_final_state_as_detect(priced):
    sequential = RegimeDetector()
    for i in range(49, len(priced)):
        sequential.detect(priced.iloc[: i + 1])

    vectorised = RegimeDetector()
    vectorised.detect_series(priced)

    assert vectorised.current_regime == sequential.current_regime
    assert vectorised.confidence == pytest.approx(sequential.confidence)


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------


def test_costs_strictly_reduce_net_return(priced):
    """Costs must reduce the result — per trade, within a single run.

    Trade *counts* legitimately differ between a costed and a frictionless run:
    entry slippage shifts the fill, stop and target are ATR multiples of that
    fill, so exits trigger on different bars. Comparing totals across the two
    runs therefore compares two different trade sets; the honest invariant is
    per-trade, inside one run.
    """
    net = QuickBacktester(cost_model=CostModel()).run("BTCUSD", _AlwaysLong(), priced)

    assert net.trades > 0
    assert net.costs_pct > 0
    assert net.total_return_pct < net.gross_return_pct

    avg_cost_bps = net.costs_pct / net.trades * 100
    # Two taker legs at 5.9 bps plus slippage — tens of bps, not hundreds.
    assert 10 < avg_cost_bps < 60, f"implausible cost of {avg_cost_bps:.1f} bps/trade"


def test_gross_and_net_are_both_reported(priced):
    result = QuickBacktester(cost_model=CostModel()).run("BTCUSD", _AlwaysLong(), priced)
    payload = result.to_dict()

    assert payload["gross_return_pct"] > payload["total_return_pct"]
    assert payload["costs_pct"] == pytest.approx(
        payload["gross_return_pct"] - payload["total_return_pct"], abs=0.01
    )


def test_entry_is_filled_worse_than_the_bar_close(priced):
    result = QuickBacktester(cost_model=CostModel()).run("BTCUSD", _AlwaysLong(), priced)
    assert result.trade_log

    trade = result.trade_log[0]
    bar_close = float(priced["close"].iloc[trade["entry_idx"]])
    assert trade["entry_price"] > bar_close, "long entry filled at or better than the close"


def test_every_trade_records_a_positive_cost(priced):
    result = QuickBacktester(cost_model=CostModel()).run("BTCUSD", _AlwaysLong(), priced)
    assert all(t["cost_pct"] > 0 for t in result.trade_log)
    assert all(t["net_pct"] < t["gross_pct"] for t in result.trade_log)


def test_maker_entry_is_cheaper_per_trade_than_taker(priced):
    taker = QuickBacktester(cost_model=CostModel()).run("BTCUSD", _AlwaysLong(), priced)
    maker = QuickBacktester(cost_model=CostModel(), maker_entry=True).run(
        "BTCUSD", _AlwaysLong(), priced
    )

    taker_bps = taker.costs_pct / taker.trades * 100
    maker_bps = maker.costs_pct / maker.trades * 100
    assert maker_bps < taker_bps


def test_maker_entries_miss_when_price_gaps_away(synthetic_ohlcv):
    """A passive entry that never fills is a missed trade, not a free one.

    Counting misses is what stops maker mode from quietly granting both the
    cheaper fee and perfect participation.

    Note the synthetic fixture is gapless (open[i+1] == close[i], and low is
    always below open), so a resting buy limit there always fills. Real data
    gaps; this frame is lifted above the prior close on every bar so the limit
    can never be touched.
    """
    import numpy as np

    n = 600
    rng = np.random.default_rng(3)
    # Every bar's LOW sits above the previous bar's CLOSE, so a buy limit
    # resting at that close can never be touched.
    close = 100.0 * np.cumprod(1.0 + rng.uniform(0.002, 0.006, size=n))
    low = np.concatenate([[close[0] * 0.999], close[:-1] * 1.0005])
    high = np.maximum(close, low) * (1.0 + rng.uniform(0.0005, 0.002, size=n))
    open_ = (low + np.minimum(close, high)) / 2.0

    raw = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(500, 1500, size=n),
        },
        index=pd.date_range("2026-01-01", periods=n, freq="15min"),
    )
    assert (raw["low"].to_numpy()[1:] > raw["close"].to_numpy()[:-1]).all()

    gapping = IndicatorEngine().compute_all(raw.copy())
    result = QuickBacktester(cost_model=CostModel(), maker_entry=True).run(
        "BTCUSD", _AlwaysLong(), gapping
    )

    assert result.missed_entries > 0, "a buy limit filled in a market that never traded down"
    assert result.trades == 0, "a passive entry filled despite price gapping away every bar"
    assert result.to_dict()["missed_entries"] == result.missed_entries


def test_maker_entry_fills_at_the_limit_not_worse(priced):
    """A passive fill must never be slipped — that is the point of resting."""
    result = QuickBacktester(cost_model=CostModel(), maker_entry=True).run(
        "BTCUSD", _AlwaysLong(), priced
    )
    assert result.trade_log

    for trade in result.trade_log[:20]:
        # The limit rests at the close of the bar BEFORE the entry bar.
        limit = float(priced["close"].iloc[trade["entry_idx"] - 1])
        assert trade["entry_price"] == pytest.approx(limit)


def test_zero_cost_model_reproduces_frictionless_behaviour(priced):
    result = QuickBacktester(cost_model=CostModel.zero()).run("BTCUSD", _AlwaysLong(), priced)
    assert result.costs_pct == pytest.approx(0.0, abs=1e-9)
    assert result.total_return_pct == pytest.approx(result.gross_return_pct)


# ---------------------------------------------------------------------------
# Mechanics
# ---------------------------------------------------------------------------


def test_a_strategy_that_never_signals_produces_no_trades(priced):
    result = QuickBacktester().run("BTCUSD", _NeverTrades(), priced)
    assert result.trades == 0
    assert result.total_return_pct == 0.0
    # Below min_trades, the pair must be ineligible for selection.
    assert result.score == -1.0


def test_too_little_data_returns_an_empty_result(synthetic_ohlcv):
    tiny = IndicatorEngine().compute_all(synthetic_ohlcv(50).copy())
    assert QuickBacktester().run("BTCUSD", _AlwaysLong(), tiny).trades == 0


def test_only_one_position_is_held_at_a_time(priced):
    result = QuickBacktester(cost_model=CostModel.zero()).run("BTCUSD", _AlwaysLong(), priced)
    for earlier, later in zip(result.trade_log, result.trade_log[1:]):
        assert later["entry_idx"] > earlier["exit_idx"]


def test_max_hold_forces_an_exit(priced):
    result = QuickBacktester(cost_model=CostModel.zero(), max_hold_bars=5).run(
        "BTCUSD", _AlwaysLong(), priced
    )
    assert result.trades > 0
    for trade in result.trade_log:
        assert trade["exit_idx"] - trade["entry_idx"] <= 6


def test_wins_and_losses_partition_the_trades(priced):
    result = QuickBacktester(cost_model=CostModel()).run("BTCUSD", _AlwaysLong(), priced)
    assert result.wins + result.losses == result.trades
    if result.trades:
        assert result.win_rate == pytest.approx(result.wins / result.trades)


def test_results_are_deterministic(priced):
    a = QuickBacktester(cost_model=CostModel()).run("BTCUSD", _AlwaysLong(), priced)
    b = QuickBacktester(cost_model=CostModel()).run("BTCUSD", _AlwaysLong(), priced)
    assert a.to_dict() == b.to_dict()


def test_score_penalises_statistically_insignificant_results(priced):
    """A strategy with no real edge must not score positively."""
    result = QuickBacktester(cost_model=CostModel()).run("BTCUSD", _AlwaysLong(), priced)
    if result.t_stat < 1.0:
        assert result.score <= 0.0
