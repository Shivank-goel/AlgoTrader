from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.fyers import FyersClient, FyersGatewayError


def client_with(body):
    client = FyersClient("app", "token", base_url="https://api-t1.fyers.in/api/v3")
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value=body)
    client._session = MagicMock(closed=False)
    client._session.get.return_value.__aenter__ = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_quotes_use_data_endpoint():
    client = client_with({"s": "ok", "d": [{"s": "ok", "n": "NSE:SBIN-EQ", "v": {"lp": 100}}]})
    await client.get_quotes(["NSE:SBIN-EQ"])
    client._session.get.assert_called_once_with("https://api-t1.fyers.in/data/quotes", allow_redirects=False, params={"symbols": "NSE:SBIN-EQ"})


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[], [{"s": "error", "n": "NSE:SBIN-EQ"}], [{"s": "ok", "n": "NSE:OTHER-EQ", "v": {}}]])
async def test_partial_quote_failures_are_not_success(rows):
    with pytest.raises(FyersGatewayError):
        await client_with({"s": "ok", "d": rows}).get_quotes(["NSE:SBIN-EQ"])


@pytest.mark.asyncio
async def test_history_request_and_range_validation():
    client = client_with({"s": "ok", "candles": []})
    await client.get_history("NSE:SBIN-EQ", date(2026, 9, 1), date(2026, 9, 10))
    call = client._session.get.call_args
    assert call.args[0].endswith("/data/history")
    assert call.kwargs["params"]["range_from"] == "2026-09-01"
    assert call.kwargs["params"]["cont_flag"] == "0"
    with pytest.raises(ValueError):
        await client.get_history("NSE:SBIN-EQ", date(2026, 9, 10), date(2026, 9, 1))
    assert client._session.get.call_count == 1
