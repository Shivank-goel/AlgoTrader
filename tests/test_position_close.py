"""Tests for verified position close with exchange reconciliation."""

from __future__ import annotations

import asyncio
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest

from src.core.events import EventBus, FillEvent
from src.core.models import Direction, Order, OrderSide, OrderStatus, OrderType
from src.execution.exchange import DeltaExchangeClient, PositionCloseError
from src.execution.order_manager import OrderManager


@pytest.fixture
def exchange_client() -> DeltaExchangeClient:
    return DeltaExchangeClient(
        api_key="test-key",
        api_secret="test-secret",
        base_url="https://api.test",
        max_retries=3,
    )


@pytest.mark.asyncio
async def test_close_retries_until_exchange_position_is_flat(
    exchange_client: DeltaExchangeClient,
) -> None:
    """Close retries when HTTP succeeds but exchange position remains open."""
    symbol = "BTCUSD"
    close_attempts = 0
    flat_results = [False, False, False, True]
    flat_idx = 0

    async def fake_is_flat(sym: str) -> bool:
        nonlocal flat_idx
        result = flat_results[min(flat_idx, len(flat_results) - 1)]
        flat_idx += 1
        return result

    async def fake_place_order(order: Order) -> Order:
        nonlocal close_attempts
        close_attempts += 1
        order.status = OrderStatus.FILLED
        order.filled_size = order.size
        order.avg_fill_price = 50_000.0
        order.exchange_order_id = f"ex-{close_attempts}"
        return order

    event_bus = EventBus()
    fills: list[FillEvent] = []
    # Two exit orders are placed here (the exchange reports the position still
    # open after the first), and both fill — so two FillEvents are expected.
    # Waiting on a single-shot Event raced: whether the second had been
    # dispatched by assertion time was luck.
    expected_fills = 2
    all_fills_done = asyncio.Event()

    async def on_fill(event: FillEvent) -> None:
        fills.append(event)
        if len(fills) >= expected_fills:
            all_fills_done.set()

    event_bus.subscribe(FillEvent, on_fill)
    await event_bus.start()
    order_manager = OrderManager(exchange_client, event_bus, paper_mode=False)

    try:
        with patch.object(exchange_client, "is_position_flat", side_effect=fake_is_flat):
            with patch.object(exchange_client, "place_order", side_effect=fake_place_order):
                with patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock):
                    result = await order_manager.close_position_verified(
                        symbol=symbol,
                        direction=Direction.LONG,
                        size=1.0,
                        strategy_name="test",
                        reason="stop_loss",
                        max_retries=4,
                        base_interval=0.01,
                    )
                    await asyncio.wait_for(all_fills_done.wait(), timeout=1.0)
    finally:
        await event_bus.stop()

    assert close_attempts == 2
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert len(fills) == expected_fills
    # Every exit fill must be marked as such, so the engine's fill handler can
    # discard the duplicate instead of opening a phantom opposite position.
    assert all(f.order.is_exit for f in fills)
    assert all(f.fill_price > 0 for f in fills)


@pytest.mark.asyncio
async def test_close_exhaustion_halts_symbol_and_alerts(
    exchange_client: DeltaExchangeClient,
) -> None:
    """Failed closes escalate with CRITICAL alert and symbol halt."""
    alerts: list[str] = []

    async def fake_alert(message: str) -> None:
        alerts.append(message)

    async def always_open(_symbol: str) -> bool:
        return False

    async def fake_place_order(order: Order) -> Order:
        order.status = OrderStatus.FILLED
        order.filled_size = order.size
        order.avg_fill_price = 3_000.0
        order.exchange_order_id = "ex-fail"
        return order

    order_manager = OrderManager(
        exchange_client,
        EventBus(),
        paper_mode=False,
        alert_callback=fake_alert,
    )

    with patch.object(exchange_client, "is_position_flat", side_effect=always_open):
        with patch.object(exchange_client, "place_order", side_effect=fake_place_order):
            with patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(PositionCloseError) as exc_info:
                    await order_manager.close_position_verified(
                        symbol="ETHUSD",
                        direction=Direction.SHORT,
                        size=2.0,
                        reason="circuit_breaker",
                        max_retries=3,
                        base_interval=0.01,
                    )

    assert exc_info.value.attempts == 3
    assert exc_info.value.symbol == "ETHUSD"
    assert order_manager.is_symbol_halted("ETHUSD")
    assert len(alerts) == 1
    assert "CRITICAL" in alerts[0]
    assert "HUMAN INTERVENTION REQUIRED" in alerts[0]


@pytest.mark.asyncio
async def test_close_detects_fill_during_ambiguous_window(
    exchange_client: DeltaExchangeClient,
) -> None:
    """Verification detects flat exchange state even when close order status is OPEN."""
    checks = 0

    async def fake_is_flat(_symbol: str) -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    async def fake_place_order(order: Order) -> Order:
        order.status = OrderStatus.OPEN
        order.exchange_order_id = "ex-open"
        return order

    order_manager = OrderManager(exchange_client, EventBus(), paper_mode=False)

    with patch.object(exchange_client, "is_position_flat", side_effect=fake_is_flat):
        with patch.object(exchange_client, "place_order", side_effect=fake_place_order):
            with patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock):
                result = await order_manager.close_position_verified(
                    symbol="BTCUSD",
                    direction=Direction.LONG,
                    size=1.0,
                    reason="stop_loss",
                    max_retries=3,
                    base_interval=0.01,
                )

    assert result is not None
    assert result.status == OrderStatus.OPEN
    assert checks >= 2
