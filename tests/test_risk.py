"""Tests for risk management."""

import pytest

from src.core.events import EventBus
from src.core.models import Direction, PortfolioSnapshot, Position, Signal, SignalAction
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.manager import RiskManager
from src.risk.position_sizer import PositionSizer


@pytest.fixture
def risk_config(tmp_path):
    return {
        "position_sizing": {
            "method": "fixed_fractional",
            "risk_per_trade_pct": 2.0,
            "max_leverage": 5,
            "min_order_size_usd": 10,
        },
        "limits": {
            "max_open_positions": 3,
            "max_portfolio_heat_pct": 15.0,
            "max_daily_loss_pct": 5.0,
            "max_drawdown_pct": 15.0,
        },
        "stops": {"require_stop_loss": True},
        "circuit_breaker": {
            "daily_loss_halt": True,
            "max_drawdown_close_all": True,
            "consecutive_loss_limit": 5,
            "consecutive_loss_size_reduction": 0.5,
            "data_feed_timeout_seconds": 60,
            "max_consecutive_api_errors": 3,
            "state_file": str(tmp_path / "cb_state.json"),
            "audit_file": str(tmp_path / "cb_audit.log"),
        },
        # Venue symbols are BTCUSD/ETHUSD. The config (and this fixture) used to
        # say BTCUSDT/ETHUSDT, so the correlation check never matched anything.
        "correlation_groups": [["BTCUSD", "ETHUSD"]],
    }


@pytest.fixture
def portfolio():
    return PortfolioSnapshot(
        equity=10000.0,
        available_balance=10000.0,
        unrealized_pnl=0.0,
        realized_pnl_today=0.0,
        drawdown_pct=0.0,
        portfolio_heat_pct=0.0,
    )


def test_fixed_fractional_sizing(risk_config):
    sizer = PositionSizer(risk_config)
    signal = Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        strategy_name="test",
        confidence=0.8,
        entry_price=50000,
        stop_loss=49000,
    )
    size = sizer.calculate(signal, equity=10000)
    expected_risk = 10000 * 0.02
    expected_size = expected_risk / 1000
    assert abs(size - expected_size) < 0.001


def test_can_trade_basic(risk_config, portfolio):
    event_bus = EventBus()
    cb = CircuitBreaker(risk_config, event_bus)
    sizer = PositionSizer(risk_config)
    rm = RiskManager(risk_config, sizer, cb)

    can, reason = rm.can_trade(portfolio)
    assert can is True


def test_reject_max_positions(risk_config, portfolio):
    portfolio.open_positions = [
        Position(symbol=f"SYM{i}", side=Direction.LONG, entry_price=100, size=1)
        for i in range(3)
    ]
    event_bus = EventBus()
    cb = CircuitBreaker(risk_config, event_bus)
    rm = RiskManager(risk_config, PositionSizer(risk_config), cb)

    can, reason = rm.can_trade(portfolio)
    assert can is False
    assert "Max positions" in reason


def test_circuit_breaker_consecutive_losses(risk_config):
    event_bus = EventBus()
    cb = CircuitBreaker(risk_config, event_bus)

    for _ in range(5):
        cb.record_trade_result(-100)

    assert cb.size_multiplier == 0.5


def test_validate_signal_requires_stop_loss(risk_config, portfolio):
    event_bus = EventBus()
    cb = CircuitBreaker(risk_config, event_bus)
    rm = RiskManager(risk_config, PositionSizer(risk_config), cb)

    signal = Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        strategy_name="test",
        confidence=0.8,
        entry_price=50000,
        stop_loss=None,
    )
    valid, reason = rm.validate_signal(signal, portfolio)
    assert valid is False
    assert "Stop-loss" in reason


def test_correlated_exposure_blocks_second_leg(risk_config, portfolio):
    """An open BTCUSD position blocks a correlated ETHUSD entry.

    Regression: the correlation group listed BTCUSDT/ETHUSDT, which are not
    symbols this venue trades, so the gate never fired.
    """
    event_bus = EventBus()
    cb = CircuitBreaker(risk_config, event_bus)
    rm = RiskManager(risk_config, PositionSizer(risk_config), cb)

    portfolio.open_positions = [
        Position(
            symbol="BTCUSD",
            side=Direction.LONG,
            entry_price=50000,
            size=0.01,
            stop_loss=49000,
        )
    ]
    signal = Signal(
        symbol="ETHUSD",
        direction=Direction.LONG,
        strategy_name="test",
        confidence=0.8,
        entry_price=3000,
        stop_loss=2900,
    )

    assert rm._is_correlated_exposure("ETHUSD", portfolio) is True
    # An uncorrelated symbol is still allowed through.
    assert rm._is_correlated_exposure("SOLUSD", portfolio) is False


def test_position_size_uses_available_balance_not_total_equity(risk_config):
    event_bus = EventBus()
    cb = CircuitBreaker(risk_config, event_bus)
    rm = RiskManager(risk_config, PositionSizer(risk_config), cb)
    signal = Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        strategy_name="test",
        confidence=0.8,
        entry_price=50000,
        stop_loss=49000,
    )
    portfolio = PortfolioSnapshot(
        equity=10000.0,
        available_balance=1000.0,
        unrealized_pnl=0.0,
        realized_pnl_today=0.0,
        drawdown_pct=0.0,
        portfolio_heat_pct=0.0,
    )
    size = rm.calculate_position_size(signal, portfolio)
    full_equity_size = PositionSizer(risk_config).calculate(signal, equity=10000.0)
    assert size < full_equity_size
    assert size == PositionSizer(risk_config).calculate(signal, equity=1000.0)
