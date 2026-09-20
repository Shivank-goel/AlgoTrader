"""Deterministic diagnostics; reports are evidence, never deployment approval."""

from __future__ import annotations

import numpy as np

from src.backtest.statistics import evaluate
from src.research.models import ExperimentOutput, ExperimentSpec


def strategy_integrity(strategy, panel, *, sample_points: int = 12,
                       warmup_multiplier: int = 2) -> dict:
    """Execute lookahead and warm-up invariance checks on portfolio decisions."""
    from src.strategies.portfolio import PricePanel
    if sample_points < 1 or warmup_multiplier < 1:
        raise ValueError("Invalid strategy integrity settings")
    warmup = strategy.min_history()
    candidates = list(range(warmup, len(panel), max(1, (len(panel) - warmup) // sample_points)))[:sample_points]
    if not candidates:
        raise ValueError("Not enough data for strategy integrity checks")
    lookahead_failures = []
    warmup_failures = []
    for index in candidates:
        visible = PricePanel(panel.close.iloc[:index + 1], panel.returns.iloc[:index + 1])
        full = strategy.target_weights(panel, index)
        prefix = strategy.target_weights(visible, index)
        if not full.equals(prefix):
            lookahead_failures.append(index)
        start = max(0, index + 1 - warmup * warmup_multiplier)
        tail = PricePanel(panel.close.iloc[start:index + 1], panel.returns.iloc[start:index + 1])
        tail_decision = strategy.target_weights(tail, len(tail) - 1)
        if start and not full.equals(tail_decision):
            warmup_failures.append(index)
    return {"sampled": len(candidates), "lookahead_failures": lookahead_failures,
            "warmup_failures": warmup_failures,
            "passed": not lookahead_failures and not warmup_failures}


def walk_forward_splits(n: int, train: int, test: int, *, gap: int = 0) -> list[tuple[slice, slice]]:
    """Rolling, non-overlapping OOS windows, expressed explicitly in observations."""
    if any(type(v) is not int for v in (n, train, test, gap)) or min(n, train, test) < 1 or gap < 0:
        raise ValueError("Invalid split sizes")
    return [(slice(end - train, end), slice(end + gap, end + gap + test))
            for end in range(train, n - gap - test + 1, test)]


def block_bootstrap(values: list[float], *, block: int, seed: int, samples: int = 2000) -> tuple[float, float]:
    """Circular block bootstrap preserves within-block temporal dependence."""
    x = np.asarray(values, dtype=float)
    if x.ndim != 1 or len(x) < 2 or not np.isfinite(x).all() or not 1 <= block <= len(x) or samples < 1:
        raise ValueError("Invalid bootstrap inputs")
    rng = np.random.default_rng(seed)
    means = np.empty(samples)
    offsets = np.arange(block)
    for i in range(samples):
        starts = rng.integers(0, len(x), size=(len(x) + block - 1) // block)
        indices = ((starts[:, None] + offsets) % len(x)).ravel()[:len(x)]
        means[i] = x[indices].mean()
    return tuple(float(v) for v in np.quantile(means, [.025, .975]))


def report(spec: ExperimentSpec, output: ExperimentOutput, *, n_trials: int,
           trial_sharpe_std: float, campaign_trials: int | None = None) -> dict:
    dates = output.timestamps
    if dates[0] < spec.start or dates[-1] > spec.end:
        raise ValueError("Returns are outside the registered date range")
    returns = np.asarray(output.net_returns)
    baseline = np.asarray(output.baseline_returns)
    excess = returns - baseline
    if spec.bootstrap_block > len(returns):
        raise ValueError("Not enough observations for the registered bootstrap block")
    equity = np.cumprod(np.r_[1.0, 1.0 + returns])
    drawdown = float(np.max(1 - equity / np.maximum.accumulate(equity)))
    masks = {
        "train": np.array([t <= spec.train_end for t in dates]),
        "validation": np.array([spec.train_end < t <= spec.validation_end for t in dates]),
        "holdout": np.array([spec.validation_end < t <= spec.holdout_end for t in dates]),
    }
    periods = {name: {"n": int(mask.sum()), "mean_net": float(returns[mask].mean()) if mask.any() else None,
                       "mean_excess": float(excess[mask].mean()) if mask.any() else None}
               for name, mask in masks.items()}
    metrics = evaluate(returns, n_trials=n_trials, trial_sharpe_std=trial_sharpe_std)
    midpoint = len(returns) // 2
    dispersion_valid = n_trials >= 2 and np.isfinite(trial_sharpe_std) and trial_sharpe_std > 0
    k60 = bool(dispersion_valid and len(returns) >= 100 and metrics["deflated_sharpe"] > .95
               and returns[:midpoint].sum() > 0 and returns[midpoint:].sum() > 0)
    lower, upper = block_bootstrap(excess.tolist(), block=spec.bootstrap_block, seed=spec.seed)
    positive = np.maximum(returns, 0)
    top = float(np.sort(positive)[-max(1, len(returns) // 20):].sum() / positive.sum()) if positive.sum() else None
    reasons = []
    if not k60:
        reasons.append("K-60 numerical criteria not met")
    if any(p["n"] < 2 for p in periods.values()):
        reasons.append("Insufficient observations in a registered split")
    if drawdown > spec.max_drawdown:
        reasons.append("Registered drawdown limit exceeded")
    if lower <= 0:
        reasons.append("Block-bootstrap excess-return interval includes zero")
    if periods["holdout"]["mean_excess"] is None or periods["holdout"]["mean_excess"] <= 0:
        reasons.append("No positive holdout benchmark excess")
    from src.fyers.profitability import TaxPolicy, economic_report
    economics = economic_report(
        output.net_returns, output.baseline_returns, output.timestamps,
        capital_inr=float(spec.cost_model.get("capital_inr", 10000)),
        monthly_infrastructure_inr=float(spec.cost_model.get("monthly_infrastructure_inr", 0)),
        policy=TaxPolicy.load(), bootstrap_block=spec.bootstrap_block, seed=spec.seed,
    )
    if economics.excess_mean_ci95[0] <= 0:
        reasons.append("Economic excess-return confidence bound is not positive")
    if economics.after_tax_and_infrastructure_excess_return <= 0:
        reasons.append("Strategy does not beat the benchmark after tax and infrastructure scenario")
    return {
        "metrics": metrics, "full_trial_count": n_trials,
        "multiple_testing": {
            "campaign_trials": campaign_trials,
            "full_registry_trials": n_trials,
            "bonferroni_false_positive_bound": (
                min(1.0, campaign_trials * .05) if campaign_trials is not None else None
            ),
            "deflated_sharpe_probability": metrics["deflated_sharpe"],
        },
        "k60_numerical_only": k60, "max_drawdown": drawdown, "splits": periods,
        "excess_mean_ci95": [lower, upper], "top_5pct_positive_return_share": top,
        "mean_turnover": float(np.mean(output.turnover)),
        "cost_sensitivity_mean_net": {
            str(bps): float(np.mean(returns - np.asarray(output.turnover) * bps / 10000))
            for bps in (0, 5, 10, 25, 50)
        },
        "economics": economics.model_dump(mode="json"),
        "critic_reasons": reasons,
        "qualification": False,
        "review_required": ["Holdout genuinely unseen", "Measured costs and spread", "Return units and trial comparability",
                            "Parameter/regime robustness", "Execution realism", "Forward shadow and operational evidence"],
    }
