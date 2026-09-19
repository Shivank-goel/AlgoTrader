"""Control authorization is enforced before the engine can be called."""

from unittest.mock import AsyncMock

import httpx
import pytest

from src.dashboard.app import create_app


@pytest.mark.parametrize("token", ["", "short"])
async def test_controls_disabled_without_strong_token(monkeypatch, token):
    monkeypatch.setenv("DASHBOARD_CONTROL_TOKEN", token)
    engine = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://test") as client:
        response = await client.post("/api/engine/start")
    assert response.status_code == 503
    assert "error" in response.json()
    engine.start_background.assert_not_called()


@pytest.mark.parametrize("method,path", [("POST", "/api/engine/start"), ("POST", "/api/mode"), ("DELETE", "/api/pairs/BTCUSD"), ("POST", "/api/universe/rescan")])
async def test_all_control_routes_reject_unauthorized(monkeypatch, method, path):
    monkeypatch.setenv("DASHBOARD_CONTROL_TOKEN", "a" * 32)
    engine = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://test") as client:
        response = await client.request(method, path, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert engine.mock_calls == []


async def test_authorized_control_and_public_monitoring(monkeypatch):
    token = "a" * 32
    monkeypatch.setenv("DASHBOARD_CONTROL_TOKEN", token)
    engine = AsyncMock()
    engine.start_background.return_value = {"status": "started"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://test") as client:
        response = await client.post("/api/engine/start", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    engine.start_background.assert_awaited_once()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        assert (await client.get("/api/heartbeat")).status_code == 200


async def test_fyers_readiness_is_public_but_halt_requires_auth(monkeypatch):
    monkeypatch.setenv("DASHBOARD_CONTROL_TOKEN", "a" * 32)
    monkeypatch.setattr("src.fyers.operations.readiness", lambda config: {
        "live_enabled": False, "entry_ready": False, "blockers": ["fixture"], "alerts": []})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        report = await client.get("/api/fyers/readiness")
        denied = await client.post("/api/fyers/halt", json={"reason": "stop"})
    assert report.json()["blockers"] == ["fixture"]
    assert denied.status_code == 401
