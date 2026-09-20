"""Fail-closed K-60 gate bound to code, config, data, costs and trial history."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

from src.backtest.statistics import evaluate
from src.fyers.models import ROOT


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def qualification(path: Path, trials_path: Path) -> tuple[bool, str]:
    """Recompute rather than trusting a supplied pass flag or DSR number.

    Evidence includes chronological net returns, trial name, strategy name,
    data path/hash, costs hash, registry hash and explicit data/cost review.
    Review is still required: code cannot prove that supplied returns are honest.
    """
    try:
        evidence = json.loads(path.read_text())
        registry = json.loads(trials_path.read_text())
        if not isinstance(evidence, dict) or not isinstance(registry, dict):
            return False, "invalid evidence or registry object"
        trials = registry["trials"]
        if not isinstance(trials, list) or not trials or any(not isinstance(t, dict) for t in trials) or type(registry["count"]) is not int or registry["count"] != len(trials):
            return False, "invalid full trial registry"
        if evidence["registry_sha256"] != digest(trials_path):
            return False, "trial registry changed; revalidate candidate"
        if evidence["costs_sha256"] != digest(ROOT / "config/fyers_costs.yaml"):
            return False, "FYERS cost configuration changed"
        if evidence["data_sha256"] != digest(ROOT / evidence["data_path"]):
            return False, "research data changed"
        for category in ("code_artifacts", "config_artifacts"):
            artifacts = evidence.get(category)
            if not isinstance(artifacts, dict) or not artifacts:
                return False, f"missing {category.replace('_', ' ')}"
            for relative, expected in artifacts.items():
                candidate = (ROOT / relative).resolve()
                if (not isinstance(relative, str) or not isinstance(expected, str)
                        or not candidate.is_relative_to(ROOT.resolve())
                        or digest(candidate) != expected):
                    return False, f"{category.replace('_', ' ')} changed"
        matches = [t for t in trials if t["name"] == evidence["trial_name"]]
        if len(matches) != 1 or evidence.get("broker") != "fyers" or evidence.get("reviewed_net_costs_and_data") is not True:
            return False, "missing registered FYERS trial or data/cost review"
        if not isinstance(evidence.get("strategy"), str) or not evidence["strategy"]:
            return False, "missing strategy identity"
        forward = evidence.get("forward_evidence")
        if (not isinstance(forward, dict) or forward.get("accepted") is not True
                or type(forward.get("observations")) is not int or forward["observations"] < 20
                or not isinstance(forward.get("selector_sha256"), str)
                or len(forward["selector_sha256"]) != 64
                or not isinstance(forward.get("start"), str)
                or not isinstance(forward.get("end"), str)
                or (datetime.fromisoformat(forward["end"]) -
                    datetime.fromisoformat(forward["start"])).days < 182):
            return False, "forward evidence has not been reviewed and accepted"
        economics = evidence.get("economic_evidence")
        if (not isinstance(economics, dict)
                or not isinstance(economics.get("benchmark_id"), str)
                or not (economics["benchmark_id"] == "NIFTY200_MOMENTUM30_TRI"
                        or economics["benchmark_id"].startswith("AMFI:"))
                or not isinstance(economics.get("benchmark_artifact_path"), str)
                or not isinstance(economics.get("benchmark_artifact_sha256"), str)
                or economics["benchmark_artifact_sha256"] != digest(
                    ROOT / economics["benchmark_artifact_path"])
                or not isinstance(economics.get("excess_lower_confidence_bound"), int | float)
                or economics["excess_lower_confidence_bound"] <= 0
                or not isinstance(economics.get("after_tax_and_infrastructure_excess"), int | float)
                or economics["after_tax_and_infrastructure_excess"] <= 0
                or not isinstance(economics.get("tax_policy_sha256"), str)
                or economics["tax_policy_sha256"] != digest(ROOT / "config/fyers_economics.yaml")
                or not isinstance(economics.get("break_even_capital_inr"), int | float)
                or economics["break_even_capital_inr"] > evidence.get("capital_inr", 10000)):
            return False, "benchmark-relative economic evidence failed"
        holdout = evidence.get("holdout_evidence")
        if (not isinstance(holdout, dict)
                or holdout.get("untouched_before_evaluation") is not True
                or not isinstance(holdout.get("experiment_id"), str)
                or not holdout["experiment_id"]
                or not isinstance(holdout.get("mean_excess_return"), int | float)
                or holdout["mean_excess_return"] <= 0
                or not isinstance(holdout.get("report_path"), str)
                or not isinstance(holdout.get("report_sha256"), str)
                or holdout["report_sha256"] != digest(ROOT / holdout["report_path"])):
            return False, "sealed holdout evidence failed"
        returns = evidence["net_returns"]
        if not isinstance(returns, list) or len(returns) < 100 or any(isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x) for x in returns):
            return False, "need at least 100 finite net observations"
        sharpes = [float(t["sharpe"]) for t in trials]
        if len(sharpes) < 2 or not all(math.isfinite(x) for x in sharpes):
            return False, "invalid trial dispersion"
        import statistics
        dispersion = statistics.stdev(sharpes)
        if dispersion <= 0:
            return False, "invalid trial dispersion"
        metrics = evaluate(returns, n_trials=len(trials), trial_sharpe_std=dispersion)
        if not any(t.get("n_obs") == len(returns) and math.isclose(float(t["sharpe"]), metrics["sharpe"], abs_tol=1e-6)
                   for t in matches):
            return False, "returns do not match the registered trial"
        mid = len(returns) // 2
        if not (metrics["deflated_sharpe"] > .95 and sum(returns[:mid]) > 0 and sum(returns[mid:]) > 0):
            return False, "K-60 failed"
        return True, evidence["strategy"]
    except (OSError, ValueError, KeyError, TypeError):
        return False, "qualification evidence missing or invalid"
