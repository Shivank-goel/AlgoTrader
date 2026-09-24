"""Deterministic, point-in-time entry protection plans for NSE families.

No defaults are inferred here. Callers must provide completed-bar structural
inputs and explicit configuration parameters; this keeps research and shadow
semantics identical and makes missing protection fail closed.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProtectionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    stop_loss_price: float = Field(gt=0)
    take_profit_price: float | None = Field(default=None, gt=0)
    protection_required: bool = True
    stop_method: str = Field(min_length=1)
    target_method: str | None = None
    calculation_inputs: dict[str, float | int | str] = {}
    strategy_version: str = "v1"
    time_exit_minutes: int | None = Field(default=None, gt=0)

    @classmethod
    def from_levels(cls, *, family: str, entry_reference_price: float,
                    stop_loss_price: float, take_profit_price: float | None = None,
                    strategy_version: str = "v1", inputs: dict[str, Any] | None = None,
                    stop_method: str = "configured", target_method: str | None = None,
                    protection_required: bool = True) -> ProtectionPlan:
        if not math.isfinite(entry_reference_price) or entry_reference_price <= 0:
            raise ValueError("entry reference price must be positive")
        if stop_loss_price >= entry_reference_price:
            raise ValueError(f"{family} stop must be below long entry reference")
        if take_profit_price is not None and take_profit_price <= entry_reference_price:
            raise ValueError(f"{family} target must be above long entry reference")
        values = {str(k): v for k, v in (inputs or {}).items()
                  if isinstance(v, str | int | float) and not isinstance(v, bool)}
        values["entry_reference_price"] = entry_reference_price
        return cls(stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
                   protection_required=protection_required, stop_method=stop_method,
                   target_method=target_method, calculation_inputs=values,
                   strategy_version=strategy_version)


def atr_protection(*, family: str, entry_reference_price: float, atr: float,
                   stop_atr_multiplier: float, reward_risk_multiple: float | None,
                   strategy_version: str = "v1") -> ProtectionPlan:
    """Volatility stop using ATR available at the completed signal bar."""
    if not math.isfinite(atr) or atr <= 0 or stop_atr_multiplier <= 0:
        raise ValueError("ATR protection inputs must be positive")
    stop = entry_reference_price - atr * stop_atr_multiplier
    target = (entry_reference_price + (entry_reference_price - stop) * reward_risk_multiple
              if reward_risk_multiple is not None else None)
    return ProtectionPlan.from_levels(
        family=family, entry_reference_price=entry_reference_price,
        stop_loss_price=stop, take_profit_price=target, strategy_version=strategy_version,
        inputs={"atr": atr, "stop_atr_multiplier": stop_atr_multiplier,
                **({"reward_risk_multiple": reward_risk_multiple} if reward_risk_multiple is not None else {})},
        stop_method="atr", target_method="risk_multiple" if target is not None else None,
    )


def structural_protection(*, family: str, entry_reference_price: float, invalidation_level: float,
                          target_level: float | None, strategy_version: str = "v1") -> ProtectionPlan:
    """Use a completed-bar structural invalidation/target level."""
    return ProtectionPlan.from_levels(
        family=family, entry_reference_price=entry_reference_price,
        stop_loss_price=invalidation_level, take_profit_price=target_level,
        strategy_version=strategy_version, inputs={"invalidation_level": invalidation_level,
                                                    **({"target_level": target_level} if target_level is not None else {})},
        stop_method="structural", target_method="structural" if target_level is not None else None,
    )
