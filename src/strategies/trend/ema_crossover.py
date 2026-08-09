"""EMA crossover trend-following strategy."""

from __future__ import annotations

from typing import Optional

from src.core.models import Direction, MarketState, Regime, Signal
from src.strategies.base import BaseStrategy


class EMACrossoverStrategy(BaseStrategy):
    name = "ema_crossover"

    def get_preferred_regimes(self) -> list[Regime]:
        return [Regime.TRENDING]

    def analyze(self, market_state: MarketState) -> Optional[Signal]:
        fast = self.params.get("fast_period", 12)
        slow = self.params.get("slow_period", 26)
        trend_period = self.params.get("trend_period", 200)
        adx_threshold = self.params.get("adx_threshold", 25)

        ema_fast = self._get_indicator(market_state, f"ema_{fast}")
        ema_slow = self._get_indicator(market_state, f"ema_{slow}")
        ema_trend = self._get_indicator(market_state, f"ema_{trend_period}")
        adx = self._get_indicator(market_state, "ADX_14", "adx_14")

        if not all([ema_fast, ema_slow, ema_trend]):
            return None

        if adx < adx_threshold:
            return None

        price = market_state.price
        vol_ratio = self._get_indicator(market_state, "volume_ratio", default=1.0)
        if vol_ratio < 0.8:
            return None

        if ema_fast > ema_slow and price > ema_trend:
            return self._make_signal(market_state, Direction.LONG)
        if ema_fast < ema_slow and price < ema_trend:
            return self._make_signal(market_state, Direction.SHORT)

        return None
