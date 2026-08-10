"""Tests for TradingEngine._on_fill — the only path that writes trade records.

Regressions covered here caused silent portfolio corruption:

1. A duplicate exit fill (the close was retried because the exchange still
   reported the position open) arrived after the position had been popped.
   `_on_fill` inferred "no local position => this is an entry" and opened a
   phantom position facing the opposite way.
2. Paper-mode closes synthesised a fill at price 0.0, so every paper close
   booked a loss of 100% of notional.
"""

from __future__ import annotations

import pytest

from src.core.events import FillEvent
from src.core.models import Direction, Order, OrderSide, OrderStatus, OrderType, Position


@pytest.fixture
def engine(isolated_engine):
    return isolated_engine


def _exit_order(symbol: str = "BTCUSD", size: float = 1.0) -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        size=size,
        status=OrderStatus.FILLED,
        filled_size=size,
        avg_fill_price=51_000.0,
        strategy_name="test",
        is_exit=True,
    )


async def test_duplicate_exit_fill_does_not_open_phantom_position(engine):
    """A second exit fill for an already-closed position is discarded."""
    engine.portfolio.open_position(
        Position(
            symbol="BTCUSD",
            side=Direction.LONG,
            entry_price=50_000.0,
            size=1.0,
            strategy_name="test",
        )
    )

    order = _exit_order()
    await engine._on_fill(FillEvent(order=order, fill_price=51_000.0, fill_size=1.0))
    assert engine.portfolio._positions == {}, "first exit fill should close the position"

    # The retry's fill lands after the position is already gone.
    await engine._on_fill(FillEvent(order=order, fill_price=51_000.0, fill_size=1.0))

    assert engine.portfolio._positions == {}, (
        "duplicate exit fill re-opened a position — a SELL fill with no open "
        "position was treated as a new short entry"
    )
    assert len(engine.journal.get_trades()) == 1, "the close was journalled twice"


async def test_entry_fill_still_opens_a_position(engine):
    """The exit guard must not block genuine entries."""
    entry = Order(
        symbol="BTCUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        size=1.0,
        price=50_000.0,
        stop_loss=49_000.0,
        take_profit=52_000.0,
        status=OrderStatus.FILLED,
        filled_size=1.0,
        avg_fill_price=50_000.0,
        strategy_name="test",
    )
    await engine._on_fill(FillEvent(order=entry, fill_price=50_000.0, fill_size=1.0))

    pos = engine.portfolio._positions.get("BTCUSD")
    assert pos is not None
    assert pos.side == Direction.LONG
    assert pos.entry_price == 50_000.0


@pytest.mark.parametrize("bad_price", [0.0, -1.0, None])
async def test_non_positive_fill_price_is_rejected(engine, bad_price):
    """A zero/negative fill price must not mutate portfolio state."""
    engine.portfolio.open_position(
        Position(
            symbol="BTCUSD",
            side=Direction.LONG,
            entry_price=50_000.0,
            size=1.0,
            strategy_name="test",
        )
    )
    equity_before = engine.portfolio.equity

    order = _exit_order()
    await engine._on_fill(FillEvent(order=order, fill_price=bad_price, fill_size=1.0))

    assert "BTCUSD" in engine.portfolio._positions, "position closed at a bogus price"
    assert engine.portfolio.equity == equity_before
    assert engine.journal.get_trades() == []
