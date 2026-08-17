"""Intraday cross-sectional reversal on NSE cash equity.

The seed sleeve. Rank the universe on the previous session's move, take the
extremes, hold from today's open to today's close, square off. Market-neutral by
construction, which is the point: Round 1 established that every apparently
profitable configuration was long-beta, and a net-zero book removes that by
design so whatever remains is either alpha or nothing.

Why this needs only daily bars: the signal is yesterday's close-to-close return
and the holding period is today's open-to-close. Both come out of daily OHLC, so
the strategy gets years of history instead of the 60 days of intraday data free
sources allow.

**Turnover is the binding economic constraint, not the signal.** At ₹10,000 with
4x MIS, a full daily rebalance costs ~106% of capital per year at 10.6 bps a
round trip. So `rebalance_band` exists as a first-class parameter: a name is only
traded when its rank moves far enough to be worth the fee. This is where the
strategy lives or dies, and it is swept alongside the signal parameters rather
than fixed afterwards.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class Signal(str, Enum):
    """Which past move to rank on. Spelled out; a bare sign got inverted once."""

    PREV_CLOSE_TO_CLOSE = "prev_close_to_close"
    OVERNIGHT_GAP = "overnight_gap"          # prev close -> today's open
    MULTI_DAY = "multi_day"                  # cumulative over `formation_days`


class Tilt(str, Enum):
    LONG_LOSERS = "long_losers"    # reversal
    LONG_WINNERS = "long_winners"  # momentum


class IntradayCrossSectional:
    """Daily-rebalanced, market-neutral, open-to-close book.

    Not a `PortfolioStrategy`: that interface assumes a single close-price panel
    and a bar-index holding period. This one needs open *and* close, and its
    holding period is exactly one session, so it carries its own panel type and
    backtester rather than distorting the crypto abstraction to fit.
    """

    name = "nse_intraday_xs"

    def __init__(self, params: Optional[dict] = None) -> None:
        p = params or {}
        self.params = p
        self.signal = Signal(p.get("signal", Signal.PREV_CLOSE_TO_CLOSE))
        self.tilt = Tilt(p.get("tilt", Tilt.LONG_LOSERS))
        self.n_legs: int = int(p.get("n_legs", 20))
        self.formation_days: int = int(p.get("formation_days", 1))
        self.gross_cap: float = float(p.get("gross_cap", 1.0))
        # A name is only re-traded when its target weight moves by more than
        # this fraction of a full leg. 0.0 = rebalance everything, every day.
        self.rebalance_band: float = float(p.get("rebalance_band", 0.0))
        self.vol_normalise: bool = bool(p.get("vol_normalise", False))
        self.vol_window: int = int(p.get("vol_window", 20))
        # Skip names whose prior move is extreme enough to suggest a corporate
        # action or a circuit hit rather than a tradeable dislocation.
        self.max_abs_signal_pct: float = float(p.get("max_abs_signal_pct", 20.0))

    def min_history(self) -> int:
        return max(self.formation_days, self.vol_window if self.vol_normalise else 0) + 2

    # -- signal ---------------------------------------------------------

    def raw_signal(
        self, opens: pd.DataFrame, closes: pd.DataFrame, i: int
    ) -> Optional[pd.Series]:
        """Cross-sectional score at session `i`.

        Every variant uses only prices from sessions strictly before `i`, so no
        price ever appears in both the signal and the return the book earns.
        """
        if i < self.min_history():
            return None

        if self.signal is Signal.PREV_CLOSE_TO_CLOSE:
            score = closes.iloc[i - 1] / closes.iloc[i - 2] - 1.0
        elif self.signal is Signal.OVERNIGHT_GAP:
            # The gap of the PREVIOUS session, not this one.
            #
            # Using this session's gap (O_i / C_{i-1}) shares the price O_i with
            # the O_i -> C_i return the book earns, so noise in that single
            # print makes a name look like a bigger loser AND gives it a higher
            # return. That is mechanical, not economic, and it inflated an
            # earlier version of this strategy to Sharpe 6.48 (see K-71).
            # Lagging by one session removes the shared price entirely; the
            # signal survives at about a third the apparent size.
            score = opens.iloc[i - 1] / closes.iloc[i - 2] - 1.0
        else:  # MULTI_DAY
            score = closes.iloc[i - 1] / closes.iloc[i - 1 - self.formation_days] - 1.0

        score = score.replace([np.inf, -np.inf], np.nan).dropna()

        # Drop implausible moves: splits, bonuses, circuit-locked names.
        score = score[score.abs() * 100 <= self.max_abs_signal_pct]

        if self.vol_normalise and len(score):
            window = closes.iloc[max(0, i - self.vol_window) : i]
            vol = window.pct_change().std().reindex(score.index).replace(0.0, np.nan)
            score = (score / vol).replace([np.inf, -np.inf], np.nan).dropna()

        return score if len(score) >= 2 * self.n_legs else None

    def target_weights(
        self, opens: pd.DataFrame, closes: pd.DataFrame, i: int
    ) -> pd.Series:
        """Desired book for session `i`. Net-zero, gross capped."""
        empty = pd.Series(0.0, index=closes.columns)
        score = self.raw_signal(opens, closes, i)
        if score is None:
            return empty

        ranked = score.sort_values(ascending=True)   # worst first
        losers = ranked.index[: self.n_legs]
        winners = ranked.index[-self.n_legs :]

        if self.tilt is Tilt.LONG_LOSERS:
            long_leg, short_leg = losers, winners
        else:
            long_leg, short_leg = winners, losers

        w = pd.Series(0.0, index=closes.columns)
        w[long_leg] = 1.0
        w[short_leg] = -1.0

        # Net-zero, then scale to the gross budget.
        w = w - w.mean()
        gross = w.abs().sum()
        if gross <= 0:
            return empty
        return w / gross * self.gross_cap

    def apply_rebalance_band(
        self, target: pd.Series, current: pd.Series
    ) -> pd.Series:
        """Suppress trades too small to be worth their fee.

        Returns the book to actually hold: names inside the band keep their
        existing weight rather than being nudged to target. This is the single
        most important economic lever in the strategy.
        """
        if self.rebalance_band <= 0:
            return target

        full_leg = self.gross_cap / (2 * self.n_legs) if self.n_legs else 0.0
        threshold = full_leg * self.rebalance_band

        held = target.copy()
        unchanged = (target - current).abs() < threshold
        held[unchanged] = current[unchanged]
        return held
