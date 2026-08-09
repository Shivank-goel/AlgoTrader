"""Volume-confirmed breakout strategy."""

from __future__ import annotations

from typing import Optional

from src.core.models import Direction, MarketState, Regime, Signal
from src.strategies.base import BaseStrategy


class VolumeBreakoutStrategy(BaseStrategy):
    name = "volume_breakout"

    def get_preferred_regimes(self) -> list[Regime]:
        return [Regime.QUIET, Regime.TRENDING]

    def analyze(self, market_state: MarketState) -> Optional[Signal]:
        vol_mult = self.params.get("volume_multiplier", 2.0)
        period = self.params.get("consolidation_period", 20)

        upper = self._get_indicator(
            market_state, f"DCHU_{period}_{period}", f"DCHU_{period}"
        )
        lower = self._get_indicator(
            market_state, f"DCHL_{period}_{period}", f"DCHL_{period}"
        )
        vol_ratio = self._get_indicator(market_state, "volume_ratio", default=1.0)
        bbw = self._get_indicator(market_state, "bbw", default=0.1)
        atr = self._get_indicator(market_state, "atr_14", default=0)

        if not upper or not lower:
            return None

        range_width = upper - lower
        price = market_state.price

        # Consolidation: narrow range relative to ATR
        if atr > 0 and range_width > atr * 3:
            return None

        if vol_ratio < vol_mult:
            return None

        if price > upper:
            signal = self._make_signal(market_state, Direction.LONG)
            if signal.stop_loss and lower:
                signal.stop_loss = lower
            if signal.take_profit:
                signal.take_profit = price + range_width
            return signal

        if price < lower:
            signal = self._make_signal(market_state, Direction.SHORT)
            if signal.stop_loss and upper:
                signal.stop_loss = upper
            if signal.take_profit:
                signal.take_profit = price - range_width
            return signal

        return None
