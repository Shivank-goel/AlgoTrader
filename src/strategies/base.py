"""Abstract base strategy class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from src.core.models import Direction, MarketState, Regime, Signal, SignalAction


class BaseStrategy(ABC):
    """Base class for all trading strategies."""

    name: str = "base"

    def __init__(self, params: Optional[dict[str, Any]] = None) -> None:
        self.params = params or {}

    @abstractmethod
    def analyze(self, market_state: MarketState) -> Optional[Signal]:
        """Analyze market state and return a signal if conditions are met."""

    @abstractmethod
    def get_preferred_regimes(self) -> list[Regime]:
        """Return regimes where this strategy performs best."""

    def get_stop_loss(self, entry_price: float, side: Direction, atr: float) -> float:
        multiplier = self.params.get("atr_stop_multiplier", 2.0)
        if side == Direction.LONG:
            return entry_price - atr * multiplier
        return entry_price + atr * multiplier

    def get_take_profit(self, entry_price: float, side: Direction, atr: float) -> float:
        multiplier = self.params.get("atr_stop_multiplier", 2.0) * 2
        if side == Direction.LONG:
            return entry_price + atr * multiplier
        return entry_price - atr * multiplier

    def get_confidence(self, market_state: MarketState) -> float:
        if market_state.regime in self.get_preferred_regimes():
            return min(1.0, market_state.regime_confidence + 0.2)
        return max(0.0, market_state.regime_confidence - 0.3)

    def _get_indicator(self, state: MarketState, *keys: str, default: float = 0.0) -> float:
        for key in keys:
            if key in state.indicators:
                return float(state.indicators[key])
        return default

    def _make_signal(
        self,
        state: MarketState,
        direction: Direction,
        action: SignalAction = SignalAction.ENTER,
        confidence: Optional[float] = None,
    ) -> Signal:
        atr = self._get_indicator(state, "atr_14", default=state.price * 0.02)
        conf = confidence if confidence is not None else self.get_confidence(state)
        entry = state.price
        return Signal(
            symbol=state.symbol,
            direction=direction,
            action=action,
            strategy_name=self.name,
            confidence=conf,
            entry_price=entry,
            stop_loss=self.get_stop_loss(entry, direction, atr),
            take_profit=self.get_take_profit(entry, direction, atr),
            regime=state.regime,
        )
