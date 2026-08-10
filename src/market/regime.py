"""Market regime detector using ADX, BBW, ATR, and volume."""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Optional

import numpy as np
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

    def detect_series(self, df: pd.DataFrame) -> tuple[list[Regime], np.ndarray]:
        """Vectorised regime classification for every bar of `df`.

        Equivalent to calling detect() on each expanding prefix, but without
        the O(n^2) blowup: detect() recomputed `bbw.dropna().tail(100)` and
        `atr_14.dropna().tail(50)` over the whole prefix on every bar, which is
        what made a 3000-bar backtest take ~8 seconds.

        Hysteresis is inherently sequential, so it is replayed in a single pass
        rather than vectorised. Detector state is left as of the final bar,
        matching what a run of detect() calls would leave behind.
        """
        n = len(df)
        if n == 0:
            return [], np.zeros(0)

        if "ADX_14" not in df.columns and "adx" not in df.columns:
            df = self.indicators.compute_all(df)

        adx = self._column(df, "ADX_14", "adx_14", default=20.0)
        vol_ratio = self._column(df, "volume_ratio", default=1.0)
        close = df["close"].to_numpy(dtype=float)

        # bbw percentile within a trailing 100-bar window. detect() computes
        # (recent < current).sum() / len(recent) over bbw.dropna().tail(100),
        # so the denominator is the count of non-NaN values in the window, not
        # the window width — they differ during indicator warm-up.
        # rank(method="min") gives 1 + count(strictly less).
        if "bbw" in df.columns:
            bbw = df["bbw"]
            rank_min = bbw.rolling(100, min_periods=1).rank(method="min")
            counts = bbw.rolling(100, min_periods=1).count()
            with np.errstate(invalid="ignore", divide="ignore"):
                bbw_pct = np.array(
                    ((rank_min - 1.0) / counts * 100.0).to_numpy(dtype=float),
                    copy=True,
                )
            # detect() returns 50.0 outright when the prefix is shorter than
            # the lookback.
            if n:
                bbw_pct[: min(99, n)] = 50.0
        else:
            bbw_pct = np.full(n, 50.0)
        bbw_pct = np.where(np.isnan(bbw_pct), 50.0, bbw_pct)

        if "atr_14" in df.columns:
            atr = df["atr_14"].to_numpy(dtype=float)
            with np.errstate(invalid="ignore", divide="ignore"):
                atr_pct = np.where(close != 0, atr / close * 100.0, 0.0)
            # Trailing mean over available (non-NaN) values, as _atr_average does.
            atr_avg_abs = (
                df["atr_14"].rolling(50, min_periods=1).mean().to_numpy(dtype=float)
            )
            with np.errstate(invalid="ignore", divide="ignore"):
                atr_avg = np.where(close != 0, atr_avg_abs / close * 100.0, 0.0)
        else:
            atr_pct = np.zeros(n)
            atr_avg = np.zeros(n)
        atr_pct = np.nan_to_num(atr_pct)
        atr_avg = np.nan_to_num(atr_avg)

        w = REGIME_WEIGHTS
        trending = np.zeros(n)
        ranging = np.zeros(n)
        volatile = np.zeros(n)
        quiet = np.zeros(n)

        # ADX
        trending += np.where(adx > 25, w["adx"], 0.0)
        quiet += np.where(adx < 15, w["adx"] * 0.5, 0.0)
        ranging += np.where(adx < 15, w["adx"] * 0.5, 0.0)
        ranging += np.where((adx <= 25) & (adx >= 15), w["adx"], 0.0)

        # Bollinger band width percentile
        volatile += np.where(bbw_pct > 80, w["bbw"], 0.0)
        quiet += np.where(bbw_pct < 20, w["bbw"], 0.0)
        mid_high = (bbw_pct <= 80) & (bbw_pct > 60)
        trending += np.where(mid_high, w["bbw"] * 0.7, 0.0)
        volatile += np.where(mid_high, w["bbw"] * 0.3, 0.0)
        ranging += np.where((bbw_pct <= 60) & (bbw_pct >= 20), w["bbw"], 0.0)

        # ATR relative to its own trailing average
        hot = (atr_avg > 0) & (atr_pct > atr_avg * 2)
        cold = ~hot & (atr_pct < atr_avg * 0.5)
        normal = ~hot & ~cold
        volatile += np.where(hot, w["atr"], 0.0)
        quiet += np.where(cold, w["atr"], 0.0)
        trending += np.where(normal, w["atr"] * 0.5, 0.0)
        ranging += np.where(normal, w["atr"] * 0.5, 0.0)

        # Volume
        heavy = vol_ratio > 1.5
        light = ~heavy & (vol_ratio < 0.5)
        soft = ~heavy & ~light & (vol_ratio < 0.8)
        normal_vol = ~heavy & ~light & ~soft
        volatile += np.where(heavy, w["volume"] * 0.6, 0.0)
        trending += np.where(heavy, w["volume"] * 0.4, 0.0)
        quiet += np.where(light, w["volume"], 0.0)
        ranging += np.where(soft, w["volume"], 0.0)
        trending += np.where(normal_vol, w["volume"], 0.0)

        stacked = np.vstack([trending, ranging, volatile, quiet])
        order = (Regime.TRENDING, Regime.RANGING, Regime.VOLATILE, Regime.QUIET)
        # argmax ties break toward the first index, matching max(scores, key=...)
        # over a dict built in this same order.
        best = stacked.argmax(axis=0)
        totals = stacked.sum(axis=0)
        totals = np.where(totals == 0, 1.0, totals)
        confidences = stacked[best, np.arange(n)] / totals

        # Bars that detect() would have refused to classify.
        too_short = min(50, n)

        regimes: list[Regime] = []
        for i in range(n):
            if i < too_short - 1:
                regimes.append(Regime.UNKNOWN)
                continue
            regimes.append(self._apply_hysteresis(order[best[i]]))

        if n:
            self._regime_confidence = float(confidences[-1])

        return regimes, confidences

    @staticmethod
    def _column(
        df: pd.DataFrame, *names: str, default: float
    ) -> np.ndarray:
        for name in names:
            if name in df.columns:
                return np.nan_to_num(
                    df[name].to_numpy(dtype=float), nan=default
                )
        return np.full(len(df), default)

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
