import numpy as np
import pandas as pd
from types import SimpleNamespace
import yaml

from src.fyers.journal import Journal
from src.fyers.models import RuntimeConfig
from src.shadow.continuous import ContinuousStrategyLab, StrategyLabConfig


def setup_lab(tmp_path, monkeypatch):
    candidate = {
        "id": "momentum_fixture_v1", "kind": "momentum",
        "symbols": ["NSE:SBIN-EQ", "NSE:INFY-EQ"], "interval_seconds": 60,
        "lookback_intervals": 2, "horizon_intervals": 1, "top_n": 1,
        "capital_inr": 2000,
    }
    lab_path = tmp_path / "lab.yaml"
    lab_path.write_text(yaml.safe_dump({"enabled": True, "candidates": [candidate]}))
    runtime = RuntimeConfig.load().model_copy(update={
        "database": "runtime.db", "strategy_lab_file": "lab.yaml", "stale_seconds": 30,
    })
    monkeypatch.setattr("src.shadow.continuous.ROOT", tmp_path)
    return ContinuousStrategyLab(runtime), runtime


def market(journal, now, sbin, infy, *, opened=True):
    with journal.db:
        journal.put("account_check", {"market_open": opened, "at": now})
    for symbol, price in [("NSE:SBIN-EQ", sbin), ("NSE:INFY-EQ", infy)]:
        journal.event("tick", {"symbol": symbol, "bid_price": price - 0.05,
                      "ask_price": price + 0.05, "bid_size": 100, "ask_size": 100,
                      "exch_feed_time": now}, now)


def test_strategy_lab_records_idempotent_forward_results(tmp_path, monkeypatch):
    lab, runtime = setup_lab(tmp_path, monkeypatch)
    journal = Journal(tmp_path / runtime.database)
    market(journal, 961, 100, 100)
    journal.close()
    assert not lab.run_once(now=961)["candidates"][0]["decision_created"]
    journal = Journal(tmp_path / runtime.database)
    market(journal, 1021, 102, 101)
    journal.close()
    lab.run_once(now=1021)
    journal = Journal(tmp_path / runtime.database)
    market(journal, 1081, 104, 101)
    journal.close()
    decision = lab.run_once(now=1081)
    assert decision["candidates"][0]["decision_created"]
    assert decision["execution"] == "observation_only" and not decision["live_enabled"]
    journal = Journal(tmp_path / runtime.database)
    market(journal, 1141, 106, 101)
    journal.close()
    result = lab.run_once(now=1141)
    assert result["candidates"][0]["metrics"]["completed"] == 1
    lab.run_once(now=1141)
    journal = Journal(tmp_path / runtime.database)
    assert journal.db.execute("SELECT COUNT(*) FROM strategy_observations").fetchone()[0] == 2
    assert journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='strategy_evaluated'").fetchone()[0] == 1
    journal.close()


def test_strategy_lab_waits_for_open_market_and_quotes(tmp_path, monkeypatch):
    lab, runtime = setup_lab(tmp_path, monkeypatch)
    journal = Journal(tmp_path / runtime.database)
    market(journal, 1000, 100, 100, opened=False)
    journal.close()
    result = lab.run_once(now=1000)
    assert result["candidates"][0]["state"] == "waiting_for_market"
    assert not (tmp_path / runtime.database).read_bytes().count(b"strategy_decision")


def test_strategy_lab_config_rejects_duplicate_candidate_ids():
    candidate = {"id": "same", "kind": "momentum", "symbols": ["NSE:A-EQ", "NSE:B-EQ"],
                 "interval_seconds": 60, "lookback_intervals": 2, "horizon_intervals": 1,
                 "top_n": 1, "capital_inr": 1000}
    try:
        StrategyLabConfig.model_validate({"enabled": True, "candidates": [candidate, candidate]})
    except ValueError as exc:
        assert "candidate IDs must be unique" in str(exc)
    else:
        raise AssertionError("duplicate candidate IDs accepted")


def test_overdue_strategy_observation_expires_instead_of_using_late_price(tmp_path, monkeypatch):
    lab, runtime = setup_lab(tmp_path, monkeypatch)
    for now, sbin, infy in [(961, 100, 100), (1021, 102, 101), (1081, 104, 101)]:
        journal = Journal(tmp_path / runtime.database)
        market(journal, now, sbin, infy)
        journal.close()
        lab.run_once(now=now)
    journal = Journal(tmp_path / runtime.database)
    market(journal, 1201, 110, 101)
    journal.close()
    result = lab.run_once(now=1201)
    metrics = result["candidates"][0]["metrics"]
    assert metrics["completed"] == 0 and metrics["expired"] == 1


def test_regime_decision_is_frozen_once_per_day(tmp_path, monkeypatch):
    runtime = RuntimeConfig.load().model_copy(update={"database": "runtime.db"})
    lab = ContinuousStrategyLab(runtime)
    monkeypatch.setattr("src.shadow.continuous.ROOT", tmp_path)
    index = pd.date_range("2025-01-01", periods=300, freq="B")
    base = 100 * np.exp(np.arange(300) * .001)
    synthetic = pd.DataFrame({member.symbol: base for member in lab.universe.members}, index=index)
    benchmark = pd.Series(base, index=index, name="NSE:NIFTY50-INDEX")
    artifact = SimpleNamespace(session_date=index[-1].date(), content_sha256="a" * 64,
                               missing_symbols=[])
    monkeypatch.setattr("src.shadow.continuous.completed_bar_panel",
                        lambda *_args, **_kwargs: (synthetic, synthetic, benchmark, [artifact]))
    monkeypatch.setattr(lab, "_evaluate_regime_families", lambda *_: None)
    monkeypatch.setattr(lab, "_record_family_previews", lambda *_: None)
    journal = Journal(tmp_path / runtime.database)
    now = 1_800_000_000
    with journal.db:
        journal.put("account_check", {"market_open": True, "at": now})
    for member in lab.universe.members:
        journal.event("tick", {"symbol": member.symbol, "bid_price": 99.9,
                      "ask_price": 100.1, "bid_size": 100, "ask_size": 100,
                      "exch_feed_time": now}, now)
    journal.close()
    first = lab.run_once(now=now)["selector"]
    second = lab.run_once(now=now + 1)["selector"]
    assert first == second
    journal = Journal(tmp_path / runtime.database)
    assert journal.db.execute("SELECT COUNT(*) FROM regime_decisions").fetchone()[0] == 1
    journal.close()
