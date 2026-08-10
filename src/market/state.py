"""Market state snapshot builder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.core.models import Candle, MarketState, Regime
from src.data.indicators import IndicatorEngine
from src.market.regime import RegimeDetector


@dataclass
class PreparedFrame:
    """Per-frame precomputation shared by every bar of a backtest."""

    symbol: str
    arrays: dict[str, np.ndarray]
    closes: np.ndarray
    timestamps: Optional[Any]
    regimes: list[Regime]
    confidences: np.ndarray

    def __len__(self) -> int:
        return len(self.closes)


class MarketStateBuilder:
    """Builds a frozen MarketState from current data."""

    def __init__(
        self,
        indicator_engine: Optional[IndicatorEngine] = None,
        regime_detector: Optional[RegimeDetector] = None,
    ) -> None:
        self.indicators = indicator_engine or IndicatorEngine()
        self.regime_detector = regime_detector or RegimeDetector(self.indicators)

    def prepare(self, symbol: str, df: pd.DataFrame) -> "PreparedFrame":
        """Precompute everything build_at needs for a whole frame.

        Splitting preparation from per-bar access is what makes bar-by-bar
        backtesting linear instead of quadratic.
        """
        regimes, confidences = self.regime_detector.detect_series(df)
        return PreparedFrame(
            symbol=symbol,
            arrays=self.indicators.prepare_arrays(df),
            closes=df["close"].to_numpy(dtype=float),
            timestamps=(
                df.index.to_pydatetime()
                if isinstance(df.index, pd.DatetimeIndex)
                else None
            ),
            regimes=regimes,
            confidences=confidences,
        )

    def build_at(self, prepared: "PreparedFrame", i: int) -> MarketState:
        """MarketState for bar `i`, without recomputing anything.

        Omits support/resistance and candles, which build() populates but no
        strategy reads — every strategy uses only price, indicators and regime.
        """
        ts = prepared.timestamps[i] if prepared.timestamps is not None else datetime.utcnow()
        return MarketState(
            symbol=prepared.symbol,
            timestamp=ts,
            price=float(prepared.closes[i]),
            regime=prepared.regimes[i],
            regime_confidence=float(prepared.confidences[i]),
            indicators=self.indicators.get_values_at(prepared.arrays, i),
        )

    def build(
        self,
        symbol: str,
        df: pd.DataFrame,
        multi_tf: Optional[dict[str, pd.DataFrame]] = None,
        candles: Optional[list[Candle]] = None,
    ) -> MarketState:
        if df.empty:
            return MarketState(
                symbol=symbol,
                timestamp=datetime.utcnow(),
                price=0.0,
                regime=Regime.UNKNOWN,
            )

        regime, confidence = self.regime_detector.detect(df)
        indicator_values = self.indicators.get_latest_values(df)
        support, resistance = self.indicators.support_resistance(df)

        mtf_context = {}
        if multi_tf:
            for tf, tf_df in multi_tf.items():
                if not tf_df.empty:
                    mtf_context[tf] = self.indicators.get_latest_values(tf_df)

        price = float(df["close"].iloc[-1])
        ts = df.index[-1]
        if not isinstance(ts, datetime):
            ts = datetime.utcnow()

        return MarketState(
            symbol=symbol,
            timestamp=ts,
            price=price,
            regime=regime,
            regime_confidence=confidence,
            indicators=indicator_values,
            multi_timeframe=mtf_context,
            support_levels=support,
            resistance_levels=resistance,
            candles=candles or [],
        )
