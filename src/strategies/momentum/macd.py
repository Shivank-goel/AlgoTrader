"""MACD momentum strategy."""

from __future__ import annotations

from typing import Optional

from src.core.models import Direction, MarketState, Regime, Signal
from src.strategies.base import BaseStrategy


class MACDStrategy(BaseStrategy):
    name = "macd"

    def get_preferred_regimes(self) -> list[Regime]:
        return [Regime.TRENDING]

    def analyze(self, market_state: MarketState) -> Optional[Signal]:
        trend_period = self.params.get("trend_period", 50)

        macd = self._get_indicator(market_state, "MACD_12_26_9")
        signal = self._get_indicator(market_state, "MACDs_12_26_9")
        histogram = self._get_indicator(market_state, "MACDh_12_26_9")
        ema_trend = self._get_indicator(market_state, f"ema_{trend_period}")

        if macd == 0 and signal == 0:
            return None

        price = market_state.price

        if macd > signal and macd > 0 and histogram > 0:
            if ema_trend and price < ema_trend:
                return None
            return self._make_signal(market_state, Direction.LONG)

        if macd < signal and macd < 0 and histogram < 0:
            if ema_trend and price > ema_trend:
                return None
            return self._make_signal(market_state, Direction.SHORT)

        return None
