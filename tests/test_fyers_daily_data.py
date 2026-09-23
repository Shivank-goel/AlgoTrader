from datetime import date
from unittest.mock import AsyncMock

import pytest

from src.execution.fyers import FyersGatewayError
from src.fyers.daily_data import _forward_universe_path, _history_with_rate_limit
from src.fyers.models import RuntimeConfig


@pytest.mark.asyncio
async def test_history_rate_limit_retries_with_bounded_backoff(monkeypatch):
    client = type("Client", (), {})()
    client.get_history = AsyncMock(side_effect=[
        FyersGatewayError("FYERS history: HTTP 429"), {"s": "ok", "candles": []},
    ])
    sleeps = []

    async def pause(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("src.fyers.daily_data.asyncio.sleep", pause)
    config = RuntimeConfig.load().model_copy(update={
        "history_rate_limit_retries": 2, "history_rate_limit_backoff_seconds": 3,
    })
    result = await _history_with_rate_limit(client, "NSE:SBIN-EQ", date(2026, 9, 1), date(2026, 9, 2), config)
    assert result == {"s": "ok", "candles": []}
    assert sleeps == [3]
    assert client.get_history.await_count == 2


@pytest.mark.asyncio
async def test_history_non_rate_limit_error_is_not_retried(monkeypatch):
    client = type("Client", (), {})()
    client.get_history = AsyncMock(side_effect=FyersGatewayError("FYERS history: HTTP 400"))
    monkeypatch.setattr("src.fyers.daily_data.asyncio.sleep", AsyncMock())
    with pytest.raises(FyersGatewayError, match="HTTP 400"):
        await _history_with_rate_limit(client, "NSE:SBIN-EQ", date(2026, 9, 1), date(2026, 9, 2), RuntimeConfig.load())
    assert client.get_history.await_count == 1


def test_daily_sync_uses_the_top_level_forward_universe_path():
    assert _forward_universe_path(RuntimeConfig.load()).name == "nse_forward_universe.yaml"
