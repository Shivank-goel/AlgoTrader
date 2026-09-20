"""One-shot registered experiments with durable output and explicit recovery.

The callable is trusted offline Python, not an LLM or a security sandbox.
Interrupted runs cannot be rerun automatically under the same experiment ID.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from src.backtest.statistics import Trial, TrialsRegistry, sharpe_ratio, t_statistic
from src.research.models import ExperimentOutput, ExperimentResult, ExperimentSpec
from src.research.registry import ExperimentRegistry, canonical, evidence_hash, verify_artifacts
from src.research.validation import report


def publish_trial(registry: ExperimentRegistry, experiment_id: str, trials_path: Path) -> dict:
    """Recover a stored result without rerunning the experiment or double-counting.

    A publication lock serializes the cross-file recovery boundary. record_once
    also holds the ordinary append lock, so all trial writers cooperate.
"""
    import fcntl
    trials_path = trials_path.resolve()
    lock_path = trials_path.with_suffix(trials_path.suffix + ".publish.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        audit = next((r for r in registry.status() if r["experiment_id"] == experiment_id), None)
        if audit is None or audit["state"] == "inconsistent":
            raise ValueError("Unknown or inconsistent experiment; inspect research status")
        existing = registry.evidence(experiment_id, "published")
        if existing:
            return existing
        result = registry.evidence(experiment_id, "result")
        if result is None:
            raise ValueError("Experiment has no durable result; investigate interruption")
        spec = registry.spec(experiment_id)
        validated = ExperimentResult.model_validate(result)
        validated.validate_spec(spec)
        values = validated.output.net_returns if validated.output is not None else []
        spec_hash = evidence_hash(spec.model_dump(mode="json"))
        result_hash = evidence_hash(result)
        trial = Trial(name=f"experiment:{experiment_id}", family=spec.strategy,
                      sharpe=sharpe_ratio(values), n_obs=len(values),
                      net_return_pct=sum(values) * 100, t_stat=t_statistic(values),
                      params={"experiment_id": experiment_id, "status": result["status"], "parameters": spec.parameters,
                              "spec_sha256": spec_hash, "result_sha256": result_hash})
        trials = TrialsRegistry(trials_path)
        trials.record_once(trial)
        if result["status"] == "completed":
            payload = report(spec, ExperimentOutput.model_validate(result["output"]),
                             n_trials=trials.count, trial_sharpe_std=trials.sharpe_std(),
                             campaign_trials=registry.campaign_trial_count(experiment_id))
        else:
            payload = {"qualification": False, "status": "failed", "error_type": result["error_type"],
                       "full_trial_count": trials.count}
        payload["evidence"] = {"spec_sha256": spec_hash, "result_sha256": result_hash,
                               "trials_sha256": trials.snapshot_sha256, "published_at": registry.now()}
        registry.event(experiment_id, "published", payload)
        return payload


def run_once(registry: ExperimentRegistry, experiment_id: str, root: Path, trials_path: Path,
             backtest: Callable[[ExperimentSpec], ExperimentOutput], *, holdout: bool = False) -> dict:
    with registry.execution_lock(experiment_id):
        return _run_once(registry, experiment_id, root, trials_path, backtest, holdout=holdout)


def _run_once(registry: ExperimentRegistry, experiment_id: str, root: Path, trials_path: Path,
              backtest: Callable[[ExperimentSpec], ExperimentOutput], *, holdout: bool = False) -> dict:
    spec = registry.spec(experiment_id)
    if (spec.evaluation_stage == "holdout") != holdout:
        raise ValueError("Use research evaluate-holdout for holdout experiments")
    frozen_spec = canonical(spec.model_dump(mode="json"))
    verify_artifacts(root, spec)
    # Validate the full trial history before spending a registered experiment.
    TrialsRegistry(trials_path)
    registry.event(experiment_id, "started", {})
    try:
        output = ExperimentOutput.model_validate(backtest(spec))
        if canonical(spec.model_dump(mode="json")) != frozen_spec:
            raise ValueError("Experiment specification mutated during execution")
        verify_artifacts(root, spec)
        if output.timestamps[0] < spec.start or output.timestamps[-1] > spec.end:
            raise ValueError("Output outside registered dates")
        if len(output.net_returns) < spec.bootstrap_block:
            raise ValueError("Output shorter than registered bootstrap block")
    except Exception as exc:
        # Do not persist exception text: trusted code may still include credentials.
        registry.event(experiment_id, "result", {"status": "failed", "error_type": type(exc).__name__})
        publish_trial(registry, experiment_id, trials_path)
        raise
    registry.event(experiment_id, "result", {"status": "completed", "output": output.model_dump(mode="json")})
    return publish_trial(registry, experiment_id, trials_path)


def resolve_interruption(registry: ExperimentRegistry, experiment_id: str, trials_path: Path,
                         *, operator: str, reason: str) -> dict:
    """Explicitly count an abandoned attempt; never rerun it or fabricate returns."""
    with registry.execution_lock(experiment_id):
        if registry.evidence(experiment_id, "started") is None:
            raise ValueError("Experiment has not started")
        result = registry.evidence(experiment_id, "result")
        if result is None:
            registry.event(experiment_id, "result", {
                "status": "failed", "error_type": "InterruptedExperiment",
                "resolution": {"operator": operator, "reason": reason},
            })
        elif result.get("error_type") != "InterruptedExperiment" or result.get("resolution") != {"operator": operator, "reason": reason}:
            raise ValueError("Experiment already has a result; recover its publication")
        return publish_trial(registry, experiment_id, trials_path)
