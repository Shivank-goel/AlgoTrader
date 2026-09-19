"""Offline experiment lifecycle, immutable evidence and reproducibility."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.backtest.statistics import Trial, TrialsRegistry, paired_bootstrap_ci
from src.research.models import ExperimentOutput, ExperimentSpec
from src.research.registry import ExperimentRegistry, verify_artifacts
from src.research.runner import publish_trial, resolve_interruption, run_once
from src.research.validation import block_bootstrap, walk_forward_splits


@pytest.fixture
def research_setup(tmp_path):
    dataset = tmp_path / "prices.csv"
    dataset.write_text("synthetic fixture only")
    begin = datetime(2020, 1, 1, tzinfo=timezone.utc)
    spec = ExperimentSpec(
        experiment_id="test", hypothesis="synthetic regression", strategy="fixture", strategy_version="1",
        dataset_version="fixture-1", git_sha="a" * 40, timeframe="day", universe=["TEST"],
        start=begin, end=begin + timedelta(days=200), features=[], parameters={}, search_space={},
        baseline="flat", cost_model={"fixture": True}, slippage={"fixture": True}, seed=3,
        execution_assumptions={"synthetic": True}, artifacts={"prices.csv": hashlib.sha256(dataset.read_bytes()).hexdigest()},
        environment={"python": "3.12"}, train_end=begin + timedelta(days=40),
        validation_end=begin + timedelta(days=80), holdout_end=begin + timedelta(days=120),
        max_drawdown=.2, bootstrap_block=5,
    )
    registry = ExperimentRegistry(tmp_path / "experiments.db")
    registry.campaign("test", 2)
    registry.register("test", spec)
    yield registry, spec, tmp_path
    registry.close()


def output(spec):
    return ExperimentOutput(timestamps=[spec.start + timedelta(days=i) for i in range(120)],
                            net_returns=np.random.default_rng(1).normal(.001, .01, 120).tolist(),
                            baseline_returns=[0.] * 120, turnover=[.1] * 120)


def test_registry_immutable_and_budgeted(research_setup):
    registry, spec, _ = research_setup
    registry.register("test", spec.model_copy(update={"experiment_id": "second"}))
    with pytest.raises(ValueError, match="budget"):
        registry.register("test", spec.model_copy(update={"experiment_id": "third"}))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        registry.db.execute("DELETE FROM experiments")
    registry.db.rollback()
    assert registry.spec("test") == spec


def test_manifest_detects_changed_data(research_setup):
    _, spec, root = research_setup
    verify_artifacts(root, spec)
    (root / "prices.csv").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        verify_artifacts(root, spec)


@pytest.mark.parametrize("name", ["../outside", ".env", ".git/config"])
def test_manifest_rejects_unsafe_paths(research_setup, name):
    _, spec, root = research_setup
    with pytest.raises(ValueError):
        verify_artifacts(root, spec.model_copy(update={"artifacts": {name: "x"}}))


def test_run_once_and_publication_recovery(research_setup):
    registry, spec, root = research_setup
    path = root / "trials.json"
    report = run_once(registry, spec.experiment_id, root, path, output)
    assert report["qualification"] is False
    assert TrialsRegistry(path).count == 1
    assert publish_trial(registry, spec.experiment_id, path) == report
    with pytest.raises(ValueError, match="transition"):
        run_once(registry, spec.experiment_id, root, path, output)
    assert TrialsRegistry(path).count == 1


def test_failure_is_preserved_and_counted(research_setup):
    registry, spec, root = research_setup
    def fail(_):
        raise ValueError("sensitive error text must not be stored")
    with pytest.raises(ValueError):
        run_once(registry, spec.experiment_id, root, root / "trials.json", fail)
    result = registry.evidence(spec.experiment_id, "result")
    assert result == {"status": "failed", "error_type": "ValueError"}
    assert TrialsRegistry(root / "trials.json").count == 1


def test_recover_after_output_saved_before_publication(research_setup):
    registry, spec, root = research_setup
    registry.event("test", "started", {})
    registry.event("test", "result", {"status": "completed", "output": output(spec).model_dump(mode="json")})
    report = publish_trial(registry, "test", root / "trials.json")
    assert report["full_trial_count"] == 1


def test_paired_bootstrap_keeps_matching_rows():
    estimate, low, high = paired_bootstrap_ci([1, float("nan"), 3, 4], [0, 2, float("nan"), 3])
    assert estimate == low == high == 1
    with pytest.raises(ValueError):
        paired_bootstrap_ci([1], [1, 2])


def test_walk_forward_and_bootstrap_are_deterministic():
    splits = walk_forward_splits(100, 30, 10, gap=2)
    assert len(splits) == 6
    assert all(train.stop + 2 == test.start for train, test in splits)
    assert all(a[1].stop <= b[1].start for a, b in zip(splits, splits[1:]))
    values = [0.01, -0.02, .03] * 10
    assert block_bootstrap(values, block=3, seed=4) == block_bootstrap(values, block=3, seed=4)


@pytest.mark.parametrize("kind,payload", [
    ("result", {"status": "failed", "error_type": "Error"}),
    ("published", {"qualification": False}), ("unknown", {}), ("started", {"unexpected": True}),
])
def test_invalid_event_order_does_not_persist(research_setup, kind, payload):
    registry, _, _ = research_setup
    with pytest.raises(ValueError):
        registry.event("test", kind, payload)
    assert registry.status()[0]["state"] == "registered"


@pytest.mark.parametrize("payload", [
    {"status": "completed"}, {"status": "failed"},
    {"status": "made-up", "error_type": "Error"},
    {"status": "failed", "error_type": "Error", "output": {}},
])
def test_invalid_result_is_not_persisted_or_published(research_setup, payload):
    registry, _, root = research_setup
    registry.event("test", "started", {})
    with pytest.raises(ValueError):
        registry.event("test", "result", payload)
    with pytest.raises(ValueError, match="durable result"):
        publish_trial(registry, "test", root / "trials.json")
    assert not (root / "trials.json").exists()
    assert registry.status()[0]["state"] == "unfinished"


def test_result_dates_validated_before_persistence(research_setup):
    registry, spec, _ = research_setup
    registry.event("test", "started", {})
    bad = output(spec).model_dump(mode="json")
    bad["timestamps"][-1] = (spec.end + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="outside"):
        registry.event("test", "result", {"status": "completed", "output": bad})
    assert registry.evidence("test", "result") is None


def test_interruption_blocks_campaign_until_counted(research_setup):
    registry, spec, root = research_setup
    registry.register("test", spec.model_copy(update={"experiment_id": "next"}))
    def interrupt(_):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run_once(registry, "test", root, root / "trials.json", interrupt)
    with pytest.raises(ValueError, match="unresolved"):
        run_once(registry, "next", root, root / "trials.json", output)
    result = resolve_interruption(registry, "test", root / "trials.json", operator="tester", reason="worker interrupted")
    assert result["status"] == "failed"
    assert result["full_trial_count"] == 1
    assert resolve_interruption(registry, "test", root / "trials.json", operator="tester", reason="worker interrupted") == result
    run_once(registry, "next", root, root / "trials.json", output)
    assert TrialsRegistry(root / "trials.json").count == 2


def test_running_worker_cannot_be_resolved(research_setup):
    registry, _, root = research_setup
    registry.event("test", "started", {})
    with registry.execution_lock("test"):
        with pytest.raises(ValueError, match="worker is active"):
            resolve_interruption(registry, "test", root / "trials.json", operator="tester", reason="wrong")
    assert registry.evidence("test", "result") is None


@pytest.mark.parametrize("boundary", ["before_trial", "after_trial", "before_publication", "after_publication"])
def test_crash_recovery_never_reruns_or_duplicates(research_setup, monkeypatch, boundary):
    registry, spec, root = research_setup
    path = root / "trials.json"
    count = 0
    def compute(s):
        nonlocal count
        count += 1
        return output(s)
    original_record = TrialsRegistry.record_once
    original_event = registry.event
    def record(self, trial):
        if boundary == "before_trial":
            raise OSError("injected before trial")
        stored = original_record(self, trial)
        if boundary == "after_trial":
            raise OSError("injected after trial")
        return stored
    def event(experiment_id, kind, payload):
        if kind == "published" and boundary == "before_publication":
            raise OSError("injected before publication")
        original_event(experiment_id, kind, payload)
        if kind == "published" and boundary == "after_publication":
            raise OSError("injected after publication")
    monkeypatch.setattr(TrialsRegistry, "record_once", record)
    monkeypatch.setattr(registry, "event", event)
    with pytest.raises(OSError):
        run_once(registry, "test", root, path, compute)
    monkeypatch.setattr(TrialsRegistry, "record_once", original_record)
    monkeypatch.setattr(registry, "event", original_event)
    report = publish_trial(registry, "test", path)
    assert count == 1 and TrialsRegistry(path).count == 1
    assert report == publish_trial(registry, "test", path)


def test_concurrent_publication_is_exactly_once(research_setup):
    registry, spec, root = research_setup
    registry.event("test", "started", {})
    registry.event("test", "result", {"status": "completed", "output": output(spec).model_dump(mode="json")})
    def publish(_):
        worker = ExperimentRegistry(root / "experiments.db")
        try:
            return publish_trial(worker, "test", root / "trials.json")
        finally:
            worker.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(publish, range(8)))
    assert all(r == reports[0] for r in reports)
    assert TrialsRegistry(root / "trials.json").count == 1


def test_publication_hashes_and_staleness(research_setup):
    registry, spec, root = research_setup
    path = root / "trials.json"
    published = run_once(registry, "test", root, path, output)
    assert published["evidence"]["trials_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert registry.report_view("test", path)["freshness"] == "current"
    TrialsRegistry(path).record(Trial(name="later", sharpe=.1, n_obs=100))
    assert registry.report_view("test", path)["freshness"] == "stale"
    assert registry.evidence("test", "published") == published
    assert publish_trial(registry, "test", path) == published


def test_legacy_import_reads_one_validated_snapshot(research_setup, monkeypatch):
    registry, _, root = research_setup
    path = root / "trials.json"
    TrialsRegistry(path).record(Trial(name="legacy", sharpe=.1, n_obs=100))
    before = path.read_bytes()
    from pathlib import Path
    original = Path.read_bytes
    calls = []
    def read(self):
        if self == path:
            calls.append(self)
            assert len(calls) == 1
        return original(self)
    monkeypatch.setattr(Path, "read_bytes", read)
    assert registry.import_legacy(path) == 1
    stored = registry.db.execute("SELECT payload FROM legacy").fetchone()[0]
    assert json.loads(stored) == json.loads(before)
    assert original(path) == before


def test_existing_bad_events_survive_migration_and_are_reported(research_setup):
    registry, _, root = research_setup
    registry.db.execute("INSERT INTO events(experiment,kind,payload,created) VALUES(?,?,?,?)",
                        ("test", "result", '{"status":"completed"}', registry.now()))
    registry.db.commit()
    before = registry.db.execute("SELECT * FROM events").fetchall()
    registry.db.execute("PRAGMA user_version=0")
    migrated = ExperimentRegistry(root / "experiments.db")
    try:
        assert migrated.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert migrated.db.execute("SELECT * FROM events").fetchall() == before
        assert migrated.status()[0]["state"] == "inconsistent"
        with pytest.raises(ValueError, match="inconsistent"):
            publish_trial(migrated, "test", root / "trials.json")
    finally:
        migrated.close()


def test_unpublished_result_blocks_next_campaign_run(research_setup):
    registry, spec, root = research_setup
    registry.register("test", spec.model_copy(update={"experiment_id": "next"}))
    registry.event("test", "started", {})
    registry.event("test", "result", {"status": "completed", "output": output(spec).model_dump(mode="json")})
    with pytest.raises(ValueError, match="unresolved"):
        run_once(registry, "next", root, root / "trials.json", output)
    publish_trial(registry, "test", root / "trials.json")
    assert run_once(registry, "next", root, root / "trials.json", output)["full_trial_count"] == 2


def test_unbound_historical_report_is_preserved(research_setup):
    registry, spec, root = research_setup
    registry.event("test", "started", {})
    registry.event("test", "result", {"status": "completed", "output": output(spec).model_dump(mode="json")})
    historical = {"qualification": False, "full_trial_count": 206}
    registry.db.execute("INSERT INTO events(experiment,kind,payload,created) VALUES(?,?,?,?)",
                        ("test", "published", json.dumps(historical), registry.now()))
    registry.db.commit()
    view = registry.report_view("test", root / "trials.json")
    assert view["freshness"] == "unverifiable"
    assert view["publication"] == historical
    assert not registry.status()[0]["publication_bound"]


def test_cli_status_resolution_and_report(research_setup):
    from click.testing import CliRunner
    from src.research.cli import research
    registry, _, root = research_setup
    registry.event("test", "started", {})
    runner = CliRunner()
    base = ["--database", str(root / "experiments.db")]
    status = runner.invoke(research, base + ["status", "--campaign", "test"])
    assert status.exit_code == 0
    assert json.loads(status.output)["experiments"][0]["state"] == "unfinished"
    resolve = runner.invoke(research, base + ["resolve-interruption", "test", "--operator", "tester",
        "--reason", "stopped", "--trials", str(root / "trials.json")])
    assert resolve.exit_code == 0, resolve.output
    report = runner.invoke(research, base + ["report", "test", "--trials", str(root / "trials.json")])
    assert report.exit_code == 0
    assert json.loads(report.output)["freshness"] == "current"


def test_forged_publication_bindings_are_rejected(research_setup):
    registry, spec, _ = research_setup
    registry.event("test", "started", {})
    registry.event("test", "result", {"status": "completed", "output": output(spec).model_dump(mode="json")})
    with pytest.raises(ValueError, match="frozen evidence"):
        registry.event("test", "published", {"qualification": False, "full_trial_count": 1,
                        "evidence": {"spec_sha256": "a" * 64, "result_sha256": "b" * 64,
                                     "trials_sha256": "c" * 64, "published_at": registry.now()}})
    assert registry.evidence("test", "published") is None
