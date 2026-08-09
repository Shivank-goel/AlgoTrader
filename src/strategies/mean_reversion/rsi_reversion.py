"""RSI mean-reversion strategy."""

from __future__ import annotations

from typing import Optional

from src.core.models import Direction, MarketState, Regime, Signal
from src.strategies.base import BaseStrategy


class RSIReversionStrategy(BaseStrategy):
    name = "rsi_reversion"

    def get_preferred_regimes(self) -> list[Regime]:
        return [Regime.RANGING]

    def analyze(self, market_state: MarketState) -> Optional[Signal]:
        rsi_period = self.params.get("rsi_period", 14)
        rsi_lower = self.params.get("rsi_lower", 30)
        rsi_upper = self.params.get("rsi_upper", 70)
        adx_max = self.params.get("adx_max", 20)

        rsi_key = f"rsi_{rsi_period}" if rsi_period != 14 else "rsi_14"
        rsi = self._get_indicator(market_state, rsi_key, "rsi_14")
        adx = self._get_indicator(market_state, "ADX_14", "adx_14", default=25)

        if adx > adx_max:
            return None

        if rsi <= rsi_lower:
            return self._make_signal(market_state, Direction.LONG)

        if rsi >= rsi_upper:
            return self._make_signal(market_state, Direction.SHORT)

        return None
