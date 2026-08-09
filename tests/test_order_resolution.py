"""Tests for ambiguous order resolution and symbol halt behavior."""

from __future__ import annotations

import asyncio
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest

from src.core.events import EventBus, FillEvent
from src.core.models import Direction, Order, OrderSide, OrderType, Position, new_client_order_id
from src.execution.exchange import (
    DeltaExchangeClient,
    OrderResolutionError,
)
from src.execution.order_manager import OrderManager
from src.portfolio.manager import PortfolioManager


@pytest.fixture
def exchange_client() -> DeltaExchangeClient:
    return DeltaExchangeClient(
        api_key="test-key",
        api_secret="test-secret",
        base_url="https://api.test",
        max_retries=3,
    )


@pytest.mark.asyncio
async def test_poll_detects_fill_during_ambiguous_window_and_updates_position(
    exchange_client: DeltaExchangeClient,
) -> None:
    """Ambiguous submit → poll sees fill → FillEvent updates portfolio."""
    client_order_id = new_client_order_id()
    poll_calls = 0

    async def fake_submit(payload: dict) -> tuple[dict, bool]:
        return (
            {
                "id": "ex-1",
                "client_order_id": payload["client_order_id"],
                "state": "open",
            },
            True,
        )

    async def fake_lookup(cid: str) -> Optional[dict]:
        nonlocal poll_calls
        poll_calls += 1
        if poll_calls < 2:
            return {
                "id": "ex-1",
                "client_order_id": cid,
                "state": "open",
            }
        return {
            "id": "ex-1",
            "client_order_id": cid,
            "state": "closed",
            "average_fill_price": "51000",
            "filled_size": 2,
        }

    order = Order(
        symbol="BTCUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        size=0.01,
        price=51_000.0,
        client_order_id=client_order_id,
    )

    event_bus = EventBus()
    portfolio = PortfolioManager(initial_equity=10_000.0)
    fills: list[FillEvent] = []

    async def on_fill(event: FillEvent) -> None:
        fills.append(event)
        portfolio.open_position(
            Position(
                symbol=event.order.symbol,
                side=Direction.LONG,
                entry_price=event.fill_price,
                size=event.fill_size,
                strategy_name=event.order.strategy_name,
            )
        )

    event_bus.subscribe(FillEvent, on_fill)
    await event_bus.start()
    order_manager = OrderManager(exchange_client, event_bus, paper_mode=False)

    try:
        with patch.object(exchange_client, "get_product", return_value={"id": 1, "min_size": 1}):
            with patch.object(
                exchange_client, "_submit_order_with_idempotency", side_effect=fake_submit
            ):
                with patch.object(
                    exchange_client, "lookup_order_by_client_id", side_effect=fake_lookup
                ):
                    with patch.object(
                        exchange_client,
                        "poll_order_until_terminal",
                        wraps=exchange_client.poll_order_until_terminal,
                    ) as poll_spy:
                        resolved = await order_manager.submit_order(order)
                        await asyncio.sleep(0.05)
    finally:
        await event_bus.stop()

    poll_spy.assert_awaited_once()
    assert resolved.status.value == "filled"
    assert resolved.avg_fill_price == 51_000.0
    assert resolved.filled_size == 2.0
    assert len(fills) == 1
    assert fills[0].fill_price == 51_000.0
    assert "BTCUSD" in portfolio._positions


@pytest.mark.asyncio
async def test_unresolved_order_halts_symbol_and_alerts(
    exchange_client: DeltaExchangeClient,
) -> None:
    """Poll exhaustion halts symbol, logs critical path, and fires alert callback."""
    client_order_id = new_client_order_id()
    alert_messages: list[str] = []

    async def fake_alert(message: str) -> None:
        alert_messages.append(message)

    async def fake_submit(payload: dict) -> tuple[dict, bool]:
        return (
            {
                "id": "ex-stuck",
                "client_order_id": payload["client_order_id"],
                "state": "open",
            },
            True,
        )

    async def fake_lookup(_cid: str) -> Optional[dict]:
        return {
            "id": "ex-stuck",
            "client_order_id": client_order_id,
            "state": "open",
        }

    order = Order(
        symbol="ETHUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        size=0.5,
        price=3_000.0,
        client_order_id=client_order_id,
    )

    event_bus = EventBus()
    order_manager = OrderManager(
        exchange_client,
        event_bus,
        paper_mode=False,
        alert_callback=fake_alert,
    )

    with patch.object(exchange_client, "get_product", return_value={"id": 2, "min_size": 1}):
        with patch.object(
            exchange_client, "_submit_order_with_idempotency", side_effect=fake_submit
        ):
            with patch.object(
                exchange_client, "lookup_order_by_client_id", side_effect=fake_lookup
            ):
                with patch.object(
                    exchange_client,
                    "poll_order_until_terminal",
                    side_effect=OrderResolutionError(client_order_id, "ETHUSD", polls=3),
                ):
                    with pytest.raises(OrderResolutionError):
                        await order_manager.submit_order(order)

    assert order_manager.is_symbol_halted("ETHUSD")
    assert len(alert_messages) == 1
    assert "UNRESOLVED ORDER" in alert_messages[0]
    assert "ETHUSD" in alert_messages[0]


@pytest.mark.asyncio
async def test_poll_order_until_terminal_with_exponential_backoff(
    exchange_client: DeltaExchangeClient,
) -> None:
    """Poll transitions from open to filled and returns terminal payload."""
    client_order_id = new_client_order_id()
    poll_idx = 0

    async def fake_lookup(cid: str) -> Optional[dict]:
        nonlocal poll_idx
        poll_idx += 1
        if poll_idx < 3:
            return {"id": "1", "client_order_id": cid, "state": "open"}
        return {
            "id": "1",
            "client_order_id": cid,
            "state": "filled",
            "average_fill_price": "42000",
            "filled_size": 1,
        }

    with patch.object(
        exchange_client, "lookup_order_by_client_id", side_effect=fake_lookup
    ):
        with patch("src.execution.exchange.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
            result = await exchange_client.poll_order_until_terminal(
                client_order_id,
                symbol="BTCUSD",
                max_polls=5,
                base_interval=0.1,
            )

    assert result["state"] == "filled"
    assert poll_idx == 3
    assert sleep_mock.await_count == 2
