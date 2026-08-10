"""Significance statistics for backtest results.

The problem this exists to solve, concretely: searching 30 strategy
configurations and reporting the best one's t-statistic is not evidence. Round 1
produced a 4h configuration showing +41.6% net on three symbols that collapsed
to -58.4% on six. A plain Sharpe ratio cannot tell those apart; a deflated one
can, because it accounts for how many configurations were tried to find it.

Deflated Sharpe (Bailey & Lopez de Prado, 2014) answers: given that I ran N
trials, what is the probability this Sharpe exceeds zero for a real reason?

Every number here is computed on **per-trade returns**, not equity-curve points,
so `n_obs` is the trade count.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)

# Euler-Mascheroni, used in the expected-maximum-Sharpe approximation.
EULER_GAMMA = 0.5772156649015329

DEFAULT_TRIALS_PATH = "data/trials.json"


def _clean(returns: Sequence[float]) -> np.ndarray:
    arr = np.asarray(list(returns), dtype=float)
    return arr[np.isfinite(arr)]


def sharpe_ratio(returns: Sequence[float]) -> float:
    """Per-trade Sharpe (not annualised). Zero when undefined."""
    arr = _clean(returns)
    if len(arr) < 2:
        return 0.0
    sd = float(arr.std(ddof=1))
    if sd <= 0:
        return 0.0
    return float(arr.mean() / sd)


def t_statistic(returns: Sequence[float]) -> float:
    arr = _clean(returns)
    if len(arr) < 2:
        return 0.0
    sd = float(arr.std(ddof=1))
    if sd <= 0:
        return 0.0
    return float(arr.mean() / (sd / math.sqrt(len(arr))))


def probabilistic_sharpe_ratio(
    observed_sharpe: float,
    n_obs: int,
    *,
    benchmark_sharpe: float = 0.0,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """P(true Sharpe > benchmark), correcting for skew and fat tails.

    Non-normal returns inflate a naive Sharpe's apparent reliability. Negative
    skew and excess kurtosis — both typical of stop-loss strategies, which clip
    winners and occasionally gap through stops — reduce this probability.
    """
    if n_obs < 2:
        return 0.0

    # Guard the variance term: it goes negative for extreme skew/kurtosis combos.
    variance = (
        1.0
        - skew * observed_sharpe
        + (kurtosis - 1.0) / 4.0 * observed_sharpe**2
    )
    if variance <= 0:
        return 0.0

    numerator = (observed_sharpe - benchmark_sharpe) * math.sqrt(n_obs - 1)
    return float(stats.norm.cdf(numerator / math.sqrt(variance)))


def expected_max_sharpe(n_trials: int, trial_sharpe_std: float) -> float:
    """Sharpe you'd expect from the *best* of n_trials with no real edge.

    This is the bar a search result must clear simply to be interesting. It
    grows with the number of trials, which is why the trial count must be
    honest.
    """
    if n_trials < 2 or trial_sharpe_std <= 0:
        return 0.0

    # Bailey & Lopez de Prado's approximation to E[max of n standard normals].
    z1 = stats.norm.ppf(1.0 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(trial_sharpe_std * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2))


def deflated_sharpe_ratio(
    returns: Sequence[float],
    *,
    n_trials: int,
    trial_sharpe_std: Optional[float] = None,
) -> float:
    """P(true Sharpe > 0) after deflating for the number of trials run.

    `trial_sharpe_std` is the dispersion of Sharpe ratios across the trials
    actually run. When unknown, it is approximated as 1/sqrt(n_obs), the
    standard error of a Sharpe under the null — conservative but not the real
    thing. Prefer passing the measured value from a trials registry.

    Interpretation: > 0.95 is the conventional bar. Round 1's baselines should
    land near 0.
    """
    arr = _clean(returns)
    n_obs = len(arr)
    if n_obs < 2:
        return 0.0

    observed = sharpe_ratio(arr)
    if trial_sharpe_std is None:
        trial_sharpe_std = 1.0 / math.sqrt(n_obs)

    benchmark = expected_max_sharpe(max(n_trials, 1), trial_sharpe_std)

    return probabilistic_sharpe_ratio(
        observed,
        n_obs,
        benchmark_sharpe=benchmark,
        skew=float(stats.skew(arr)) if n_obs > 2 else 0.0,
        kurtosis=float(stats.kurtosis(arr, fisher=False)) if n_obs > 3 else 3.0,
    )


def benjamini_hochberg(pvalues: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Which hypotheses survive at a given false-discovery rate.

    Use when comparing many strategies at once; controls the expected share of
    false positives among rejections rather than the family-wise error rate.
    """
    p = np.asarray(list(pvalues), dtype=float)
    n = len(p)
    if n == 0:
        return []

    order = np.argsort(p)
    thresholds = alpha * (np.arange(1, n + 1) / n)
    passed_sorted = p[order] <= thresholds

    # Everything up to the largest passing index is rejected.
    cutoff = np.where(passed_sorted)[0]
    keep = np.zeros(n, dtype=bool)
    if len(cutoff):
        keep[order[: cutoff[-1] + 1]] = True
    return keep.tolist()


def paired_bootstrap_ci(
    a: Sequence[float],
    b: Sequence[float],
    *,
    n_boot: int = 10_000,
    confidence: float = 0.90,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Bootstrap CI for mean(a) - mean(b) on paired observations.

    Returns (point_estimate, lower, upper). Paired because A/B arms on the same
    signal stream are not independent samples.
    """
    x = _clean(a)
    y = _clean(b)
    n = min(len(x), len(y))
    if n < 2:
        return 0.0, 0.0, 0.0

    diff = x[:n] - y[:n]
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    means = diff[idx].mean(axis=1)

    tail = (1.0 - confidence) / 2.0
    return (
        float(diff.mean()),
        float(np.quantile(means, tail)),
        float(np.quantile(means, 1.0 - tail)),
    )


# ---------------------------------------------------------------------------
# Trials registry
# ---------------------------------------------------------------------------


@dataclass
class Trial:
    """One evaluated configuration."""

    name: str
    sharpe: float
    n_obs: int
    net_return_pct: float = 0.0
    t_stat: float = 0.0
    family: str = "unspecified"
    params: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sharpe": round(float(self.sharpe), 6),
            "n_obs": int(self.n_obs),
            "net_return_pct": round(float(self.net_return_pct), 4),
            "t_stat": round(float(self.t_stat), 4),
            "family": self.family,
            "params": self.params,
            "recorded_at": self.recorded_at,
        }


class TrialsRegistry:
    """Append-only record of every configuration ever evaluated.

    Deflated Sharpe is only meaningful against an honest trial count, and a
    count kept in someone's head is not honest. Every search writes here, and
    the count only ever goes up — including the ~120 configurations already
    tested before this file existed.
    """

    def __init__(self, path: str = DEFAULT_TRIALS_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._trials: list[Trial] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            logger.warning("Trials registry at %s unreadable; starting empty", self.path)
            return
        for row in raw.get("trials", []):
            self._trials.append(
                Trial(
                    name=row.get("name", "?"),
                    sharpe=float(row.get("sharpe", 0.0)),
                    n_obs=int(row.get("n_obs", 0)),
                    net_return_pct=float(row.get("net_return_pct", 0.0)),
                    t_stat=float(row.get("t_stat", 0.0)),
                    family=row.get("family", "unspecified"),
                    params=row.get("params", {}),
                    recorded_at=row.get("recorded_at", ""),
                )
            )

    def _save(self) -> None:
        payload = {
            "count": len(self._trials),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "trials": [t.to_dict() for t in self._trials],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)

    def record(self, trial: Trial) -> None:
        self._trials.append(trial)
        self._save()

    def record_many(self, trials: Iterable[Trial]) -> int:
        added = list(trials)
        self._trials.extend(added)
        self._save()
        return len(added)

    @property
    def count(self) -> int:
        return len(self._trials)

    def count_for_family(self, family: str) -> int:
        return sum(1 for t in self._trials if t.family == family)

    def sharpe_std(self, family: Optional[str] = None) -> float:
        """Dispersion of Sharpe across trials — the input deflation needs.

        Falls back to 0.0 when there are too few trials to estimate it, which
        makes `expected_max_sharpe` return 0 and the deflation a no-op rather
        than a fabricated number.
        """
        pool = [
            t.sharpe
            for t in self._trials
            if family is None or t.family == family
        ]
        if len(pool) < 2:
            return 0.0
        return float(np.std(np.asarray(pool, dtype=float), ddof=1))

    def summary(self) -> dict[str, Any]:
        families: dict[str, int] = {}
        for t in self._trials:
            families[t.family] = families.get(t.family, 0) + 1
        return {
            "total_trials": len(self._trials),
            "by_family": families,
            "sharpe_std": round(self.sharpe_std(), 4),
        }


def evaluate(
    returns: Sequence[float],
    *,
    n_trials: int,
    trial_sharpe_std: Optional[float] = None,
    label: str = "",
) -> dict[str, Any]:
    """One-call verdict on a return series. Everything a pass mark needs."""
    arr = _clean(returns)
    n = len(arr)
    return {
        "label": label,
        "n_trades": n,
        "mean_pct": float(arr.mean()) if n else 0.0,
        "total_pct": float(arr.sum()) if n else 0.0,
        "sharpe": sharpe_ratio(arr),
        "t_stat": t_statistic(arr),
        "n_trials": n_trials,
        "deflated_sharpe": deflated_sharpe_ratio(
            arr, n_trials=n_trials, trial_sharpe_std=trial_sharpe_std
        ),
        "win_rate": float((arr > 0).mean()) if n else 0.0,
    }
