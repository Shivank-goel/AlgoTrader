"""Tests for the NSE intraday cross-sectional sleeve.

Two things carry most of the weight:

  1. **Sign semantics.** `LONG_LOSERS` must actually buy the losers. An earlier
     exploratory script inverted exactly this and produced a conclusion that was
     backwards for a whole round of work.
  2. **The rebalance band.** At ₹10,000 a full daily rebalance costs ~106% of
     capital a year, so suppressing trades too small to be worth their fee is
     the difference between compounding and bleeding.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest.intraday_xs_backtest import IntradayCrossSectionalBacktester
from src.backtest.nse_costs import NSEEquityCostModel
from src.strategies.nse_intraday_xs import IntradayCrossSectional, Signal, Tilt


def _panels(n_days: int = 300, n_names: int = 40, seed: int = 0):
    """Random-walk opens/closes for a synthetic universe."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n_days, freq="B")
    cols = [f"S{i:02d}" for i in range(n_names)]

    closes = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0, 0.015, size=(n_days, n_names)), axis=0)),
        index=idx, columns=cols,
    )
    # Open gaps slightly from the prior close.
    opens = closes.shift(1) * (1 + rng.normal(0, 0.004, size=(n_days, n_names)))
    opens.iloc[0] = closes.iloc[0]
    return opens.fillna(closes), closes


@pytest.fixture
def panels():
    return _panels()


# ---------------------------------------------------------------------------
# Sign semantics
# ---------------------------------------------------------------------------


def test_long_losers_buys_yesterdays_worst_performer():
    """The guard against the inverted-label bug."""
    idx = pd.date_range("2024-01-01", periods=10, freq="B")
    cols = ["WINNER", "LOSER", "MID1", "MID2"]
    closes = pd.DataFrame(100.0, index=idx, columns=cols)
    closes.loc[idx[-2], "WINNER"] = 120.0   # big up day
    closes.loc[idx[-2], "LOSER"] = 80.0     # big down day
    opens = closes.copy()

    strat = IntradayCrossSectional({"n_legs": 1, "tilt": Tilt.LONG_LOSERS})
    w = strat.target_weights(opens, closes, len(closes) - 1)

    assert w["LOSER"] > 0, "LONG_LOSERS must be long the worst performer"
    assert w["WINNER"] < 0


def test_long_winners_is_the_exact_opposite():
    opens, closes = _panels(n_days=60, n_names=10)
    i = len(closes) - 1
    kw = {"n_legs": 2}
    a = IntradayCrossSectional({**kw, "tilt": Tilt.LONG_LOSERS}).target_weights(opens, closes, i)
    b = IntradayCrossSectional({**kw, "tilt": Tilt.LONG_WINNERS}).target_weights(opens, closes, i)
    pd.testing.assert_series_equal(a, -b)


# ---------------------------------------------------------------------------
# Book construction
# ---------------------------------------------------------------------------


def test_book_is_market_neutral_and_gross_capped(panels):
    opens, closes = panels
    strat = IntradayCrossSectional({"n_legs": 5, "gross_cap": 1.0})
    w = strat.target_weights(opens, closes, 100)

    assert w.sum() == pytest.approx(0.0, abs=1e-12)
    assert w.abs().sum() == pytest.approx(1.0)
    assert (w > 0).sum() == 5
    assert (w < 0).sum() == 5


def test_no_book_before_warmup(panels):
    opens, closes = panels
    strat = IntradayCrossSectional({"formation_days": 20, "n_legs": 5})
    assert strat.target_weights(opens, closes, 3).abs().sum() == 0.0


def test_insufficient_names_produces_no_book():
    opens, closes = _panels(n_days=50, n_names=4)
    strat = IntradayCrossSectional({"n_legs": 5})  # needs 10 names
    assert strat.target_weights(opens, closes, 40).abs().sum() == 0.0


def test_extreme_moves_are_excluded_as_corporate_actions():
    """A 1:5 split reads as -80%; buying it as 'the biggest loser' is a bug."""
    idx = pd.date_range("2024-01-01", periods=10, freq="B")
    cols = ["SPLIT", "A", "B", "C", "D", "E"]
    closes = pd.DataFrame(100.0, index=idx, columns=cols)
    closes.loc[idx[-2], "SPLIT"] = 20.0     # -80%
    closes.loc[idx[-2], "A"] = 97.0         # a real -3% loser
    opens = closes.copy()

    strat = IntradayCrossSectional({"n_legs": 1, "max_abs_signal_pct": 20.0})
    w = strat.target_weights(opens, closes, len(closes) - 1)

    assert w["SPLIT"] == 0.0, "implausible move should be filtered out"
    assert w["A"] > 0, "the genuine loser should be bought instead"


@pytest.mark.parametrize("signal", list(Signal))
def test_every_signal_variant_produces_a_valid_book(panels, signal):
    opens, closes = panels
    strat = IntradayCrossSectional({"signal": signal, "n_legs": 5, "formation_days": 5})
    w = strat.target_weights(opens, closes, 100)
    assert w.sum() == pytest.approx(0.0, abs=1e-12)
    assert w.abs().sum() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Lookahead
# ---------------------------------------------------------------------------


def test_signal_uses_only_past_closes(panels):
    """Truncating everything after session i must not change the book at i."""
    opens, closes = panels
    strat = IntradayCrossSectional({"n_legs": 5})
    i = 150

    full = strat.target_weights(opens, closes, i)
    cut = strat.target_weights(opens.iloc[: i + 1], closes.iloc[: i + 1], i)
    pd.testing.assert_series_equal(full, cut)


def test_close_to_close_signal_ignores_todays_prices(panels):
    """Only the gap variant may read today's open, and none may read today's close."""
    opens, closes = panels
    strat = IntradayCrossSectional({"signal": Signal.PREV_CLOSE_TO_CLOSE, "n_legs": 5})
    i = 150

    before = strat.target_weights(opens, closes, i)

    tampered_o, tampered_c = opens.copy(), closes.copy()
    tampered_o.iloc[i] *= 1.5
    tampered_c.iloc[i] *= 0.5
    after = strat.target_weights(tampered_o, tampered_c, i)

    pd.testing.assert_series_equal(before, after)


# ---------------------------------------------------------------------------
# Rebalance band — the economic lever
# ---------------------------------------------------------------------------


def test_zero_band_rebalances_fully(panels):
    strat = IntradayCrossSectional({"n_legs": 5, "rebalance_band": 0.0})
    target = pd.Series({"A": 0.1, "B": -0.1})
    current = pd.Series({"A": 0.0, "B": 0.0})
    pd.testing.assert_series_equal(strat.apply_rebalance_band(target, current), target)


def test_band_suppresses_small_adjustments():
    strat = IntradayCrossSectional({"n_legs": 5, "gross_cap": 1.0, "rebalance_band": 0.5})
    # A full leg is 1.0 / (2*5) = 0.1; the band threshold is 0.05.
    target = pd.Series({"KEEP": 0.10, "TWEAK": 0.12})
    current = pd.Series({"KEEP": 0.00, "TWEAK": 0.10})

    held = strat.apply_rebalance_band(target, current)
    assert held["KEEP"] == 0.10, "a full new position must still be taken"
    assert held["TWEAK"] == 0.10, "a 0.02 nudge is below the band and should be skipped"


def test_band_reduces_cost_only_when_positions_can_be_carried(panels):
    """The band is useless under MIS, and that is the point.

    A rebalance band saves money by *keeping* a position instead of re-trading
    it. MIS forbids carrying, so every position is re-established every session
    regardless — the band cannot help. It only bites for a strategy that holds
    (`squares_off_daily=False`), which for a long/short book is impossible in
    Indian cash equity (K-10).
    """
    opens, closes = panels
    cfg_loose = {"n_legs": 5, "rebalance_band": 0.0}
    cfg_tight = {"n_legs": 5, "rebalance_band": 1.5}

    carrying = IntradayCrossSectionalBacktester(NSEEquityCostModel(), squares_off_daily=False)
    loose = carrying.run(IntradayCrossSectional(cfg_loose), opens, closes)
    tight = carrying.run(IntradayCrossSectional(cfg_tight), opens, closes)
    assert tight.avg_turnover < loose.avg_turnover
    assert tight.costs_pct < loose.costs_pct


def test_daily_square_off_imposes_a_cost_floor(panels):
    """An intraday book pays a full round trip every session, always.

    Regression for a real bug: the backtester charged only `book - held`, which
    is zero for a name that stays in the book. That silently understated the
    cost of every intraday strategy and made the seed sleeve look profitable.
    """
    opens, closes = panels
    model = NSEEquityCostModel()
    strat = IntradayCrossSectional({"n_legs": 5})

    squared = IntradayCrossSectionalBacktester(model, squares_off_daily=True).run(
        strat, opens, closes
    )
    carried = IntradayCrossSectionalBacktester(model, squares_off_daily=False).run(
        strat, opens, closes
    )

    assert squared.costs_pct > carried.costs_pct, "square-off must cost at least as much"

    # Gross exposure is 1.0, so the floor is exactly one round trip per session.
    per_session_bps = squared.costs_pct / squared.sessions * 100
    assert per_session_bps == pytest.approx(model.round_trip_fee_bps_at(1_000), rel=0.02)


def test_square_off_cost_is_independent_of_the_band(panels):
    """Corollary: under MIS no turnover control can reduce the fee."""
    opens, closes = panels
    bt = IntradayCrossSectionalBacktester(NSEEquityCostModel(), squares_off_daily=True)

    a = bt.run(IntradayCrossSectional({"n_legs": 5, "rebalance_band": 0.0}), opens, closes)
    b = bt.run(IntradayCrossSectional({"n_legs": 5, "rebalance_band": 0.5}), opens, closes)
    assert a.costs_pct == pytest.approx(b.costs_pct, rel=0.02)


def test_a_band_wider_than_a_full_leg_never_opens_a_position(panels):
    """Degenerate but worth pinning: the band suppresses the opening trade too.

    A full leg is gross_cap / (2 * n_legs). A band above 1.0 makes the threshold
    exceed that, so the move from flat to a full position is itself suppressed
    and the book stays empty forever.
    """
    opens, closes = panels
    r = IntradayCrossSectionalBacktester(NSEEquityCostModel()).run(
        IntradayCrossSectional({"n_legs": 5, "rebalance_band": 2.0}), opens, closes
    )
    assert r.costs_pct == 0.0
    assert r.net_return_pct == 0.0


# ---------------------------------------------------------------------------
# Backtester accounting
# ---------------------------------------------------------------------------


def test_costs_reduce_net_below_gross(panels):
    opens, closes = panels
    r = IntradayCrossSectionalBacktester(NSEEquityCostModel()).run(
        IntradayCrossSectional({"n_legs": 5}), opens, closes
    )
    assert r.sessions > 0
    assert r.costs_pct > 0
    assert r.net_return_pct < r.gross_return_pct


def test_cost_matches_the_published_rate():
    """Turnover of 1.0 is ONE leg per name — half a round trip.

    Establishing a book is turnover 1.0; unwinding it is another 1.0. So a
    complete round trip is turnover 2.0 and costs the published 10.6 bps, and a
    single leg costs half that.
    """
    bt = IntradayCrossSectionalBacktester(NSEEquityCostModel())

    one_leg = bt._turnover_cost_pct(pd.Series({"A": 0.5, "B": -0.5}))
    assert one_leg * 100 == pytest.approx(10.6045 / 2, abs=0.05)

    round_trip = one_leg + bt._turnover_cost_pct(pd.Series({"A": -0.5, "B": 0.5}))
    assert round_trip * 100 == pytest.approx(10.6045, abs=0.05)


def test_a_flat_strategy_costs_nothing(panels):
    opens, closes = panels
    strat = IntradayCrossSectional({"n_legs": 500})  # more legs than names
    r = IntradayCrossSectionalBacktester().run(strat, opens, closes)
    assert r.sessions == 0
    assert r.costs_pct == 0.0


def test_results_are_deterministic(panels):
    opens, closes = panels
    bt = IntradayCrossSectionalBacktester()
    strat = IntradayCrossSectional({"n_legs": 5})
    assert bt.run(strat, opens, closes).to_dict() == bt.run(strat, opens, closes).to_dict()


def test_a_market_wide_drift_produces_no_pnl():
    """The neutrality property. This is what the Round 1 strategies failed."""
    rng = np.random.default_rng(3)
    n_days, n_names = 250, 20
    idx = pd.date_range("2024-01-01", periods=n_days, freq="B")
    cols = [f"S{i}" for i in range(n_names)]
    base = np.cumsum(rng.normal(0, 0.012, size=(n_days, n_names)), axis=0)

    def build(drift):
        c = pd.DataFrame(100 * np.exp(base + drift * np.arange(n_days)[:, None]),
                         index=idx, columns=cols)
        o = c.shift(1).fillna(c.iloc[0])
        return o, c

    bt = IntradayCrossSectionalBacktester(NSEEquityCostModel())
    strat = IntradayCrossSectional({"n_legs": 5})

    flat = bt.run(strat, *build(0.0)).gross_return_pct
    bull = bt.run(strat, *build(0.001)).gross_return_pct

    assert bull == pytest.approx(flat, abs=1.0), (
        f"a common drift moved gross return {flat:.2f}% -> {bull:.2f}%; book is not neutral"
    )


def test_split_covers_both_halves(panels):
    opens, closes = panels
    halves = IntradayCrossSectionalBacktester().run_split(
        IntradayCrossSectional({"n_legs": 5}), opens, closes, n_splits=2
    )
    assert len(halves) == 2
    assert all(h.sessions > 0 for h in halves)
