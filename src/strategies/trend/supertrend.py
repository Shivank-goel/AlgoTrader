"""Supertrend trend-following strategy."""

from __future__ import annotations

from typing import Optional

from src.core.models import Direction, MarketState, Regime, Signal
from src.strategies.base import BaseStrategy


class SupertrendStrategy(BaseStrategy):
    name = "supertrend"

    def get_preferred_regimes(self) -> list[Regime]:
        return [Regime.TRENDING, Regime.VOLATILE]

    def analyze(self, market_state: MarketState) -> Optional[Signal]:
        trend_period = self.params.get("trend_period", 200)
        rsi_lower = self.params.get("rsi_lower", 30)
        rsi_upper = self.params.get("rsi_upper", 70)

        st_dir = self._get_indicator(market_state, "SUPERTd_10_3.0", "SUPERTd_10_3")
        st_value = self._get_indicator(market_state, "SUPERT_10_3.0", "SUPERT_10_3")
        ema_trend = self._get_indicator(market_state, f"ema_{trend_period}")
        rsi = self._get_indicator(market_state, "rsi_14", default=50)

        if st_dir == 0 and st_value == 0:
            return None

        price = market_state.price

        if st_dir > 0 and price > st_value:
            if rsi < rsi_lower:
                return None
            if ema_trend and price < ema_trend:
                return None
            return self._make_signal(market_state, Direction.LONG)

        if st_dir < 0 and price < st_value:
            if rsi > rsi_upper:
                return None
            if ema_trend and price > ema_trend:
                return None
            return self._make_signal(market_state, Direction.SHORT)

        return None
