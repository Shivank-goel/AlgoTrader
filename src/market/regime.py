"""Market regime detector using ADX, BBW, ATR, and volume."""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Optional

import pandas as pd

from src.core.models import Regime
from src.data.indicators import IndicatorEngine

logger = logging.getLogger(__name__)

REGIME_WEIGHTS = {
    "adx": 0.30,
    "bbw": 0.25,
    "atr": 0.25,
    "volume": 0.20,
}

HYSTERESIS_BARS = 3


class RegimeDetector:
    """Classifies market into trending, ranging, volatile, or quiet regimes."""

    def __init__(
        self,
        indicator_engine: Optional[IndicatorEngine] = None,
        hysteresis_bars: int = HYSTERESIS_BARS,
    ) -> None:
        self.indicators = indicator_engine or IndicatorEngine()
        self.hysteresis_bars = hysteresis_bars
        self._current_regime: Regime = Regime.UNKNOWN
        self._regime_confidence: float = 0.0
        self._candidate_regime: Optional[Regime] = None
        self._candidate_count: int = 0
        self._history: deque[tuple[datetime, Regime, float]] = deque(maxlen=500)

    @property
    def current_regime(self) -> Regime:
        return self._current_regime

    @property
    def confidence(self) -> float:
        return self._regime_confidence

    def detect(self, df: pd.DataFrame) -> tuple[Regime, float]:
        if df.empty or len(df) < 50:
            return Regime.UNKNOWN, 0.0

        if "ADX_14" not in df.columns and "adx" not in df.columns:
            df = self.indicators.compute_all(df)

        scores = {
            Regime.TRENDING: 0.0,
            Regime.RANGING: 0.0,
            Regime.VOLATILE: 0.0,
            Regime.QUIET: 0.0,
        }

        adx = self._get_col(df, "ADX_14", "adx_14", default=20.0)
        bbw_pct = self.indicators.bbw_percentile(df)
        atr_pct = self.indicators.atr_pct_of_price(df)
        atr_avg = self._atr_average(df)
        vol_ratio = self._get_col(df, "volume_ratio", default=1.0)

        # ADX scoring
        if adx > 25:
            scores[Regime.TRENDING] += REGIME_WEIGHTS["adx"]
        elif adx < 15:
            scores[Regime.QUIET] += REGIME_WEIGHTS["adx"] * 0.5
            scores[Regime.RANGING] += REGIME_WEIGHTS["adx"] * 0.5
        else:
            scores[Regime.RANGING] += REGIME_WEIGHTS["adx"]

        # BBW percentile scoring
        if bbw_pct > 80:
            scores[Regime.VOLATILE] += REGIME_WEIGHTS["bbw"]
        elif bbw_pct < 20:
            scores[Regime.QUIET] += REGIME_WEIGHTS["bbw"]
        elif bbw_pct > 60:
            scores[Regime.TRENDING] += REGIME_WEIGHTS["bbw"] * 0.7
            scores[Regime.VOLATILE] += REGIME_WEIGHTS["bbw"] * 0.3
        else:
            scores[Regime.RANGING] += REGIME_WEIGHTS["bbw"]

        # ATR scoring
        if atr_avg > 0 and atr_pct > atr_avg * 2:
            scores[Regime.VOLATILE] += REGIME_WEIGHTS["atr"]
        elif atr_pct < atr_avg * 0.5:
            scores[Regime.QUIET] += REGIME_WEIGHTS["atr"]
        else:
            scores[Regime.TRENDING] += REGIME_WEIGHTS["atr"] * 0.5
            scores[Regime.RANGING] += REGIME_WEIGHTS["atr"] * 0.5

        # Volume scoring
        if vol_ratio > 1.5:
            scores[Regime.VOLATILE] += REGIME_WEIGHTS["volume"] * 0.6
            scores[Regime.TRENDING] += REGIME_WEIGHTS["volume"] * 0.4
        elif vol_ratio < 0.5:
            scores[Regime.QUIET] += REGIME_WEIGHTS["volume"]
        elif vol_ratio < 0.8:
            scores[Regime.RANGING] += REGIME_WEIGHTS["volume"]
        else:
            scores[Regime.TRENDING] += REGIME_WEIGHTS["volume"]

        detected = max(scores, key=scores.get)
        total = sum(scores.values()) or 1.0
        confidence = scores[detected] / total

        final_regime = self._apply_hysteresis(detected)
        self._regime_confidence = confidence
        self._history.append((datetime.utcnow(), final_regime, confidence))

        return final_regime, confidence

    def _apply_hysteresis(self, detected: Regime) -> Regime:
        if detected == self._current_regime:
            self._candidate_regime = None
            self._candidate_count = 0
            return self._current_regime

        if detected == self._candidate_regime:
            self._candidate_count += 1
        else:
            self._candidate_regime = detected
            self._candidate_count = 1

        if self._candidate_count >= self.hysteresis_bars:
            old = self._current_regime
            self._current_regime = detected
            self._candidate_regime = None
            self._candidate_count = 0
            if old != detected:
                logger.info("Regime change: %s -> %s", old.value, detected.value)
            return detected

        return self._current_regime if self._current_regime != Regime.UNKNOWN else detected

    def get_history(self, limit: int = 50) -> list[tuple[datetime, Regime, float]]:
        return list(self._history)[-limit:]

    @staticmethod
    def _get_col(df: pd.DataFrame, *names: str, default: float = 0.0) -> float:
        for name in names:
            if name in df.columns:
                val = df[name].iloc[-1]
                if pd.notna(val):
                    return float(val)
        return default

    @staticmethod
    def _atr_average(df: pd.DataFrame, lookback: int = 50) -> float:
        if "atr_14" not in df.columns:
            return 0.0
        recent = df["atr_14"].dropna().tail(lookback)
        if recent.empty:
            return 0.0
        price = df["close"].iloc[-1]
        if price == 0:
            return 0.0
        return float(recent.mean() / price * 100)
