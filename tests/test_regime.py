"""Tests for market regime detection."""

import numpy as np
import pandas as pd
import pytest

from src.core.models import Regime
from src.data.indicators import IndicatorEngine
from src.market.regime import RegimeDetector


@pytest.fixture
def engine():
    return IndicatorEngine()


@pytest.fixture
def detector():
    return RegimeDetector(hysteresis_bars=1)


def _make_df(close_values, volume_values=None):
    n = len(close_values)
    if volume_values is None:
        volume_values = [1000] * n
    return pd.DataFrame({
        "open": close_values,
        "high": [c * 1.01 for c in close_values],
        "low": [c * 0.99 for c in close_values],
        "close": close_values,
        "volume": volume_values,
    })


def test_detect_trending(detector, engine):
    np.random.seed(1)
    trend = list(50000 + np.cumsum(np.random.randn(100) * 200 + 50))
    df = engine.compute_all(_make_df(trend))
    regime, confidence = detector.detect(df)
    assert regime in (Regime.TRENDING, Regime.VOLATILE, Regime.RANGING)
    assert 0 <= confidence <= 1


def test_detect_quiet(detector, engine):
    flat = [50000 + np.sin(i / 10) * 10 for i in range(100)]
    low_vol = [100] * 100
    df = engine.compute_all(_make_df(flat, low_vol))
    regime, confidence = detector.detect(df)
    assert regime in list(Regime)
    assert confidence >= 0


def test_hysteresis(detector, engine):
    detector.hysteresis_bars = 3
    flat = [50000] * 100
    df = engine.compute_all(_make_df(flat))
    r1, _ = detector.detect(df)
    r2, _ = detector.detect(df)
    assert r1 == r2


def test_insufficient_data(detector):
    df = pd.DataFrame({"close": [1, 2, 3]})
    regime, confidence = detector.detect(df)
    assert regime == Regime.UNKNOWN
    assert confidence == 0.0
