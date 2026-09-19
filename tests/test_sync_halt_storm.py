"""Regression tests for the exchange-sync halt storm.

A single outage produced 3034 halts: sync_exchange_state re-triggered the
circuit breaker on *every* failure past the threshold, each one rewriting
data/circuit_breaker_state.json, appending to the audit log and firing a
Telegram alert. The audit log reached 2 MB from one incident.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def failing_engine(isolated_engine, monkeypatch):
    """Engine whose exchange sync always raises, with alerts captured."""
    eng = isolated_engine
    eng.order_manager.paper_mode = False  # exercise broker sync, independent of safe defaults

    eng.exchange.get_balance = AsyncMock(side_effect=ConnectionError("network down"))
    eng.exchange.get_positions = AsyncMock(side_effect=ConnectionError("network down"))

    alerts: list[str] = []

    async def capture_alert(message: str) -> None:
        alerts.append(message)

    monkeypatch.setattr(eng.notifier, "send_alert", capture_alert)
    eng._captured_alerts = alerts
    return eng


async def test_persistent_sync_failure_halts_exactly_once(failing_engine):
    eng = failing_engine
    max_failures = int(
        eng.settings.get("engine", {}).get("max_sync_failures", 5)
    )

    halt_calls: list[tuple[str, str]] = []
    original_set_halted = eng.circuit_breaker._set_halted

    def counting_set_halted(reason, action, details):
        halt_calls.append((reason, action))
        return original_set_halted(reason, action, details)

    eng.circuit_breaker._set_halted = counting_set_halted

    for _ in range(max_failures * 10):
        await eng.sync_exchange_state()

    assert eng._consecutive_sync_failures == max_failures * 10
    assert len(halt_calls) == 1, (
        f"circuit breaker halted {len(halt_calls)} times for one outage; "
        "it must fire only on the threshold crossing"
    )
    assert len(eng._captured_alerts) == 1, "one outage produced repeated alerts"


async def test_audit_log_records_one_halt_per_outage(failing_engine):
    eng = failing_engine
    audit_path = eng.circuit_breaker._audit_path

    for _ in range(30):
        await eng.sync_exchange_state()

    halt_lines = [
        line
        for line in audit_path.read_text().splitlines()
        if "exchange_sync_failure" in line
    ]
    assert len(halt_lines) == 1, f"audit log grew to {len(halt_lines)} halt entries"


async def test_successful_sync_resets_the_failure_counter(isolated_engine, monkeypatch):
    """A recovery must clear the counter so the next outage can halt again."""
    eng = isolated_engine
    eng.order_manager.paper_mode = False
    eng._consecutive_sync_failures = 3

    eng.exchange.get_balance = AsyncMock(return_value={"result": []})
    eng.exchange.get_positions = AsyncMock(return_value=[])
    monkeypatch.setattr(eng.notifier, "send_alert", AsyncMock())

    await eng.sync_exchange_state()

    assert eng._consecutive_sync_failures == 0
