"""Immutable strategy-candidate registration and shadow activation gates.

This module deliberately does not qualify strategies.  Registration only freezes
the exact specification and permits prospective shadow observations.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

FAMILIES = ("momentum_6_12", "donchian_breakout", "residual_reversal")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def generate_candidate_specs(*, trials_path: Path, output_dir: Path | None = None) -> list[dict[str, Any]]:
    """Build truthful specs from explicitly registered evidence only.

    Missing provenance is reported, never replaced with a placeholder hash.
    """
    try:
        registry = json.loads(trials_path.read_text())
        trials = registry.get("trials", [])
    except (OSError, ValueError, TypeError):
        trials = []
    results = []
    for family in FAMILIES:
        matches = [t for t in trials if t.get("family") == family]
        if len(matches) != 1:
            results.append({"strategy_family": family,
                            "status": "MULTIPLE_UNRESOLVED_TRIALS" if len(matches) > 1 else "NOT_READY_HISTORICAL_VALIDATION",
                            "reason": "exactly one preselected registered trial is required",
                            "trial_count": len(matches)})
            continue
        # A trial row alone is not sufficient: require all immutable evidence
        # to be present in the registered row rather than fabricating hashes.
        trial = matches[0]
        spec = {"candidate_id": f"{family}_{trial.get('name')}",
                "strategy_family": family, "strategy_version": trial.get("version", "v1"),
                "lifecycle": "CANDIDATE", "shadow_enabled": False,
                "supported_sides": trial.get("supported_sides"),
                "eligible_regimes": trial.get("eligible_regimes"),
                "protection_required": True, "protection_method": trial.get("protection_method"),
                "protection_inputs": trial.get("protection_inputs"),
                "trial_id": trial.get("name"), "code_sha256": trial.get("code_sha256"),
                "config_sha256": trial.get("config_sha256"), "data_sha256": trial.get("data_sha256"),
                "costs_sha256": trial.get("costs_sha256"),
                "point_in_time_safe": trial.get("point_in_time_safe") is True,
                "historical_validation_passed": trial.get("historical_validation_passed") is True}
        audit = audit_candidate(spec, trials_path=trials_path)
        result = {"strategy_family": family, "spec": spec, "audit": audit}
        if audit["status"] == "READY_TO_REGISTER" and output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{family}_{spec['strategy_version']}.json"
            path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
            result["spec_file"] = str(path)
        results.append(result)
    return results


def audit_candidate(spec: dict[str, Any], *, trials_path: Path) -> dict[str, Any]:
    """Return a fail-closed audit; no state is changed."""
    family = spec.get("strategy_family") or spec.get("family")
    reasons: list[str] = []
    if family not in FAMILIES:
        reasons.append("unknown_strategy_family")
    for key in ("candidate_id", "strategy_version", "code_sha256", "config_sha256", "data_sha256", "costs_sha256"):
        if not spec.get(key):
            reasons.append(f"missing_{key}")
    if not spec.get("trial_id"):
        reasons.append("missing_trial_linkage")
    try:
        registry = json.loads(trials_path.read_text())
        trials = registry.get("trials", [])
        if not any(t.get("name") == spec.get("trial_id") for t in trials):
            reasons.append("trial_not_found")
    except (OSError, ValueError, TypeError):
        reasons.append("trial_registry_unavailable")
    if not spec.get("eligible_regimes"):
        reasons.append("missing_regime_mapping")
    if not spec.get("supported_sides"):
        reasons.append("missing_supported_sides")
    if spec.get("protection_required") and not (spec.get("protection_method") and spec.get("protection_inputs")):
        reasons.append("protection_unavailable")
    checks = {
        "reproducible_strategy": "PASS" if family in FAMILIES else "FAIL",
        "historical_trial_registered": "PASS" if "trial_not_found" not in reasons and "missing_trial_linkage" not in reasons else "FAIL",
        "hashes_complete": "PASS" if not any(r.startswith("missing_") and r.endswith("sha256") for r in reasons) else "FAIL",
        "protection_defined": "PASS" if "protection_unavailable" not in reasons else "FAIL",
        "point_in_time_safe": "PASS" if spec.get("point_in_time_safe") is True else "FAIL",
        "historical_validation_gate": "PASS" if spec.get("historical_validation_passed") is True else "FAIL",
        "forward_evidence_required": "NO",
    }
    # These two explicit research attestations are pre-forward gates; neither
    # is inferred from later observations or final qualification.json.
    if checks["point_in_time_safe"] == "FAIL":
        reasons.append("point_in_time_safety_not_attested")
    if checks["historical_validation_gate"] == "FAIL":
        reasons.append("historical_validation_incomplete")
    status = "READY_TO_REGISTER" if not reasons else "NOT_READY_HISTORICAL_VALIDATION"
    if "protection_unavailable" in reasons:
        status = "NOT_READY_PROTECTION_INCOMPLETE"
    return {"candidate_id": spec.get("candidate_id"), "strategy_family": family,
            "status": status, "reasons": reasons, "candidate_admission": checks,
            "forward_evidence_required": False, "spec_sha256": sha256_json(spec)}


class CandidateRegistry:
    """SQLite-backed immutable registry shared by all shadow candidates."""

    def __init__(self, database: Path, trials_path: Path):
        self.database, self.trials_path = database, trials_path

    def _init(self, db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS shadow_candidates (
            candidate_id TEXT PRIMARY KEY, strategy_family TEXT NOT NULL,
            strategy_version TEXT NOT NULL, lifecycle TEXT NOT NULL,
            status TEXT NOT NULL, shadow_enabled INTEGER NOT NULL,
            activated_at REAL, spec_sha256 TEXT NOT NULL, spec TEXT NOT NULL,
            registered_at REAL NOT NULL)""")

    def register(self, spec: dict[str, Any], *, activate: bool = False, now: float | None = None) -> dict[str, Any]:
        audit = audit_candidate(spec, trials_path=self.trials_path)
        if audit["status"] != "READY_TO_REGISTER":
            raise ValueError("candidate is not ready: " + ",".join(audit["reasons"]))
        spec = dict(spec)
        spec["strategy_family"] = spec.get("strategy_family") or spec.get("family")
        candidate_id = str(spec["candidate_id"])
        digest = audit["spec_sha256"]
        now = time.time() if now is None else now
        lifecycle = "CANDIDATE"
        status = "ACTIVE_SHADOW" if activate else "PAUSED"
        with sqlite3.connect(self.database) as db:
            self._init(db)
            row = db.execute("SELECT spec_sha256,spec FROM shadow_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError(f"candidate ID {candidate_id} was reused with changed specification")
                return json.loads(row[1])
            frozen = dict(spec, lifecycle=lifecycle, shadow_enabled=True, status=status,
                          activated_at=now if activate else None, registered_at=now,
                          spec_sha256=digest)
            db.execute("INSERT INTO shadow_candidates VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (candidate_id, spec["strategy_family"], spec["strategy_version"], lifecycle,
                        status, 1, frozen["activated_at"], digest,
                        json.dumps(frozen, sort_keys=True), now))
            return frozen

    def list(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database) as db:
            self._init(db)
            return [json.loads(row[0]) for row in db.execute("SELECT spec FROM shadow_candidates ORDER BY candidate_id")]
