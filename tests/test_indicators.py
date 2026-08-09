"""Tests for the indicator engine."""

import numpy as np
import pandas as pd
import pytest

from src.data.indicators import IndicatorEngine


@pytest.fixture
def sample_ohlcv():
    np.random.seed(42)
    n = 200
    close = 50000 + np.cumsum(np.random.randn(n) * 100)
    df = pd.DataFrame({
        "open": close + np.random.randn(n) * 50,
        "high": close + abs(np.random.randn(n) * 100),
        "low": close - abs(np.random.randn(n) * 100),
        "close": close,
        "volume": np.random.randint(100, 10000, n).astype(float),
    })
    return df


@pytest.fixture
def engine():
    return IndicatorEngine()


def test_compute_all_adds_indicators(engine, sample_ohlcv):
    result = engine.compute_all(sample_ohlcv)
    assert "ema_9" in result.columns
    assert "ema_21" in result.columns
    assert "rsi_14" in result.columns
    assert "atr_14" in result.columns


def test_get_latest_values(engine, sample_ohlcv):
    result = engine.compute_all(sample_ohlcv)
    values = engine.get_latest_values(result)
    assert "close" in values
    assert "rsi_14" in values
    assert isinstance(values["close"], float)


def test_bbw_percentile(engine, sample_ohlcv):
    result = engine.compute_all(sample_ohlcv)
    pct = engine.bbw_percentile(result)
    assert 0 <= pct <= 100


def test_atr_pct_of_price(engine, sample_ohlcv):
    result = engine.compute_all(sample_ohlcv)
    pct = engine.atr_pct_of_price(result)
    assert pct >= 0


def test_support_resistance(engine, sample_ohlcv):
    support, resistance = engine.support_resistance(sample_ohlcv)
    assert len(support) <= 3
    assert len(resistance) <= 3
    if support and resistance:
        assert max(support) <= min(resistance)
