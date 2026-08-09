"""Bollinger Band mean-reversion strategy."""

from __future__ import annotations

from typing import Optional

from src.core.models import Direction, MarketState, Regime, Signal
from src.strategies.base import BaseStrategy


class BollingerStrategy(BaseStrategy):
    name = "bollinger"

    def get_preferred_regimes(self) -> list[Regime]:
        return [Regime.RANGING, Regime.QUIET]

    def analyze(self, market_state: MarketState) -> Optional[Signal]:
        adx_max = self.params.get("adx_max", 20)
        adx = self._get_indicator(market_state, "ADX_14", "adx_14", default=25)
        bbw = self._get_indicator(market_state, "bbw", default=0.05)

        if adx > adx_max:
            return None

        bbl = self._get_indicator(market_state, "BBL_20_2.0", "BBL_20_2")
        bbu = self._get_indicator(market_state, "BBU_20_2.0", "BBU_20_2")
        bbm = self._get_indicator(market_state, "BBM_20_2.0", "BBM_20_2")
        rsi = self._get_indicator(market_state, "rsi_14", default=50)

        if not all([bbl, bbu, bbm]):
            return None

        price = market_state.price

        if price <= bbl and rsi < 40:
            return self._make_signal(market_state, Direction.LONG)

        if price >= bbu and rsi > 60:
            return self._make_signal(market_state, Direction.SHORT)

        return None
