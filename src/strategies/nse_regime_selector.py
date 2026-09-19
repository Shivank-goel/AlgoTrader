"""Lagged, long-only NSE strategy families and frozen regime selection."""

from __future__ import annotations

import math
from enum import Enum

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator


class NseRegime(str, Enum):
    TRENDING_UP = "trending_up"
    RANGING = "ranging"
    VOLATILE = "volatile"
    FALLING = "falling"
    UNKNOWN = "unknown"


class StrategyFamily(str, Enum):
    MOMENTUM = "momentum_6_12"
    BREAKOUT = "donchian_breakout"
    RESIDUAL_REVERSAL = "residual_reversal"


class FrozenFamilyEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    family: StrategyFamily
    lower_confidence_bound: float
    qualified: bool = False


class SelectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    min_regime_confidence: float = Field(default=.60, ge=0, le=1)
    top_n: int = Field(default=5, ge=1, le=5)
    capital_inr: float = Field(default=10000, gt=0)
    max_order_inr: float = Field(default=2000, gt=0)
    evidence: list[FrozenFamilyEvidence]

    @model_validator(mode="after")
    def unique_evidence(self) -> SelectorConfig:
        if len({row.family for row in self.evidence}) != len(self.evidence):
            raise ValueError("family evidence must be unique")
        return self


class RegimeAwareSelector:
    """Pure decisions: callers provide ordered daily closes through the decision date."""

    ELIGIBLE = {
        NseRegime.TRENDING_UP: (StrategyFamily.MOMENTUM, StrategyFamily.BREAKOUT),
        NseRegime.RANGING: (StrategyFamily.RESIDUAL_REVERSAL,),
        NseRegime.VOLATILE: (), NseRegime.FALLING: (), NseRegime.UNKNOWN: (),
    }

    def __init__(self, config: SelectorConfig) -> None:
        self.config = config

    @staticmethod
    def _valid(closes: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(closes.index, pd.DatetimeIndex) or not closes.index.is_monotonic_increasing:
            raise ValueError("daily closes require an ordered DatetimeIndex")
        if closes.index.has_duplicates or closes.columns.has_duplicates:
            raise ValueError("daily close labels must be unique")
        clean = closes.astype(float).replace([np.inf, -np.inf], np.nan)
        if (clean.dropna(how="all") <= 0).any().any():
            raise ValueError("daily closes must be positive")
        return clean

    def regime(self, closes: pd.DataFrame) -> tuple[NseRegime, float, dict]:
        closes = self._valid(closes)
        if len(closes) < 253 or closes.iloc[-1].notna().sum() < 10:
            return NseRegime.UNKNOWN, 0.0, {"reason": "insufficient_history"}
        returns = closes.pct_change(fill_method=None)
        benchmark = returns.median(axis=1, skipna=True).fillna(0).add(1).cumprod()
        level, ma200 = benchmark.iloc[-1], benchmark.iloc[-200:].mean()
        ret60 = level / benchmark.iloc[-61] - 1
        vol20 = returns.median(axis=1, skipna=True).iloc[-20:].std() * math.sqrt(252)
        history_vol = returns.median(axis=1, skipna=True).rolling(20).std().dropna() * math.sqrt(252)
        vol_threshold = history_vol.iloc[-252:].quantile(.80)
        breadth = (closes.iloc[-1] > closes.iloc[-100:].mean()).mean()
        details = {"return_60d": float(ret60), "volatility_20d": float(vol20),
                   "volatility_80pct": float(vol_threshold), "breadth_above_100d": float(breadth),
                   "above_200d": bool(level > ma200)}
        if vol20 > vol_threshold:
            regime = NseRegime.VOLATILE
            confidence = min(1.0, .5 + (vol20 / max(vol_threshold, 1e-9) - 1))
        elif level < ma200 and ret60 < 0:
            regime = NseRegime.FALLING
            confidence = min(1.0, .5 + abs(ret60) * 5)
        elif level > ma200 and ret60 > 0 and breadth >= .60:
            regime = NseRegime.TRENDING_UP
            confidence = min(1.0, .5 + ret60 * 3 + max(0, breadth - .5))
        else:
            regime = NseRegime.RANGING
            confidence = min(1.0, .5 + max(0, .60 - abs(breadth - .5)))
        return regime, float(confidence), details

    @staticmethod
    def scores(family: StrategyFamily, closes: pd.DataFrame) -> pd.Series:
        closes = RegimeAwareSelector._valid(closes)
        latest = closes.iloc[-1]
        if family is StrategyFamily.MOMENTUM:
            daily_vol = closes.pct_change(fill_method=None).iloc[-252:].std() * math.sqrt(252)
            score = ((latest / closes.iloc[-127] - 1) + (latest / closes.iloc[-253] - 1)) / daily_vol
        elif family is StrategyFamily.BREAKOUT:
            prior_high = closes.iloc[-56:-1].max()
            trend = latest > closes.iloc[-200:].mean()
            score = (latest / prior_high - 1).where(trend)
        else:
            five_day = latest / closes.iloc[-6] - 1
            score = -(five_day - five_day.median())
        return score.replace([np.inf, -np.inf], np.nan).dropna().sort_values(ascending=False)

    def select(self, closes: pd.DataFrame) -> dict:
        regime, confidence, details = self.regime(closes)
        eligible = list(self.ELIGIBLE[regime]) if confidence >= self.config.min_regime_confidence else []
        evidence = {row.family: row for row in self.config.evidence}
        ranked = sorted((evidence[family] for family in eligible
                         if family in evidence and evidence[family].qualified),
                        key=lambda row: row.lower_confidence_bound, reverse=True)
        reason = None
        selected = None
        if not eligible:
            reason = "regime_not_tradeable_or_low_confidence"
        elif not ranked:
            reason = "no_eligible_family_is_qualified"
        elif len(ranked) > 1 and math.isclose(ranked[0].lower_confidence_bound,
                                              ranked[1].lower_confidence_bound):
            reason = "selector_evidence_tie"
        else:
            selected = ranked[0].family
        return {"regime": regime.value, "regime_confidence": confidence,
                "regime_details": details, "eligible_families": [row.value for row in eligible],
                "selected_family": selected.value if selected else None, "reason": reason,
                "live_enabled": False}

    def whole_share_targets(self, scores: pd.Series, prices: pd.Series) -> tuple[dict[str, int], list[dict]]:
        targets, rejected = {}, []
        for symbol in scores.index:
            if len(targets) >= self.config.top_n:
                break
            price = float(prices.get(symbol, np.nan))
            quantity = int(self.config.max_order_inr // price) if math.isfinite(price) and price > 0 else 0
            if quantity < 1:
                rejected.append({"symbol": symbol, "reason": "unaffordable"})
            else:
                targets[str(symbol)] = quantity
        return targets, rejected
