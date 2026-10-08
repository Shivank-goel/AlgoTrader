import inspect
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.research.nse_trials import NseTrialRegistry, execute_registered_trial


def test_primary_trial_registration_persists_seven_fields_and_is_idempotent(tmp_path):
    database = tmp_path / "trials.sqlite3"
    registry = NseTrialRegistry(database)
    spec = json.loads(Path("config/research/nse_trials/momentum_6_12_v3.json").read_text())
    spec["trial_id"] = "trial-v3"

    registered = registry.register(spec, dataset_hash="a" * 64, now=123.0)
    assert registered["status"] == "REGISTERED"
    assert registry.register(spec, dataset_hash="a" * 64, now=456.0) == registered

    with sqlite3.connect(database) as db:
        row = db.execute("SELECT * FROM nse_primary_trials").fetchone()
    assert len(row) == 7
    assert row[0] == "trial-v3"
    assert row[2] == "REGISTERED"
    assert row[3] == 123.0
    assert row[4] is None
    assert json.loads(row[5])["dataset_hash"] == "a" * 64
    assert row[6] is None

    conflicting = dict(spec, strategy_family="donchian_breakout")
    with pytest.raises(ValueError, match="immutable"):
        registry.register(conflicting, dataset_hash="a" * 64)


@pytest.mark.parametrize("field", ["config_hash", "code_hash", "cost_model_hash"])
def test_registration_rejects_bad_provenance_without_inserting(tmp_path, field):
    database = tmp_path / "trials.sqlite3"
    registry = NseTrialRegistry(database)
    spec = json.loads(Path("config/research/nse_trials/momentum_6_12_v3.json").read_text())
    spec["trial_id"] = f"bad-{field}"
    spec[field] = "b" * (62 if field == "config_hash" else 64)
    with pytest.raises(ValueError):
        registry.register(spec, dataset_hash="a" * 64)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM nse_primary_trials").fetchone()[0] == 0


def test_unbound_historical_executor_cannot_consume_registered_trial(tmp_path):
    database = tmp_path / "trials.sqlite3"
    registry = NseTrialRegistry(database)
    spec = json.loads(Path("config/research/nse_trials/momentum_6_12_v3.json").read_text())
    spec["trial_id"] = "unbound-executor"
    registry.register(spec, dataset_hash="a" * 64)
    with pytest.raises(ValueError, match="HISTORICAL_EXECUTOR_PROVENANCE_MISSING"):
        execute_registered_trial(registry, spec["trial_id"], dataset_dir=tmp_path)
    assert registry.get(spec["trial_id"])["state"] == "REGISTERED"
    assert registry.get(spec["trial_id"])["result"] is None


def test_future_spec_requires_allocation_contract(tmp_path):
    registry = NseTrialRegistry(tmp_path / "trials.sqlite3")
    spec = json.loads(Path("config/research/nse_trials/momentum_6_12_v4.json").read_text())
    with pytest.raises(ValueError, match="allocation"):
        registry.register({k: v for k, v in spec.items() if k != "allocation_rule"}, dataset_hash="a" * 64)


def test_simulator_failure_is_registry_atomic(tmp_path, monkeypatch):
    from src.research.nse_executor_provenance import historical_executor_hash
    spec = json.loads(Path("config/research/nse_trials/momentum_6_12_v3.json").read_text())
    spec.update({"trial_id": "atomic-failure", "historical_executor_hash": historical_executor_hash()})
    registry = NseTrialRegistry(tmp_path / "trials.sqlite3")
    registry.register(spec, dataset_hash="a" * 64)
    artifact_path = tmp_path / "2026-01-01.json"
    artifact_path.write_text("synthetic")
    monkeypatch.setattr("src.fyers.data_readiness.daily_data_readiness", lambda *a, **k: {"data_ready": True, "dataset": {"artifact_sha256": "a" * 64}, "reasons": []})
    fake_bar = SimpleNamespace(benchmark=object(), missing_symbols=[], bars={})
    monkeypatch.setattr("src.fyers.daily_data.load_completed_bar", lambda path: fake_bar)
    monkeypatch.setattr("src.research.nse_engine.simulate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic failure")))
    with pytest.raises(RuntimeError, match="synthetic failure"):
        execute_registered_trial(registry, "atomic-failure", dataset_dir=tmp_path)
    row = registry.get("atomic-failure")
    assert row["state"] == "REGISTERED" and row["executed_at"] is None and row["result"] is None


def _prepare_registered_execution(tmp_path):
    from src.research.nse_executor_provenance import historical_executor_hash
    spec = json.loads(Path("config/research/nse_trials/momentum_6_12_v3.json").read_text())
    spec.update({"trial_id": "negative-result", "historical_executor_hash": historical_executor_hash()})
    registry = NseTrialRegistry(tmp_path / "trials.sqlite3")
    registry.register(spec, dataset_hash="a" * 64)
    (tmp_path / "2026-01-01.json").write_text("synthetic")
    return registry, spec


def test_legitimate_negative_result_persists_and_duplicate_is_rejected(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import src.research.nse_engine as engine
    registry, spec = _prepare_registered_execution(tmp_path)
    monkeypatch.setattr("src.fyers.data_readiness.daily_data_readiness", lambda *a, **k: {"data_ready": True, "dataset": {"artifact_sha256": "a" * 64}, "reasons": []})
    monkeypatch.setattr("src.fyers.daily_data.load_completed_bar", lambda path: SimpleNamespace(benchmark=object(), missing_symbols=[], bars={}))
    result = {"trade_count": 1, "net_return": -0.03, "historical_admission": "NOT_EVALUATED", "trades": []}
    monkeypatch.setattr(engine, "simulate", lambda *a, **k: result)
    execute_registered_trial(registry, spec["trial_id"], dataset_dir=tmp_path)
    stored = registry.get(spec["trial_id"])
    assert stored["state"] == "COMPLETED" and stored["result"] == result
    executed_at, original = stored["executed_at"], stored["result"]
    with pytest.raises(ValueError, match="ALREADY_EXECUTED"):
        execute_registered_trial(registry, spec["trial_id"], dataset_dir=tmp_path)
    stored_again = registry.get(spec["trial_id"])
    assert stored_again["executed_at"] == executed_at and stored_again["result"] == original


def test_historical_execution_has_no_operational_side_effect_reachability():
    source = inspect.getsource(execute_registered_trial)
    for forbidden in ("CandidateRegistry", "PaperBroker", "submit_order", "scheduled_intents", "live_enabled"):
        assert forbidden not in source


def test_failed_historical_execution_has_no_operational_side_effects(tmp_path, monkeypatch):
    registry, spec = _prepare_registered_execution(tmp_path)
    monkeypatch.setattr("src.fyers.data_readiness.daily_data_readiness", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("preflight failure")))
    with pytest.raises(RuntimeError, match="preflight failure"):
        execute_registered_trial(registry, spec["trial_id"], dataset_dir=tmp_path)
    row = registry.get(spec["trial_id"])
    assert row["state"] == "REGISTERED" and row["executed_at"] is None and row["result"] is None
