"""Regime-adaptive parameter tuning with smooth transitions."""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.core.models import Regime

logger = logging.getLogger(__name__)


class ParameterAdapter:
    """Loads and blends regime-specific strategy parameters."""

    def __init__(self, strategy_config: dict[str, Any]) -> None:
        self.config = strategy_config
        self._current_regime: Optional[Regime] = None
        self._active_params: dict[str, dict[str, Any]] = {}
        self._transition_blend: float = 1.0

    def on_regime_change(
        self, new_regime: Regime, old_regime: Optional[Regime] = None
    ) -> dict[str, dict[str, Any]]:
        self._current_regime = new_regime
        params = self.config.get("parameters", {})
        updated = {}

        for strategy_name, strategy_params in params.items():
            regime_params = strategy_params.get(new_regime.value, {})
            default_params = strategy_params.get("default", {})

            if old_regime and self._transition_blend < 1.0:
                old_params = strategy_params.get(old_regime.value, default_params)
                blended = self._blend_params(old_params, regime_params, self._transition_blend)
            else:
                blended = {**default_params, **regime_params}

            updated[strategy_name] = blended
            self._active_params[strategy_name] = blended

        logger.info("Parameters adapted for regime: %s", new_regime.value)
        return updated

    def get_params(self, strategy_name: str) -> dict[str, Any]:
        return self._active_params.get(
            strategy_name,
            self.config.get("parameters", {}).get(strategy_name, {}).get("default", {}),
        )

    def update_from_optimizer(self, strategy_name: str, params: dict[str, Any]) -> None:
        if strategy_name not in self.config.get("parameters", {}):
            self.config.setdefault("parameters", {})[strategy_name] = {}
        regime_key = self._current_regime.value if self._current_regime else "default"
        self.config["parameters"][strategy_name][regime_key] = params
        self._active_params[strategy_name] = params

    def set_transition_blend(self, blend: float) -> None:
        self._transition_blend = max(0.0, min(1.0, blend))

    @staticmethod
    def _blend_params(
        old: dict[str, Any], new: dict[str, Any], blend: float
    ) -> dict[str, Any]:
        result = {**old}
        for key, val in new.items():
            if key in old and isinstance(val, (int, float)) and isinstance(old[key], (int, float)):
                result[key] = old[key] * (1 - blend) + val * blend
            else:
                result[key] = val
        return result
