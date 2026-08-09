"""Donchian channel breakout strategy."""

from __future__ import annotations

from typing import Optional

from src.core.models import Direction, MarketState, Regime, Signal
from src.strategies.base import BaseStrategy


class DonchianStrategy(BaseStrategy):
    name = "donchian"

    def get_preferred_regimes(self) -> list[Regime]:
        return [Regime.TRENDING, Regime.QUIET]

    def analyze(self, market_state: MarketState) -> Optional[Signal]:
        entry_period = self.params.get("entry_period", 20)
        vol_mult = self.params.get("volume_multiplier", 1.5)

        upper = self._get_indicator(
            market_state, f"DCHU_{entry_period}_{entry_period}", f"DCHU_{entry_period}"
        )
        lower = self._get_indicator(
            market_state, f"DCHL_{entry_period}_{entry_period}", f"DCHL_{entry_period}"
        )
        vol_ratio = self._get_indicator(market_state, "volume_ratio", default=1.0)

        if not upper or not lower:
            return None

        price = market_state.price

        if price >= upper and vol_ratio >= vol_mult:
            return self._make_signal(market_state, Direction.LONG)

        if price <= lower and vol_ratio >= vol_mult:
            return self._make_signal(market_state, Direction.SHORT)

        return None
