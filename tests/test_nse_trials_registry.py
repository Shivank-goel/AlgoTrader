import json
import sqlite3

import pytest

from src.research.nse_trials import NseTrialRegistry


def test_primary_trial_registration_persists_seven_fields_and_is_idempotent(tmp_path):
    database = tmp_path / "trials.sqlite3"
    registry = NseTrialRegistry(database)
    spec = {"trial_id": "trial-v3", "status": "PROPOSED", "strategy_family": "momentum_6_12"}

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
