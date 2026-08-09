"""Tests for idempotent order submission via client_order_id."""

from __future__ import annotations

from typing import Optional
from unittest.mock import patch

import pytest

from src.core.models import Order, OrderSide, OrderType, new_client_order_id
from src.execution.exchange import AmbiguousOrderError, DeltaExchangeClient


@pytest.fixture
def exchange_client() -> DeltaExchangeClient:
    return DeltaExchangeClient(
        api_key="test-key",
        api_secret="test-secret",
        base_url="https://api.test",
        max_retries=3,
    )


@pytest.mark.asyncio
async def test_timeout_then_lookup_adopts_single_order_on_mock_exchange(
    exchange_client: DeltaExchangeClient,
) -> None:
    """Submit → timeout (order accepted) → lookup adopts; only one order exists."""
    client_order_id = new_client_order_id()
    mock_store: dict[str, dict] = {}
    post_attempts: list[str] = []

    async def fake_request_once(
        method: str,
        path: str,
        data: Optional[dict] = None,
        auth: bool = False,
        params: Optional[dict] = None,
    ) -> dict:
        if method == "POST" and path == "/v2/orders":
            assert data is not None
            cid = data["client_order_id"]
            post_attempts.append(cid)

            if cid not in mock_store:
                mock_store[cid] = {
                    "id": "exchange-order-1",
                    "client_order_id": cid,
                    "state": "closed",
                    "average_fill_price": "50000",
                    "filled_size": 1,
                }

            if len(post_attempts) == 1:
                raise AmbiguousOrderError("simulated timeout after exchange accept")

            raise AssertionError(
                "Second POST must not run when lookup should adopt the existing order"
            )

        raise AssertionError(f"Unexpected request: {method} {path}")

    async def fake_lookup(cid: str) -> Optional[dict]:
        return mock_store.get(cid)

    payload = {
        "product_id": 1,
        "size": 1,
        "side": "buy",
        "order_type": "market_order",
        "client_order_id": client_order_id,
    }

    with patch.object(exchange_client, "_request_once", side_effect=fake_request_once):
        with patch.object(
            exchange_client, "lookup_order_by_client_id", side_effect=fake_lookup
        ):
            result = await exchange_client._submit_order_with_idempotency(payload)

    assert len(post_attempts) == 1
    assert post_attempts[0] == client_order_id
    assert len(mock_store) == 1
    assert result[0]["id"] == "exchange-order-1"
    assert result[0]["client_order_id"] == client_order_id
    assert result[1] is True


@pytest.mark.asyncio
async def test_place_order_includes_client_order_id_in_payload(
    exchange_client: DeltaExchangeClient,
) -> None:
    """place_order must send the order's stable client_order_id on the payload."""
    order = Order(
        symbol="BTCUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        size=0.01,
        price=50_000.0,
        client_order_id=new_client_order_id(),
    )
    captured_payload: dict = {}

    async def fake_submit(payload: dict) -> tuple[dict, bool]:
        captured_payload.update(payload)
        return (
            {
                "id": "99",
                "client_order_id": payload["client_order_id"],
                "state": "closed",
                "average_fill_price": "50000",
                "filled_size": 1,
            },
            False,
        )

    with patch.object(exchange_client, "get_product", return_value={"id": 1, "min_size": 1}):
        with patch.object(
            exchange_client,
            "_submit_order_with_idempotency",
            side_effect=fake_submit,
        ):
            filled = await exchange_client.place_order(order)

    assert captured_payload["client_order_id"] == order.client_order_id


@pytest.mark.asyncio
async def test_contracts_for_notional_caps_to_position_size_limit(
    exchange_client: DeltaExchangeClient,
) -> None:
    product = {
        "min_size": 1,
        "contract_value": "0.001",
        "position_size_limit": 5000,
    }
    with patch.object(exchange_client, "get_product", return_value=product):
        contracts = await exchange_client.contracts_for_notional("ADAUSD", 100_000.0)

    assert contracts == 5000
