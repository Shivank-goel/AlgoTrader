"""Tests for Dhan transport safety and correlation-ID recovery."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.execution.dhan import (
    DhanAmbiguousOrderError,
    DhanClient,
    DhanLiveTradingDisabled,
    DhanOrderRequest,
    DhanOrderResponse,
    DhanOrderStatus,
    new_dhan_correlation_id,
)


def make_request(**overrides: object) -> DhanOrderRequest:
    values: dict[str, object] = {
        "security_id": "11536",
        "quantity": 1,
        "transaction_type": "BUY",
        "exchange_segment": "NSE_EQ",
        "product_type": "CNC",
        "order_type": "MARKET",
    }
    values.update(overrides)
    return DhanOrderRequest(**values)


def test_correlation_id_is_dhan_compatible() -> None:
    correlation_id = new_dhan_correlation_id()

    assert len(correlation_id) == 30
    assert correlation_id.startswith("dh_")


def test_limit_order_requires_positive_price() -> None:
    with pytest.raises(ValidationError, match="positive price"):
        make_request(order_type="LIMIT")


@pytest.mark.asyncio
async def test_order_submission_is_disabled_by_default() -> None:
    client = DhanClient("client", "token")

    with pytest.raises(DhanLiveTradingDisabled):
        await client.place_order(make_request())


@pytest.mark.asyncio
async def test_submit_encodes_dhan_payload() -> None:
    client = DhanClient("client", "token", allow_order_submission=True)
    request = make_request(correlation_id="safe-correlation-id")
    client._request = AsyncMock(return_value={"orderId": "42", "orderStatus": "PENDING"})

    result = await client.place_order(request)

    assert result.order_id == "42"
    assert result.order_status == DhanOrderStatus.PENDING
    client._request.assert_awaited_once_with("POST", "/orders", payload=request.to_payload("client"))


@pytest.mark.asyncio
async def test_ambiguous_submit_adopts_order_by_correlation_id() -> None:
    client = DhanClient("client", "token", allow_order_submission=True)
    request = make_request(correlation_id="one-stable-key")
    client._request = AsyncMock(side_effect=DhanAmbiguousOrderError("timeout"))
    client.get_order_by_correlation_id = AsyncMock(
        return_value=DhanOrderResponse.model_validate(
            {"orderId": "already-accepted", "orderStatus": "TRADED"}
        )
    )

    result = await client.place_order(request)

    assert result.order_id == "already-accepted"
    assert result.order_status == DhanOrderStatus.TRADED
    client.get_order_by_correlation_id.assert_awaited_once_with("one-stable-key")
