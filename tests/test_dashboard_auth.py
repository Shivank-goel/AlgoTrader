from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.dashboard.app import create_app


def supervisor():
    value = MagicMock()
    value.status.return_value = {"running": False, "live_enabled": False}
    value.start = AsyncMock(return_value={"running": True, "live_enabled": False})
    value.stop = AsyncMock(return_value={"running": False, "live_enabled": False})
    value.schedule = AsyncMock()
    return value


@pytest.mark.parametrize("token", ["", "short"])
async def test_controls_disabled_without_strong_token(monkeypatch, token):
    monkeypatch.setenv("DASHBOARD_CONTROL_TOKEN", token)
    control = supervisor()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(supervisor=control)), base_url="http://test") as client:
        response = await client.post("/api/fyers/recorder/start")
    assert response.status_code == 503 and "error" in response.json()
    control.start.assert_not_awaited()


@pytest.mark.parametrize("path", ["/api/fyers/recorder/start", "/api/fyers/recorder/stop",
                                   "/api/fyers/halt", "/api/fyers/backup"])
async def test_every_control_rejects_unauthorized(monkeypatch, path):
    monkeypatch.setenv("DASHBOARD_CONTROL_TOKEN", "a" * 32)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(supervisor=supervisor())), base_url="http://test") as client:
        response = await client.post(path, json={"reason": "fixture"},
                                     headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


async def test_fyers_only_home_snapshot_and_safe_recorder_control(monkeypatch):
    token = "a" * 32
    monkeypatch.setenv("DASHBOARD_CONTROL_TOKEN", token)
    monkeypatch.setattr("src.dashboard.app.dashboard_snapshot", lambda: {
        "live_enabled": False, "readiness": {"blockers": []}, "sections": {}})
    control = supervisor()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(supervisor=control)), base_url="http://test") as client:
        home = await client.get("/")
        snapshot = await client.get("/api/fyers/dashboard")
        started = await client.post("/api/fyers/recorder/start",
                                    headers={"Authorization": f"Bearer {token}"})
        removed = await client.post("/api/engine/start",
                                    headers={"Authorization": f"Bearer {token}"})
    assert home.status_code == 200 and "FYERS · NSE Operations" in home.text
    assert "Adaptive Crypto Trader" not in home.text
    assert not snapshot.json()["live_enabled"] and started.status_code == 200
    assert removed.status_code == 404
