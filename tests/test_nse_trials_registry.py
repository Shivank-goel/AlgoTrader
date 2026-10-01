import json
import sqlite3
from pathlib import Path

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
