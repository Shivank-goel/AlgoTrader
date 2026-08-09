"""Tests for trading strategies."""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime

from src.core.models import Direction, MarketState, Regime
from src.strategies.trend.ema_crossover import EMACrossoverStrategy
from src.strategies.mean_reversion.rsi_reversion import RSIReversionStrategy
from src.strategies.selector import StrategySelector


@pytest.fixture
def trending_market_state():
    return MarketState(
        symbol="BTCUSDT",
        timestamp=datetime.utcnow(),
        price=50000.0,
        regime=Regime.TRENDING,
        regime_confidence=0.8,
        indicators={
            "ema_12": 50100,
            "ema_26": 49900,
            "ema_200": 48000,
            "ADX_14": 30,
            "volume_ratio": 1.2,
            "atr_14": 500,
        },
    )


@pytest.fixture
def ranging_market_state():
    return MarketState(
        symbol="BTCUSDT",
        timestamp=datetime.utcnow(),
        price=50000.0,
        regime=Regime.RANGING,
        regime_confidence=0.7,
        indicators={
            "rsi_14": 25,
            "ADX_14": 15,
            "atr_14": 300,
        },
    )


def test_ema_crossover_long_signal(trending_market_state):
    strategy = EMACrossoverStrategy({"fast_period": 12, "slow_period": 26})
    signal = strategy.analyze(trending_market_state)
    assert signal is not None
    assert signal.direction == Direction.LONG
    assert signal.stop_loss is not None
    assert signal.take_profit is not None


def test_ema_crossover_no_signal_low_adx(trending_market_state):
    trending_market_state.indicators["ADX_14"] = 15
    strategy = EMACrossoverStrategy()
    signal = strategy.analyze(trending_market_state)
    assert signal is None


def test_rsi_reversion_oversold(ranging_market_state):
    strategy = RSIReversionStrategy()
    signal = strategy.analyze(ranging_market_state)
    assert signal is not None
    assert signal.direction == Direction.LONG


def test_rsi_reversion_no_signal_trending(ranging_market_state):
    ranging_market_state.indicators["ADX_14"] = 30
    strategy = RSIReversionStrategy()
    signal = strategy.analyze(ranging_market_state)
    assert signal is None


def test_strategy_selector(trending_market_state):
    config = {
        "enabled_strategies": ["ema_crossover", "rsi_reversion"],
        "regime_preferences": {
            "trending": ["ema_crossover"],
            "ranging": ["rsi_reversion"],
        },
        "parameters": {},
        "selector": {"min_confidence": 0.3},
    }
    selector = StrategySelector(config)
    signal = selector.select_signal(trending_market_state)
    assert signal is not None
    assert signal.strategy_name == "ema_crossover"
