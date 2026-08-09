"""Tests for manual-only circuit breaker resume."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.events import EventBus
from src.core.models import PortfolioSnapshot
from src.risk.circuit_breaker import CircuitBreaker


@pytest.fixture
def breaker(tmp_path: Path) -> CircuitBreaker:
    state_file = tmp_path / "cb_state.json"
    audit_file = tmp_path / "cb_audit.log"
    config = {
        "circuit_breaker": {
            "daily_loss_halt": True,
            "state_file": str(state_file),
            "audit_file": str(audit_file),
            "manual_resume_only": True,
        },
        "limits": {"max_daily_loss_pct": 5.0, "max_drawdown_pct": 15.0},
    }
    return CircuitBreaker(config, EventBus())


def test_halt_persists_to_disk(breaker: CircuitBreaker) -> None:
    portfolio = PortfolioSnapshot(
        equity=9000.0,
        available_balance=9000.0,
        unrealized_pnl=0.0,
        realized_pnl_today=-1000.0,
        drawdown_pct=10.0,
        portfolio_heat_pct=0.0,
    )
    breaker._daily_start_equity = 10000.0
    event = breaker.check(portfolio)

    assert event is not None
    assert event.reason == "daily_loss_limit"
    assert breaker.is_halted

    saved = json.loads(breaker._state_path.read_text())
    assert saved["halted"] is True
    assert saved["reason"] == "daily_loss_limit"


def test_manual_resume_requires_operator_and_writes_audit(
    breaker: CircuitBreaker,
) -> None:
    breaker._set_halted("daily_loss_limit", "halt", {"daily_loss_pct": 6.0})

    assert breaker.manual_resume("alice@devbox") is True
    assert not breaker.is_halted

    saved = json.loads(breaker._state_path.read_text())
    assert saved["halted"] is False
    assert saved["last_resume_by"] == "alice@devbox"
    assert saved["last_resume_at"] is not None

    audit = breaker._audit_path.read_text()
    assert "RESUME operator=alice@devbox" in audit


def test_manual_resume_is_noop_when_not_halted(breaker: CircuitBreaker) -> None:
    assert breaker.manual_resume("bob@devbox") is False


def test_refresh_from_disk_applies_external_resume(breaker: CircuitBreaker) -> None:
    breaker._set_halted("max_drawdown", "close_all", {"drawdown_pct": 20.0})
    assert breaker.is_halted

    payload = json.loads(breaker._state_path.read_text())
    payload["halted"] = False
    payload["last_resume_by"] = "cli@host"
    payload["last_resume_at"] = "2026-07-03T12:00:00"
    breaker._state_path.write_text(json.dumps(payload))

    breaker.refresh_from_disk()

    assert not breaker.is_halted
    assert breaker.get_halt_status()["last_resume_by"] == "cli@host"


def test_close_all_sets_halted(breaker: CircuitBreaker) -> None:
    portfolio = PortfolioSnapshot(
        equity=8000.0,
        available_balance=8000.0,
        unrealized_pnl=0.0,
        realized_pnl_today=0.0,
        drawdown_pct=20.0,
        portfolio_heat_pct=0.0,
    )
    breaker._peak_equity = 10000.0
    event = breaker.check(portfolio)

    assert event is not None
    assert event.action == "close_all"
    assert breaker.is_halted
    assert breaker.get_halt_status()["reason"] == "max_drawdown"
