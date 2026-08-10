"""Cross-sectional (portfolio) strategies.

`BaseStrategy` answers "should I trade THIS symbol?" one symbol at a time, which
is all `TradingEngine.process_symbol` can express. A cross-sectional strategy
cannot be written that way: its decision for ADAUSD depends on how ADAUSD ranks
against XRPUSD, DOGEUSD and ETHUSD *right now*. It needs the whole panel at once
and emits a set of offsetting weights, not an independent signal.

That distinction is the point. Round 1 established that every apparently
profitable configuration was long-beta — it made money in the bull year and gave
it back in the bear. A long/short book with zero net exposure removes market
direction by construction, so whatever remains is either alpha or nothing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class PricePanel:
    """Aligned close prices for a set of symbols, plus derived series.

    Built once per backtest so per-rebalance work stays cheap, mirroring
    `MarketStateBuilder.prepare` for the single-symbol path.
    """

    close: pd.DataFrame               # index = timestamp, columns = symbols
    returns: pd.DataFrame             # simple bar-over-bar returns

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)

    def __len__(self) -> int:
        return len(self.close)

    @classmethod
    def from_frames(cls, frames: dict[str, pd.DataFrame]) -> PricePanel:
        """Build from {symbol: ohlcv_df}, inner-joined on timestamp.

        Inner join matters: a cross-sectional rank is only meaningful when every
        symbol has a price at that instant.
        """
        close = pd.DataFrame(
            {symbol: df["close"] for symbol, df in frames.items()}
        ).dropna()
        return cls(close=close, returns=close.pct_change())

    def trailing_return(self, lookback: int) -> pd.DataFrame:
        return self.close.pct_change(lookback)

    def trailing_vol(self, lookback: int) -> pd.DataFrame:
        """Realised volatility per symbol over the lookback window."""
        return self.returns.rolling(lookback).std()


class PortfolioStrategy(ABC):
    """Emits target weights across a universe, rather than per-symbol signals.

    Weights are fractions of gross exposure: positive is long, negative short.
    A market-neutral book sums to ~0 and its absolute values sum to <= 1.
    """

    name: str = "portfolio_base"

    def __init__(self, params: Optional[dict] = None) -> None:
        self.params = params or {}

    @abstractmethod
    def target_weights(self, panel: PricePanel, i: int) -> pd.Series:
        """Desired weights at bar `i`, using only information available then.

        Implementations must not read `panel.close.iloc[j]` for any j > i.
        """

    @abstractmethod
    def min_history(self) -> int:
        """Bars of warm-up required before the first valid signal."""

    @staticmethod
    def _neutralise(weights: pd.Series, gross_cap: float = 1.0) -> pd.Series:
        """Force net-zero exposure and cap gross.

        Subtracting the mean is what makes the book market-neutral: it removes
        whatever common direction the raw scores carried.
        """
        if weights.abs().sum() == 0:
            return weights

        centred = weights - weights.mean()
        gross = centred.abs().sum()
        if gross <= 0:
            return centred * 0.0
        return centred / gross * gross_cap
