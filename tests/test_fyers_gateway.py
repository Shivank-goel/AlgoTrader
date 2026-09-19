"""Exercise the read-only gateway at its HTTP boundary, without broker access."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.execution.fyers import FyersClient, FyersGatewayError


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("get_profile", "profile"), ("get_funds", "funds"),
    ("get_holdings", "holdings"), ("get_positions", "positions"),
    ("get_orders", "orders"), ("get_trades", "tradebook"),
])
async def test_account_requests_use_get_and_auth(method, path):
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value={"s": "ok", "data": []})
    session = MagicMock(closed=False)
    session.get.return_value.__aenter__ = AsyncMock(return_value=response)
    session.close = AsyncMock()
    with patch("src.execution.fyers.aiohttp.ClientSession", return_value=session) as factory:
        client = FyersClient("app-200", "secret", base_url="https://api-t1.fyers.in/api/v3")
        assert await getattr(client, method)() == {"s": "ok", "data": []}
        assert factory.call_args.kwargs["headers"] == {"Authorization": "app-200:secret"}
        session.get.assert_called_once_with(f"https://api-t1.fyers.in/api/v3/{path}", allow_redirects=False)
        await client.close()
        session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
    (401, {"message": "secret"}), (429, {}), (302, {}),
    (200, {"s": "error", "message": "secret"}), (200, []),
])
async def test_errors_fail_without_exposing_response(status, body):
    client = FyersClient("app", "secret", base_url="https://api-t1.fyers.in/api/v3")
    response = MagicMock(status=status)
    response.json = AsyncMock(return_value=body)
    client._session = MagicMock(closed=False)
    client._session.get.return_value.__aenter__ = AsyncMock(return_value=response)
    with pytest.raises(FyersGatewayError) as error:
        await client.get_profile()
    assert "secret" not in str(error.value)
    assert client._session.get.call_count == 1


def test_from_env_reads_repository_config(monkeypatch):
    monkeypatch.setenv("FYERS_APP_ID", "app-200")
    monkeypatch.setenv("FYERS_ACCESS_TOKEN", "secret")
    assert FyersClient.from_env().base_url == "https://api-t1.fyers.in/api/v3"
    monkeypatch.delenv("FYERS_ACCESS_TOKEN")
    with pytest.raises(ValueError, match="required"):
        FyersClient.from_env()


def test_credentials_cannot_be_sent_to_other_host():
    with pytest.raises(ValueError, match="official"):
        FyersClient("app", "secret", base_url="https://example.com/api/v3")
