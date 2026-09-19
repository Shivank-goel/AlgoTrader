import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.execution.fyers_login import CALLBACK_HANDLER, callback_address, exchange_code, make_app
from src.execution.fyers import FyersGatewayError


@pytest.mark.parametrize("uri", ["http://0.0.0.0:8080/callback", "https://example.com/callback", "http://127.0.0.1:8080/callback?x=1"])
def test_nonlocal_or_ambiguous_redirect_rejected(uri):
    with pytest.raises(ValueError):
        callback_address(uri)


@pytest.mark.asyncio
async def test_callback_checks_state_and_accepts_code_once():
    future = asyncio.get_running_loop().create_future()
    app = make_app("expected", future)
    app.router.add_get("/callback", app[CALLBACK_HANDLER])
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/callback?state=wrong&auth_code=secret")
        assert response.status == 400
        assert not future.done()
        response = await client.get("/callback?state=expected")
        assert response.status == 400
        assert not future.done()
        response = await client.get("/callback?state=expected&auth_code=secret")
        assert response.status == 200
        assert "secret" not in await response.text()
        assert response.headers["Cache-Control"] == "no-store"
        assert future.result() == "secret"
        response = await client.get("/callback?state=expected&auth_code=other")
        assert response.status == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("body,success", [({"s": "ok", "access_token": "token"}, True), ({"s": "error", "message": "sensitive"}, False)])
async def test_token_exchange_validates_response(body, success):
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value=body)
    session = MagicMock()
    session.post.return_value.__aenter__ = AsyncMock(return_value=response)
    with patch("src.execution.fyers_login.aiohttp.ClientSession") as factory:
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        if success:
            assert await exchange_code("https://api-t1.fyers.in/api/v3", "app", "secret", "code") == "token"
            payload = session.post.call_args.kwargs["json"]
            import hashlib
            assert payload["appIdHash"] == hashlib.sha256(b"app:secret").hexdigest()
            assert session.post.call_args.kwargs["allow_redirects"] is False
        else:
            with pytest.raises(FyersGatewayError, match="valid access token"):
                await exchange_code("https://api-t1.fyers.in/api/v3", "app", "secret", "code")
