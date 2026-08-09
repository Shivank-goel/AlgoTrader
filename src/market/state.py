"""Market state snapshot builder."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from src.core.models import Candle, MarketState, Regime
from src.data.indicators import IndicatorEngine
from src.market.regime import RegimeDetector


class MarketStateBuilder:
    """Builds a frozen MarketState from current data."""

    def __init__(
        self,
        indicator_engine: Optional[IndicatorEngine] = None,
        regime_detector: Optional[RegimeDetector] = None,
    ) -> None:
        self.indicators = indicator_engine or IndicatorEngine()
        self.regime_detector = regime_detector or RegimeDetector(self.indicators)

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
