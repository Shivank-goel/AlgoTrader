"""Pre-registration audit for the three canonical NSE research families.

This intentionally refuses to create an ExperimentSpec when the verified
point-in-time dataset or an immutable strategy definition is unavailable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

FAMILIES = {
    "momentum_6_12": "6/12-month volatility-adjusted relative momentum",
    "donchian_breakout": "prior 55-day high breakout with 200-day trend filter",
    "residual_reversal": "five-day cross-sectional residual reversal (research-only)",
}


def _digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_preregistration(root: Path) -> list[dict]:
    """Return evidence-only pre-registration results; never run or register trials."""
    dataset = root / "data/fyers/daily_bars"
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
            db.execute("INSERT INTO nse_primary_trials VALUES(?,?,?,?,?,?)",
                       (spec["trial_id"], digest, "REGISTERED", now, None,
                        json.dumps(frozen, sort_keys=True), None))
        return frozen

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
    """Deterministic, offline execution skeleton using immutable daily artifacts."""
    trial = registry.get(trial_id)
    if trial["state"] != "REGISTERED":
        raise ValueError("ALREADY_EXECUTED or trial is not registered")
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
    # The Phase 12.1 proposals intentionally omit an explicit execution-price
    # contract; refusing here prevents inventing next-open/close semantics.
    if not trial["spec"].get("execution_price"):
        raise ValueError("INSUFFICIENT_EXECUTION_SEMANTICS")
    result = {"trial_id": trial_id, "state": "COMPLETED", "trades": [],
              "lookahead_status": "PASS", "historical_admission": "FAIL_HISTORICAL_ADMISSION",
              "failure_reasons": ["execution_adapter_requires_explicit_price_contract"]}
    registry.record_result(trial_id, result)
    return result
