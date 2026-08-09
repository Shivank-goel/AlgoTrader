"""Meta-strategy: selects and ranks strategies by regime and performance."""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.core.models import MarketState, Regime, Signal
from src.strategies.base import BaseStrategy
from src.strategies.breakout.volume_breakout import VolumeBreakoutStrategy
from src.strategies.mean_reversion.bollinger import BollingerStrategy
from src.strategies.mean_reversion.rsi_reversion import RSIReversionStrategy
from src.strategies.momentum.macd import MACDStrategy
from src.strategies.trend.donchian import DonchianStrategy
from src.strategies.trend.ema_crossover import EMACrossoverStrategy
from src.strategies.trend.supertrend import SupertrendStrategy

logger = logging.getLogger(__name__)

STRATEGY_CLASSES: dict[str, type[BaseStrategy]] = {
    "ema_crossover": EMACrossoverStrategy,
    "supertrend": SupertrendStrategy,
    "donchian": DonchianStrategy,
    "rsi_reversion": RSIReversionStrategy,
    "bollinger": BollingerStrategy,
    "macd": MACDStrategy,
    "volume_breakout": VolumeBreakoutStrategy,
}


class StrategySelector:
    """Picks the best strategy for the current regime and resolves conflicts."""

    def __init__(
        self,
        config: dict[str, Any],
        performance_scores: Optional[dict[str, float]] = None,
    ) -> None:
        self.config = config
        self.performance_scores = performance_scores or {}
        self._strategies: dict[str, BaseStrategy] = {}
        self._init_strategies()

    def _init_strategies(self) -> None:
        enabled = self.config.get("enabled_strategies", list(STRATEGY_CLASSES.keys()))
        params = self.config.get("parameters", {})

        for name in enabled:
            if name in STRATEGY_CLASSES:
                strategy_params = params.get(name, {}).get("default", {})
                self._strategies[name] = STRATEGY_CLASSES[name](strategy_params)

    def update_params_for_regime(self, regime: Regime) -> None:
        params = self.config.get("parameters", {})
        regime_key = regime.value
        for name, strategy in self._strategies.items():
            strategy_params = params.get(name, {})
            default_params = strategy_params.get("default", {})
            regime_params = strategy_params.get(regime_key, {})
            strategy.params = {**default_params, **regime_params}

    def get_eligible_strategies(self, regime: Regime) -> list[BaseStrategy]:
        preferences = self.config.get("regime_preferences", {})
        preferred_names = preferences.get(regime.value, [])
        eligible = []
        for name in preferred_names:
            if name in self._strategies:
                eligible.append(self._strategies[name])
        if not eligible:
            eligible = [
                s for s in self._strategies.values() if regime in s.get_preferred_regimes()
            ]
        return eligible

    def select_signal(self, market_state: MarketState) -> Optional[Signal]:
        self.update_params_for_regime(market_state.regime)
        eligible = self.get_eligible_strategies(market_state.regime)

        signals: list[Signal] = []
        for strategy in eligible:
            try:
                signal = strategy.analyze(market_state)
                if signal:
                    perf_boost = self.performance_scores.get(strategy.name, 0.5)
                    signal.confidence = min(1.0, signal.confidence * 0.7 + perf_boost * 0.3)
                    signals.append(signal)
            except Exception:
                logger.exception("Strategy %s failed", strategy.name)

        if not signals:
            return None

        min_confidence = self.config.get("selector", {}).get("min_confidence", 0.5)
        signals = [s for s in signals if s.confidence >= min_confidence]
        if not signals:
            return None

        signals.sort(
            key=lambda s: (
                s.confidence,
                self.performance_scores.get(s.strategy_name, 0),
            ),
            reverse=True,
        )

        best = signals[0]
        if len(signals) > 1:
            conflicts = [s for s in signals[1:] if s.direction != best.direction]
            if conflicts:
                logger.debug(
                    "Resolved conflict: %s (conf=%.2f) over %d alternatives",
                    best.strategy_name,
                    best.confidence,
                    len(signals) - 1,
                )

        return best

    def get_all_strategies(self) -> dict[str, BaseStrategy]:
        return self._strategies

    def update_performance_scores(self, scores: dict[str, float]) -> None:
        self.performance_scores = scores
