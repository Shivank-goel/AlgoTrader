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

import fcntl
import hashlib
import json
import logging
import math
import os
import tempfile
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
    x, y = np.asarray(list(a), dtype=float), np.asarray(list(b), dtype=float)
    if x.ndim != 1 or y.ndim != 1 or len(x) != len(y):
        raise ValueError("Paired observations must have equal lengths")
    if n_boot < 1 or not 0 < confidence < 1:
        raise ValueError("Invalid bootstrap configuration")
    valid = np.isfinite(x) & np.isfinite(y)
    x, y = x[valid], y[valid]
    n = len(x)
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


class RegistryIntegrityError(ValueError):
    """Research history is unreadable or inconsistent; never reset it implicitly."""


class TrialsRegistry:
    """Append-only record of every configuration ever evaluated.

    Deflated Sharpe is only meaningful against an honest trial count, and a
    count kept in someone's head is not honest. Every search writes here, and
    the count only ever goes up — including the ~120 configurations already
    tested before this file existed.
    """

    def __init__(self, path: str = DEFAULT_TRIALS_PATH) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._trials: list[Trial] = []
        self._rows: list[dict[str, Any]] = []
        self._seen_file = False
        self.snapshot_sha256: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            if self._seen_file:
                raise RegistryIntegrityError("Previously loaded trial registry is missing")
            return
        try:
            snapshot = self.path.read_bytes()
            raw = self.validate_snapshot(snapshot)
            rows = raw["trials"]
            if rows[:len(self._rows)] != self._rows:
                raise ValueError("Previously loaded research history changed")
            trials = [Trial(**{k: row[k] for k in Trial.__dataclass_fields__}) for row in rows]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RegistryIntegrityError("Trial registry invalid; restore verified history before research") from exc
        self._rows, self._trials = rows, trials
        self._seen_file = True
        self.snapshot_sha256 = hashlib.sha256(snapshot).hexdigest()

    @classmethod
    def validate_snapshot(cls, snapshot: bytes) -> dict[str, Any]:
        """Validate exactly the captured bytes, without a second filesystem read."""
        try:
            raw = json.loads(snapshot)
            if not isinstance(raw, dict) or not isinstance(raw.get("trials"), list):
                raise ValueError("Invalid registry object")
            if type(raw.get("count")) is not int or raw["count"] != len(raw["trials"]):
                raise ValueError("Invalid trial count")
            for row in raw["trials"]:
                cls._validate_row(row)
            return raw
        except (ValueError, KeyError, TypeError) as exc:
            raise RegistryIntegrityError("Invalid captured trial history") from exc

    def record_once(self, trial: Trial) -> Trial:
        """Append one experiment atomically, or return its identical stored trial.

        Identity is params.experiment_id; both name and identity collisions fail.
        recorded_at is assigned on first publication and ignored on a retry.
        count, dispersion and snapshot_sha256 describe the same captured snapshot.
        """
        self._validate_row(vars(trial))
        incoming = trial.to_dict()
        identity = incoming["params"].get("experiment_id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("record_once requires an experiment_id")
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._load()
            matches = [r for r in self._rows if r["name"] == incoming["name"]
                       or r["params"].get("experiment_id") == identity]
            if matches:
                comparable = lambda row: {k: v for k, v in row.items() if k != "recorded_at"}
                if len(matches) != 1 or comparable(matches[0]) != comparable(incoming):
                    raise RegistryIntegrityError("Conflicting experiment trial publication")
                stored = matches[0]
            else:
                self._save(self._rows + [incoming])
                self._load()
                stored = self._rows[-1]
            # Return a detached copy so callers cannot mutate the captured history.
            return Trial(**json.loads(json.dumps(stored)))

    @staticmethod
    def _validate_row(row: dict[str, Any]) -> None:
        if not isinstance(row, dict):
            raise ValueError("Invalid trial record")
        for key in ("name", "family", "recorded_at"):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError(f"Invalid trial {key}")
        if type(row.get("n_obs")) is not int or row["n_obs"] < 0:
            raise ValueError("Invalid observation count")
        for key in ("sharpe", "net_return_pct", "t_stat"):
            value = row.get(key)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"Invalid trial {key}")
        if not isinstance(row.get("params"), dict):
            raise ValueError("Invalid parameters")
        json.dumps(row, allow_nan=False)

    def _save(self, rows: list[dict[str, Any]]) -> None:
        payload = {
            "count": len(rows),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "trials": rows,
        }
        encoded = json.dumps(payload, indent=2, allow_nan=False)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            tmp.replace(self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            tmp.unlink(missing_ok=True)

    def record(self, trial: Trial) -> None:
        self.record_many([trial])

    def record_many(self, trials: Iterable[Trial]) -> int:
        added = []
        for trial in trials:
            # Validate before to_dict() can coerce invalid booleans/fractional counts.
            self._validate_row(vars(trial))
            added.append(trial.to_dict())
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._load()
            if added:
                self._save(self._rows + added)
                self._load()
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
