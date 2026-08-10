"""Cross-sectional momentum / reversal.

Hypothesis (momentum): within a universe of perpetuals, the recent relative
winners keep outperforming the recent relative losers over the following days.
Long the winners, short the losers, in equal risk — a market-neutral book.

Why this, out of ~120 hypotheses tested on 730 days: it is the only one whose
gross signal reached significance (t = +2.10, +32 bps per rebalance, 168h
formation / 72h hold, six symbols). It is also market-neutral by construction,
which addresses the Round 1 diagnosis that every apparently profitable
configuration was long-beta rather than alpha, and it matches the published
cross-sectional momentum literature for crypto.

A correction worth recording: an earlier exploratory script reported the exact
opposite — that momentum was reliably *negative* and reversal was the signal.
That script used `sort_values(ascending=reverse)`, so its "momentum" branch
sorted descending and went long the *lowest*-ranked names. The labels were
inverted; the underlying numbers were right. Hence `Direction` below is an
explicit enum rather than a bare sign, and `LONG_WINNERS` is spelled out in the
weight assignment, so the semantics cannot be misread again.

Two refinements are available but **off by default**: volatility-normalised
ranking and inverse-volatility leg weighting. Both were measured and both made
results worse on this universe, so they are opt-in rather than assumed.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from src.strategies.portfolio import PortfolioStrategy, PricePanel


class Tilt(str, Enum):
    """Which side of the cross-section to buy. Spelled out deliberately."""

    LONG_WINNERS = "long_winners"   # momentum
    LONG_LOSERS = "long_losers"     # reversal


class CrossSectionalMomentum(PortfolioStrategy):
    """Market-neutral book ranked on trailing relative performance."""

    name = "xs_momentum"

    def __init__(self, params: Optional[dict] = None) -> None:
        super().__init__(params)
        p = self.params
        self.formation_bars: int = int(p.get("formation_bars", 168))
        self.vol_bars: int = int(p.get("vol_bars", 168))
        self.n_legs: int = int(p.get("n_legs", 2))
        self.gross_cap: float = float(p.get("gross_cap", 1.0))
        # Both measured worse on this universe; opt-in, not assumed.
        self.vol_normalise_signal: bool = bool(p.get("vol_normalise_signal", False))
        self.inverse_vol_weight: bool = bool(p.get("inverse_vol_weight", False))
        self.tilt: Tilt = Tilt(p.get("tilt", Tilt.LONG_WINNERS))

    def min_history(self) -> int:
        return max(self.formation_bars, self.vol_bars) + 2

    def _scores(self, panel: PricePanel, i: int) -> Optional[pd.Series]:
        """Cross-sectional signal at bar `i`, using only bars <= i."""
        window = panel.close.iloc[: i + 1]
        if len(window) <= self.formation_bars:
            return None

        trailing = window.iloc[-1] / window.iloc[-1 - self.formation_bars] - 1.0

        if self.vol_normalise_signal:
            recent = panel.returns.iloc[max(0, i + 1 - self.vol_bars) : i + 1]
            vol = recent.std()
            vol = vol.replace(0.0, np.nan)
            trailing = trailing / vol

        return trailing.replace([np.inf, -np.inf], np.nan).dropna()

    def _leg_weights(self, panel: PricePanel, i: int, symbols: pd.Index) -> pd.Series:
        """Per-symbol scaling so each leg carries comparable risk."""
        if not self.inverse_vol_weight:
            return pd.Series(1.0, index=symbols)

        recent = panel.returns.iloc[max(0, i + 1 - self.vol_bars) : i + 1]
        vol = recent.std().reindex(symbols)
        vol = vol.replace(0.0, np.nan)
        if vol.isna().all():
            return pd.Series(1.0, index=symbols)

        inv = 1.0 / vol
        return inv.fillna(inv.mean())

    def target_weights(self, panel: PricePanel, i: int) -> pd.Series:
        empty = pd.Series(0.0, index=panel.close.columns)

        scores = self._scores(panel, i)
        if scores is None or len(scores) < 2 * self.n_legs:
            return empty

        # Ascending: worst trailing performer first, best last.
        ranked = scores.sort_values(ascending=True)
        losers = ranked.index[: self.n_legs]
        winners = ranked.index[-self.n_legs :]

        raw = pd.Series(0.0, index=panel.close.columns)
        scale = self._leg_weights(panel, i, panel.close.columns)

        if self.tilt is Tilt.LONG_WINNERS:
            long_leg, short_leg = winners, losers
        else:
            long_leg, short_leg = losers, winners

        raw[long_leg] = scale[long_leg]
        raw[short_leg] = -scale[short_leg]

        return self._neutralise(raw, gross_cap=self.gross_cap)
