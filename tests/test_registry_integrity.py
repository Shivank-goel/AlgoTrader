"""Research history cannot be silently erased by errors or competing writers."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.backtest.statistics import RegistryIntegrityError, Trial, TrialsRegistry


def test_stale_writers_merge_and_preserve_original_rows(tmp_path):
    path = tmp_path / "trials.json"
    first, second = TrialsRegistry(path), TrialsRegistry(path)
    first.record(Trial(name="original", sharpe=0.123456, n_obs=100))
    original = json.loads(path.read_text())["trials"]
    second.record(Trial(name="next", sharpe=0.2, n_obs=100))
    assert json.loads(path.read_text())["trials"][:1] == original
    assert TrialsRegistry(path).count == 2


def test_concurrent_writers_do_not_lose_trials(tmp_path):
    path = tmp_path / "trials.json"
    registries = [TrialsRegistry(path) for _ in range(12)]
    def write(i):
        registries[i].record(Trial(name=f"trial-{i}", sharpe=0.1, n_obs=100))
    with ThreadPoolExecutor(max_workers=6) as workers:
        list(workers.map(write, range(len(registries))))
    rows = json.loads(path.read_text())["trials"]
    assert len(rows) == len({r["name"] for r in rows}) == 12


@pytest.mark.parametrize("damage", ["corrupt", "deleted", "edited", "truncated"])
def test_existing_instance_refuses_damaged_history(tmp_path, damage):
    path = tmp_path / "trials.json"
    registry = TrialsRegistry(path)
    registry.record(Trial(name="old", sharpe=0.1, n_obs=100))
    if damage == "deleted":
        path.unlink()
    elif damage == "corrupt":
        path.write_text("not json")
    else:
        raw = json.loads(path.read_text())
        if damage == "edited":
            raw["trials"][0]["sharpe"] = 99
        else:
            raw["trials"], raw["count"] = [], 0
        path.write_text(json.dumps(raw))
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(RegistryIntegrityError):
        registry.record(Trial(name="new", sharpe=0.2, n_obs=100))
    assert (path.read_bytes() if path.exists() else None) == before
    assert registry.count == 1


@pytest.mark.parametrize("raw", [[], {}, {"count": 1, "trials": []}, {"count": True, "trials": [{}]}])
def test_invalid_schema_is_not_an_empty_registry(tmp_path, raw):
    path = tmp_path / "trials.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(RegistryIntegrityError):
        TrialsRegistry(path)


@pytest.mark.parametrize("kwargs", [{"sharpe": float("nan")}, {"n_obs": True}, {"n_obs": 1.5}, {"params": {"bad": float("inf")}}])
def test_invalid_new_trial_does_not_change_history(tmp_path, kwargs):
    path = tmp_path / "trials.json"
    registry = TrialsRegistry(path)
    registry.record(Trial(name="old", sharpe=0.1, n_obs=100))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        registry.record(Trial(**{"name": "bad", "sharpe": 0.1, "n_obs": 100, **kwargs}))
    assert path.read_bytes() == before


def test_failed_replace_preserves_file_and_in_memory_history(tmp_path, monkeypatch):
    path = tmp_path / "trials.json"
    registry = TrialsRegistry(path)
    registry.record(Trial(name="old", sharpe=0.1, n_obs=100))
    before = path.read_bytes()
    def fail_replace(self, target):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated disk failure"):
        registry.record(Trial(name="new", sharpe=0.2, n_obs=100))
    assert path.read_bytes() == before
    assert registry.count == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["trials.json", "trials.json.lock"]


def test_record_once_concurrent_retries_keep_first_timestamp(tmp_path):
    path = tmp_path / "trials.json"
    def publish(i):
        registry = TrialsRegistry(path)
        return registry.record_once(Trial(name="experiment:x", sharpe=.1, n_obs=100,
                                          params={"experiment_id": "x"}, recorded_at=f"timestamp-{i}"))
    with ThreadPoolExecutor(max_workers=6) as pool:
        returned = list(pool.map(publish, range(12)))
    assert len({r.recorded_at for r in returned}) == 1
    assert TrialsRegistry(path).count == 1


@pytest.mark.parametrize("change", [{"sharpe": .9}, {"name": "renamed"}, {"params": {"experiment_id": "other"}}])
def test_record_once_conflict_preserves_bytes(tmp_path, change):
    path = tmp_path / "trials.json"
    fields = {"name": "experiment:x", "sharpe": .1, "n_obs": 100, "params": {"experiment_id": "x"}}
    registry = TrialsRegistry(path)
    registry.record_once(Trial(**fields))
    before = path.read_bytes()
    with pytest.raises(RegistryIntegrityError, match="Conflicting"):
        registry.record_once(Trial(**{**fields, **change}))
    assert path.read_bytes() == before
