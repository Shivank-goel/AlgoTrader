"""Pre-registration audit for the three canonical NSE research families.

This intentionally refuses to create an ExperimentSpec when the verified
point-in-time dataset or an immutable strategy definition is unavailable.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

FAMILIES = {
    "momentum_6_12": "6/12-month volatility-adjusted relative momentum",
    "donchian_breakout": "prior 55-day high breakout with 200-day trend filter",
    "residual_reversal": "five-day cross-sectional residual reversal (research-only)",
}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_preregistration(root: Path) -> list[dict]:
    """Return evidence-only pre-registration results; never run or register trials."""
    # Resolve through the same runtime configuration used by dashboard
    # readiness (hyphenated ``daily-bars`` on the VM), never a cwd-relative
    # duplicate assumption.
    try:
        import yaml
        runtime = yaml.safe_load((root / "config/fyers_runtime.yaml").read_text()) or {}
        dataset = root / str(runtime.get("daily_bars_directory", "data/fyers/daily-bars"))
    except (OSError, TypeError, yaml.YAMLError):
        dataset = root / "data/fyers/daily-bars"
    universe = root / "config/nse_forward_universe.yaml"
    costs = root / "config/fyers_costs.yaml"
    source = root / "src/strategies/nse_regime_selector.py"
    results = []
    for family, definition in FAMILIES.items():
        reasons = []
        if not dataset.exists():
            reasons.append("verified_DATA_READY_daily_dataset_unavailable")
        if not universe.is_file():
            reasons.append("universe_artifact_missing")
        if not costs.is_file():
            reasons.append("cost_model_artifact_missing")
        if not source.is_file():
            reasons.append("strategy_source_missing")
        # No trial is silently selected or invented in this phase.
        reasons.append("no_preselected_registered_trial")
        results.append({"strategy_family": family, "definition": definition,
                        "strategy_source": str(source.relative_to(root)),
                        "code_hash": _digest(source), "config_hash": _digest(universe),
                        "cost_hash": _digest(costs), "status": "NOT_READY",
                        "reasons": reasons})
    return results


class NseTrialRegistry:
    """Immutable VM-bound primary trial registry. Results are never overwritten."""
    def __init__(self, database: Path):
        self.database = database

    def _init(self, db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS nse_primary_trials (
            trial_id TEXT PRIMARY KEY, spec_hash TEXT NOT NULL, state TEXT NOT NULL,
            registered_at REAL NOT NULL, executed_at REAL, spec TEXT NOT NULL,
            result TEXT)""")

    def register(self, spec: dict, *, dataset_hash: str, now: float | None = None) -> dict:
        if spec.get("status") != "PROPOSED":
            raise ValueError("only PROPOSED specs may be registered")
        if not dataset_hash:
            raise ValueError("DATASET_MISMATCH: verified dataset hash is required")
        frozen = dict(spec, dataset_hash=dataset_hash, status="REGISTERED")
        digest = hashlib.sha256(json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        now = time.time() if now is None else now
        with sqlite3.connect(self.database) as db:
            self._init(db)
            row = db.execute("SELECT spec_hash,spec FROM nse_primary_trials WHERE trial_id=?", (spec["trial_id"],)).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("registered trial specification is immutable")
                return json.loads(row[1])
            self._validate_provenance(spec)
            db.execute("INSERT INTO nse_primary_trials VALUES(?,?,?,?,?,?,?)",
                       (spec["trial_id"], digest, "REGISTERED", now, None,
                        json.dumps(frozen, sort_keys=True), None))
        return frozen

    @staticmethod
    def _validate_provenance(spec: dict) -> None:
        """Validate frozen provenance against the repository's canonical inputs."""
        required = ("code_hash", "config_hash", "cost_model_hash")
        if any(not isinstance(spec.get(key), str) or not _HEX64.fullmatch(spec[key]) for key in required):
            raise ValueError("invalid specification provenance hash")
        from src.fyers.models import ROOT
        source = ROOT / "src/strategies/nse_regime_selector.py"
        universe_path = ROOT / "config/nse_forward_universe.yaml"
        costs = ROOT / "config/fyers_costs.yaml"
        expected = {"code_hash": _digest(source), "config_hash": _digest(universe_path),
                    "cost_model_hash": _digest(costs)}
        for key, value in expected.items():
            if spec[key] != value:
                raise ValueError(f"{key} does not match canonical research input")
        if spec.get("strategy_family") not in FAMILIES or not isinstance(spec.get("strategy_version"), str):
            raise ValueError("invalid strategy family/version semantics")
        try:
            from src.fyers.universe import ForwardUniverse
            universe = ForwardUniverse.load(universe_path)
            if spec.get("universe_id") != universe.universe_id:
                raise ValueError("universe_id does not match canonical universe")
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("invalid canonical universe") from exc
        requirement = spec.get("dataset_requirement")
        if not isinstance(requirement, dict) or requirement.get("path") != "data/fyers/daily-bars" \
                or requirement.get("state") != "DATA_READY" \
                or requirement.get("benchmark") != "NSE:NIFTY50-INDEX":
            raise ValueError("dataset requirement does not match canonical inputs")

    def get(self, trial_id: str) -> dict:
        with sqlite3.connect(self.database) as db:
            self._init(db)
            row = db.execute("SELECT * FROM nse_primary_trials WHERE trial_id=?", (trial_id,)).fetchone()
            if not row:
                raise ValueError("trial is not registered")
            return {"trial_id": row[0], "spec_hash": row[1], "state": row[2],
                    "registered_at": row[3], "executed_at": row[4],
                    "spec": json.loads(row[5]), "result": json.loads(row[6]) if row[6] else None}

    def record_result(self, trial_id: str, result: dict, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with sqlite3.connect(self.database) as db:
            self._init(db)
            row = db.execute("SELECT state FROM nse_primary_trials WHERE trial_id=?", (trial_id,)).fetchone()
            if not row or row[0] != "REGISTERED":
                raise ValueError("trial must be registered and unexecuted")
            db.execute("UPDATE nse_primary_trials SET state='COMPLETED',executed_at=?,result=? WHERE trial_id=?",
                       (now, json.dumps(result, sort_keys=True), trial_id))


def execute_registered_trial(registry: NseTrialRegistry, trial_id: str, *, dataset_dir: Path) -> dict:
    """Execute only with an explicitly provenance-bound historical engine.

    Existing registrations predate the executor and therefore cannot safely
    consume newly implemented accounting or fill semantics.
    """
    trial = registry.get(trial_id)
    if trial["state"] != "REGISTERED":
        raise ValueError("ALREADY_EXECUTED or trial is not registered")
    if not trial["spec"].get("historical_executor_hash"):
        raise ValueError("HISTORICAL_EXECUTOR_PROVENANCE_MISSING")
    from src.fyers.data_readiness import daily_data_readiness
    from src.fyers.models import ROOT, RuntimeConfig

    try:
        runtime = RuntimeConfig.load(ROOT / "config/fyers_runtime.yaml")
        readiness = daily_data_readiness(runtime, directory=dataset_dir)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("DATASET_NOT_READY") from exc
    if not readiness["data_ready"]:
        raise ValueError("DATASET_NOT_READY: " + ",".join(readiness["reasons"]))
    current_hash = readiness["dataset"]["artifact_sha256"]
    if trial["spec"].get("dataset_hash") != current_hash:
        raise ValueError("DATASET_MISMATCH")
    artifacts = sorted(dataset_dir.glob("????-??-??.json"))
    if not artifacts:
        raise ValueError("DATASET_UNAVAILABLE_ON_THIS_HOST")
    from src.fyers.daily_data import load_completed_bar
    bars = [load_completed_bar(path) for path in artifacts]
    if any(bar.benchmark is None or bar.missing_symbols for bar in bars):
        raise ValueError("DATA_INCOMPLETE")
    family = trial["spec"].get("strategy_family")
    if family not in FAMILIES:
        raise ValueError("UNSUPPORTED_STRATEGY")
    execution = trial["spec"].get("execution_price")
    exit_rule = execution.get("exit_rule") if isinstance(execution, dict) else None
    if not isinstance(exit_rule, dict) or exit_rule.get("type") != "FIXED_HOLDING_PERIOD" \
            or exit_rule.get("sessions") != 1:
        raise ValueError("INSUFFICIENT_EXECUTION_SEMANTICS")
    if not isinstance(execution, dict) or execution.get("entry_timing") != "NEXT_SESSION_OPEN" \
            or execution.get("entry_price_field") != "open" \
            or execution.get("exit_timing") != "NEXT_SESSION_OPEN" \
            or execution.get("exit_price_field") != "open":
        raise ValueError("INSUFFICIENT_EXECUTION_SEMANTICS")
    raise ValueError("HISTORICAL_EXECUTOR_NOT_IMPLEMENTED")
