from datetime import datetime
from pathlib import Path

import pytest

from src.fyers.dashboard import dashboard_snapshot
from src.fyers.journal import Journal
from src.fyers.models import RuntimeConfig
from src.fyers.sessions import NseSessionCalendar, SessionPhase
from src.fyers.supervisor import RecorderSupervisor


def test_snapshot_projects_quotes_accounting_and_survives_optional_tables(tmp_path, monkeypatch):
    config = RuntimeConfig.load().model_copy(update={"database": "runtime.db"})
    journal = Journal(tmp_path / "runtime.db")
    with journal.db:
        journal.put("cash", 9000)
        journal.put("total_fees", 12)
        journal.put("paper_valuation", {"equity": 10010, "daily_pnl": 10})
    journal.event("tick", {"symbol": "NSE:SBIN-EQ", "bid_price": 99, "ask_price": 100,
                           "bid_size": 2, "ask_size": 3, "exch_feed_time": 100}, 101)
    journal.close()
    monkeypatch.setattr("src.fyers.dashboard.ROOT", tmp_path)
    monkeypatch.setattr("src.fyers.dashboard.readiness", lambda *_args, **_kwargs: {"blockers": [], "alerts": []})
    result = dashboard_snapshot(config, now=102)
    assert result["sections"]["quotes"][0]["latency_seconds"] == 1
    assert result["sections"]["accounting"]["cash"] == 9000
    assert not result["live_enabled"]


def test_snapshot_missing_journal_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr("src.fyers.dashboard.ROOT", tmp_path)
    monkeypatch.setattr("src.fyers.dashboard.readiness", lambda *_args, **_kwargs: {"blockers": ["journal_missing"]})
    result = dashboard_snapshot(RuntimeConfig.load().model_copy(update={"database": "missing.db"}))
    assert result["errors"] == ["journal unavailable"] and result["sections"] == {}


def test_supervisor_session_gate_is_fail_closed():
    config = RuntimeConfig.load().model_copy(update={"session_start": "09:10", "session_end": "15:35"})
    supervisor = RecorderSupervisor(config)
    assert supervisor.eligible(datetime(2026, 9, 21, 10, 0))
    assert not supervisor.eligible(datetime(2026, 9, 20, 10, 0))
    assert not supervisor.eligible(datetime(2026, 9, 21, 16, 0))


def test_session_calendar_reports_weekends_holidays_and_next_open():
    calendar = NseSessionCalendar(Path("config/nse_holidays.yaml"), "09:10", "15:35")
    weekend = calendar.state(datetime(2026, 9, 20, 10, 0))
    holiday = calendar.state(datetime(2026, 10, 2, 10, 0))
    before = calendar.state(datetime(2026, 9, 21, 8, 0))
    open_window = calendar.state(datetime(2026, 9, 21, 10, 0))
    closed = calendar.state(datetime(2026, 9, 21, 16, 0))
    assert weekend.phase is SessionPhase.WEEKEND
    assert weekend.next_session_start == "2026-09-21T09:10:00+05:30"
    assert holiday.phase is SessionPhase.HOLIDAY
    assert holiday.next_session_start == "2026-10-05T09:10:00+05:30"
    assert before.phase is SessionPhase.BEFORE_SESSION
    assert open_window.eligible and open_window.phase is SessionPhase.RECORDING_WINDOW
    assert closed.phase is SessionPhase.AFTER_SESSION


def test_session_calendar_wrong_year_fails_closed():
    calendar = NseSessionCalendar(Path("config/nse_holidays.yaml"), "09:10", "15:35")
    state = calendar.state(datetime(2027, 1, 4, 10, 0))
    assert not state.eligible
    assert state.phase is SessionPhase.CALENDAR_UNAVAILABLE


@pytest.mark.asyncio
async def test_supervisor_refuses_out_of_session_start(monkeypatch):
    supervisor = RecorderSupervisor()
    monkeypatch.setattr(supervisor, "eligible", lambda: False)
    with pytest.raises(ValueError, match="configured NSE session"):
        await supervisor.start()


@pytest.mark.asyncio
async def test_manual_stop_blocks_auto_restart_for_current_session(monkeypatch):
    supervisor = RecorderSupervisor()
    monkeypatch.setattr(supervisor, "_persist", lambda: None)
    supervisor.manual_stop_date = supervisor.session_state(datetime(2026, 9, 21, 10)).session_date
    monkeypatch.setattr(supervisor, "session_state",
                        lambda: supervisor.calendar.state(datetime(2026, 9, 21, 10)))
    started = False

    async def fake_start(*, manual=True):
        nonlocal started
        started = True

    monkeypatch.setattr(supervisor, "start", fake_start)
    await supervisor.run_once()
    assert not started


def test_supervisor_restart_budget_is_bounded():
    supervisor = RecorderSupervisor(
        RuntimeConfig.load().model_copy(update={"recorder_max_restarts": 3})
    )
    supervisor._restart_times = [97, 98, 99]
    assert supervisor._restart_allowed(100)
    supervisor._restart_times.append(100)
    assert not supervisor._restart_allowed(100)
