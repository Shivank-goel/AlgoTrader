"""Tests for the cross-sectional (portfolio) backtester and strategy.

The bug this suite exists to prevent is the one that actually happened: an
exploratory script used `sort_values(ascending=reverse)`, so its "momentum"
branch sorted descending and went long the *lowest*-ranked names. Every
conclusion drawn from it was sign-inverted. `test_long_winners_actually_buys_
the_winners` pins the semantics directly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest.costs import CostModel
from src.backtest.portfolio_backtest import PortfolioBacktester
from src.strategies.portfolio import PortfolioStrategy, PricePanel
from src.strategies.xs_momentum import CrossSectionalMomentum, Tilt


def _panel(n: int = 800, seed: int = 0) -> PricePanel:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="1h")
    frames = {}
    for k, sym in enumerate(("AAA", "BBB", "CCC", "DDD")):
        steps = rng.normal(0.0002 * (k - 1.5), 0.01, size=n)
        close = 100 * np.exp(np.cumsum(steps))
        frames[sym] = pd.DataFrame({"close": close}, index=idx)
    return PricePanel.from_frames(frames)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


def test_panel_inner_joins_on_timestamp():
    """A cross-sectional rank needs every symbol priced at the same instant."""
    idx = pd.date_range("2025-01-01", periods=10, freq="1h")
    frames = {
        "AAA": pd.DataFrame({"close": np.arange(10.0)}, index=idx),
        "BBB": pd.DataFrame({"close": np.arange(6.0)}, index=idx[:6]),
    }
    panel = PricePanel.from_frames(frames)
    assert len(panel) == 6
    assert panel.symbols == ["AAA", "BBB"]


def test_neutralise_produces_a_market_neutral_book():
    raw = pd.Series({"A": 1.0, "B": 1.0, "C": -1.0, "D": -1.0})
    w = PortfolioStrategy._neutralise(raw, gross_cap=1.0)
    assert w.sum() == pytest.approx(0.0, abs=1e-12)
    assert w.abs().sum() == pytest.approx(1.0)


def test_neutralise_removes_a_common_directional_tilt():
    """All-long input must come out net flat — that is what neutral means."""
    raw = pd.Series({"A": 1.0, "B": 1.0, "C": 1.0, "D": 0.0})
    w = PortfolioStrategy._neutralise(raw)
    assert w.sum() == pytest.approx(0.0, abs=1e-12)
    assert (w < 0).any(), "neutralising an all-long book must create shorts"


def test_neutralise_handles_an_empty_book():
    raw = pd.Series({"A": 0.0, "B": 0.0})
    assert PortfolioStrategy._neutralise(raw).abs().sum() == 0.0


# ---------------------------------------------------------------------------
# Signal semantics — the sign bug
# ---------------------------------------------------------------------------


def test_long_winners_actually_buys_the_winners():
    """Direct guard against the inverted-label bug."""
    n = 400
    idx = pd.date_range("2025-01-01", periods=n, freq="1h")
    # WINNER rises monotonically, LOSER falls; the other two are flat.
    frames = {
        "WINNER": pd.DataFrame({"close": np.linspace(100, 200, n)}, index=idx),
        "LOSER": pd.DataFrame({"close": np.linspace(200, 100, n)}, index=idx),
        "FLAT1": pd.DataFrame({"close": np.full(n, 150.0)}, index=idx),
        "FLAT2": pd.DataFrame({"close": np.full(n, 150.0)}, index=idx),
    }
    panel = PricePanel.from_frames(frames)

    mom = CrossSectionalMomentum({"formation_bars": 168, "n_legs": 1, "tilt": Tilt.LONG_WINNERS})
    w = mom.target_weights(panel, n - 1)
    assert w["WINNER"] > 0, "LONG_WINNERS must be long the rising symbol"
    assert w["LOSER"] < 0, "LONG_WINNERS must be short the falling symbol"

    rev = CrossSectionalMomentum({"formation_bars": 168, "n_legs": 1, "tilt": Tilt.LONG_LOSERS})
    wr = rev.target_weights(panel, n - 1)
    assert wr["WINNER"] < 0
    assert wr["LOSER"] > 0


def test_the_two_tilts_are_exact_opposites():
    panel = _panel()
    kw = {"formation_bars": 168, "vol_bars": 168}
    w_mom = CrossSectionalMomentum({**kw, "tilt": Tilt.LONG_WINNERS}).target_weights(panel, 500)
    w_rev = CrossSectionalMomentum({**kw, "tilt": Tilt.LONG_LOSERS}).target_weights(panel, 500)
    pd.testing.assert_series_equal(w_mom, -w_rev)


# ---------------------------------------------------------------------------
# Lookahead
# ---------------------------------------------------------------------------


def test_weights_do_not_depend_on_future_bars():
    """Truncating the panel after bar i must not change the decision at bar i."""
    panel = _panel()
    strat = CrossSectionalMomentum({"formation_bars": 168, "vol_bars": 168})
    i = 500

    full = strat.target_weights(panel, i)
    truncated = strat.target_weights(
        PricePanel(close=panel.close.iloc[: i + 1], returns=panel.returns.iloc[: i + 1]), i
    )
    pd.testing.assert_series_equal(full, truncated)


def test_no_signal_before_warmup():
    panel = _panel()
    strat = CrossSectionalMomentum({"formation_bars": 336, "vol_bars": 336})
    assert strat.target_weights(panel, 10).abs().sum() == 0.0


# ---------------------------------------------------------------------------
# Backtester accounting
# ---------------------------------------------------------------------------


def test_costs_reduce_net_below_gross():
    panel = _panel()
    strat = CrossSectionalMomentum({"formation_bars": 168, "vol_bars": 168})
    r = PortfolioBacktester(CostModel()).run(strat, panel, hold_bars=72)

    assert r.rebalances > 0
    assert r.costs_pct > 0
    assert r.net_return_pct < r.gross_return_pct
    assert r.net_return_pct == pytest.approx(r.gross_return_pct - r.costs_pct, abs=1e-6)


def test_zero_cost_model_leaves_gross_untouched():
    panel = _panel()
    strat = CrossSectionalMomentum({"formation_bars": 168, "vol_bars": 168})
    r = PortfolioBacktester(CostModel.zero()).run(strat, panel, hold_bars=72)
    assert r.costs_pct == pytest.approx(0.0, abs=1e-9)


def test_costs_scale_with_turnover():
    """Rebalancing more often must cost more, all else equal."""
    panel = _panel()
    kw = {"formation_bars": 168, "vol_bars": 168}
    bt = PortfolioBacktester(CostModel())
    fast = bt.run(CrossSectionalMomentum(kw), panel, hold_bars=24)
    slow = bt.run(CrossSectionalMomentum(kw), panel, hold_bars=168)
    assert fast.costs_pct > slow.costs_pct


def test_a_flat_strategy_produces_no_trades():
    class Flat(PortfolioStrategy):
        name = "flat"

        def min_history(self):
            return 10

        def target_weights(self, panel, i):
            return pd.Series(0.0, index=panel.close.columns)

    r = PortfolioBacktester(CostModel()).run(Flat(), _panel(), hold_bars=24)
    assert r.rebalances == 0
    assert r.net_return_pct == 0.0


def test_results_are_deterministic():
    panel = _panel()
    strat = CrossSectionalMomentum({"formation_bars": 168, "vol_bars": 168})
    bt = PortfolioBacktester(CostModel())
    assert bt.run(strat, panel, hold_bars=72).to_dict() == bt.run(
        strat, panel, hold_bars=72
    ).to_dict()


def test_backtester_never_exposes_future_rows():
    class Inspector(PortfolioStrategy):
        def min_history(self):
            return 1

        def target_weights(self, panel, i):
            assert len(panel.close) == len(panel.returns) == i + 1
            return pd.Series(0., index=panel.symbols)

    PortfolioBacktester().run(Inspector(), _panel(n=30), hold_bars=2)


def test_liquidation_fee_updates_drawdown_and_timestamps():
    class Exit(PortfolioStrategy):
        def min_history(self):
            return 1

        def target_weights(self, panel, i):
            return pd.Series({"A": 1. if i == 1 else 0.})

    close = pd.DataFrame({"A": [100.] * 5}, index=pd.date_range("2020-01-01", periods=5))
    result = PortfolioBacktester().run(Exit(), PricePanel(close, close.pct_change()), hold_bars=1)
    assert len(result.timestamps) == len(result.period_returns) == 2
    equity = np.prod(1 + np.asarray(result.period_returns))
    assert result.max_drawdown_pct == pytest.approx((1 - equity) * 100)


def test_too_little_data_returns_an_empty_result():
    small = PricePanel(
        close=_panel(n=50).close, returns=_panel(n=50).returns
    )
    strat = CrossSectionalMomentum({"formation_bars": 336, "vol_bars": 336})
    assert PortfolioBacktester(CostModel()).run(strat, small, hold_bars=24).rebalances == 0


def test_split_covers_the_whole_period():
    panel = _panel()
    strat = CrossSectionalMomentum({"formation_bars": 168, "vol_bars": 168})
    bt = PortfolioBacktester(CostModel())
    halves = bt.run_split(strat, panel, hold_bars=24, n_splits=2)
    assert len(halves) == 2
    assert all(h.rebalances > 0 for h in halves)


def test_a_market_neutral_book_is_insensitive_to_a_common_drift():
    """Adding the same drift to every symbol must not create P&L.

    This is the property that makes the strategy alpha rather than beta, and it
    is what the Round 1 strategies failed.
    """
    n = 900
    idx = pd.date_range("2025-01-01", periods=n, freq="1h")
    rng = np.random.default_rng(2)
    base = {s: np.cumsum(rng.normal(0, 0.01, n)) for s in ("A", "B", "C", "D")}

    def build(drift):
        return PricePanel.from_frames({
            s: pd.DataFrame(
                {"close": 100 * np.exp(v + drift * np.arange(n))}, index=idx
            )
            for s, v in base.items()
        })

    strat = CrossSectionalMomentum({"formation_bars": 168, "vol_bars": 168})
    bt = PortfolioBacktester(CostModel.zero())
    flat = bt.run(strat, build(0.0), hold_bars=72).net_return_pct
    bull = bt.run(strat, build(0.0005), hold_bars=72).net_return_pct

    assert bull == pytest.approx(flat, abs=1.5), (
        f"a common drift moved returns {flat:.2f}% -> {bull:.2f}%; the book is not neutral"
    )
