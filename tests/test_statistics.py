"""Tests for significance statistics.

The property that matters most: deflated Sharpe must fall as the trial count
rises. Without that, a search over 120 configurations reports the luckiest one
as though it were the only one — which is exactly how Round 1's "4h winner"
looked convincing at +41.6% on three symbols before collapsing to -58.4% on six.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.backtest.statistics import (
    RegistryIntegrityError,
    Trial,
    TrialsRegistry,
    benjamini_hochberg,
    deflated_sharpe_ratio,
    evaluate,
    expected_max_sharpe,
    paired_bootstrap_ci,
    probabilistic_sharpe_ratio,
    sharpe_ratio,
    t_statistic,
)


@pytest.fixture
def profitable():
    rng = np.random.default_rng(0)
    return rng.normal(0.5, 1.0, size=500)


@pytest.fixture
def noise():
    rng = np.random.default_rng(1)
    return rng.normal(0.0, 1.0, size=500)


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


def test_sharpe_and_t_are_zero_on_degenerate_input():
    for fn in (sharpe_ratio, t_statistic):
        assert fn([]) == 0.0
        assert fn([1.0]) == 0.0
        assert fn([2.0, 2.0, 2.0]) == 0.0, "zero variance must not divide by zero"


def test_sharpe_matches_the_definition(profitable):
    expected = profitable.mean() / profitable.std(ddof=1)
    assert sharpe_ratio(profitable) == pytest.approx(expected)


def test_nan_and_inf_are_dropped():
    assert sharpe_ratio([1.0, 2.0, np.nan, 3.0, np.inf]) == pytest.approx(
        sharpe_ratio([1.0, 2.0, 3.0])
    )


# ---------------------------------------------------------------------------
# The deflation property
# ---------------------------------------------------------------------------


def test_deflated_sharpe_decreases_as_trials_increase(profitable):
    """The core guarantee. More searching means a higher bar."""
    values = [
        deflated_sharpe_ratio(profitable, n_trials=n) for n in (1, 10, 100, 1000, 10_000)
    ]
    assert values == sorted(values, reverse=True), values
    assert values[0] > values[-1]


def test_a_lucky_result_from_many_trials_is_deflated_away():
    """A modest Sharpe found after 500 trials should not look significant."""
    rng = np.random.default_rng(7)
    lucky = rng.normal(0.12, 1.0, size=120)  # weak, noisy edge

    assert deflated_sharpe_ratio(lucky, n_trials=1) > deflated_sharpe_ratio(
        lucky, n_trials=500
    )
    assert deflated_sharpe_ratio(lucky, n_trials=500) < 0.95


def test_pure_noise_is_never_significant(noise):
    assert deflated_sharpe_ratio(noise, n_trials=120) < 0.95


def test_a_strong_genuine_edge_survives_deflation(profitable):
    """Deflation must not be so aggressive that real edges never pass."""
    assert deflated_sharpe_ratio(profitable, n_trials=120) > 0.95


def test_expected_max_sharpe_grows_with_trials():
    values = [expected_max_sharpe(n, 0.5) for n in (2, 10, 100, 1000)]
    assert values == sorted(values)


def test_expected_max_sharpe_is_zero_without_dispersion():
    assert expected_max_sharpe(100, 0.0) == 0.0
    assert expected_max_sharpe(1, 0.5) == 0.0


# ---------------------------------------------------------------------------
# PSR
# ---------------------------------------------------------------------------


def test_psr_is_bounded_and_monotone_in_sharpe():
    values = [probabilistic_sharpe_ratio(s, 200) for s in (-1.0, 0.0, 0.5, 2.0)]
    assert all(0.0 <= v <= 1.0 for v in values)
    assert values == sorted(values)


def test_psr_penalises_negative_skew_and_fat_tails():
    """Stop-loss strategies clip winners and gap through stops; that costs."""
    clean = probabilistic_sharpe_ratio(0.5, 200, skew=0.0, kurtosis=3.0)
    ugly = probabilistic_sharpe_ratio(0.5, 200, skew=-1.5, kurtosis=9.0)
    assert ugly < clean


def test_psr_needs_observations():
    assert probabilistic_sharpe_ratio(1.0, 1) == 0.0


# ---------------------------------------------------------------------------
# Multiple comparison
# ---------------------------------------------------------------------------


def test_benjamini_hochberg_rejects_only_small_pvalues():
    keep = benjamini_hochberg([0.001, 0.008, 0.2, 0.7, 0.9], alpha=0.05)
    assert keep[0] and keep[1]
    assert not keep[2] and not keep[3] and not keep[4]


def test_benjamini_hochberg_rejects_nothing_when_all_null():
    assert not any(benjamini_hochberg([0.4, 0.5, 0.6, 0.99], alpha=0.05))


def test_benjamini_hochberg_handles_empty():
    assert benjamini_hochberg([]) == []


# ---------------------------------------------------------------------------
# Paired bootstrap
# ---------------------------------------------------------------------------


def test_paired_bootstrap_brackets_a_real_difference():
    rng = np.random.default_rng(3)
    base = rng.normal(0.0, 1.0, size=400)
    # A noisy improvement, which is what a real A/B arm looks like. A constant
    # offset would give a zero-variance difference and a zero-width interval.
    better = base + rng.normal(0.4, 0.5, size=400)

    point, lo, hi = paired_bootstrap_ci(better, base)
    assert point == pytest.approx(0.4, abs=0.1)
    assert lo > 0, "a genuinely better arm should have a lower bound above zero"
    assert lo < point < hi


def test_paired_bootstrap_on_a_constant_offset_has_no_uncertainty():
    """Degenerate but correct: a fixed improvement has a zero-width interval."""
    rng = np.random.default_rng(3)
    base = rng.normal(0.0, 1.0, size=200)
    point, lo, hi = paired_bootstrap_ci(base + 0.4, base)
    assert point == pytest.approx(0.4)
    assert lo == pytest.approx(hi) == pytest.approx(0.4)


def test_paired_bootstrap_on_identical_arms_straddles_zero():
    rng = np.random.default_rng(4)
    arm = rng.normal(0.0, 1.0, size=300)
    point, lo, hi = paired_bootstrap_ci(arm, arm)
    assert point == pytest.approx(0.0)
    assert lo <= 0 <= hi


def test_paired_bootstrap_is_deterministic_given_a_seed():
    rng = np.random.default_rng(5)
    a, b = rng.normal(size=100), rng.normal(size=100)
    assert paired_bootstrap_ci(a, b, seed=42) == paired_bootstrap_ci(a, b, seed=42)


# ---------------------------------------------------------------------------
# Trials registry
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path):
    return TrialsRegistry(str(tmp_path / "trials.json"))


def test_registry_persists_across_instances(tmp_path, registry):
    registry.record(Trial(name="a", sharpe=0.3, n_obs=100, family="f"))
    registry.record(Trial(name="b", sharpe=0.1, n_obs=100, family="f"))

    reloaded = TrialsRegistry(str(tmp_path / "trials.json"))
    assert reloaded.count == 2
    assert reloaded.count_for_family("f") == 2


def test_registry_count_only_grows(registry):
    """An honest trial count cannot be reset by re-running a search."""
    registry.record_many(Trial(name=f"t{i}", sharpe=0.1, n_obs=50) for i in range(10))
    before = registry.count
    registry.record(Trial(name="another", sharpe=0.2, n_obs=50))
    assert registry.count == before + 1


def test_registry_reports_sharpe_dispersion(registry):
    registry.record_many(
        Trial(name=f"t{i}", sharpe=s, n_obs=100, family="xs")
        for i, s in enumerate([0.0, 0.2, -0.1, 0.4, 0.1])
    )
    assert registry.sharpe_std("xs") > 0
    assert registry.sharpe_std("nonexistent") == 0.0


def test_registry_dispersion_is_zero_when_too_few_trials(registry):
    """Better a no-op deflation than a fabricated dispersion."""
    registry.record(Trial(name="only", sharpe=1.0, n_obs=100))
    assert registry.sharpe_std() == 0.0


def test_registry_refuses_a_corrupt_file(tmp_path):
    path = tmp_path / "trials.json"
    path.write_text("{ not json")
    with pytest.raises(RegistryIntegrityError):
        TrialsRegistry(str(path))
    assert path.read_text() == "{ not json"


def test_registry_summary_groups_by_family(registry):
    registry.record(Trial(name="a", sharpe=0.1, n_obs=10, family="xs_reversal"))
    registry.record(Trial(name="b", sharpe=0.2, n_obs=10, family="price_action"))
    registry.record(Trial(name="c", sharpe=0.3, n_obs=10, family="xs_reversal"))

    summary = registry.summary()
    assert summary["total_trials"] == 3
    assert summary["by_family"]["xs_reversal"] == 2


# ---------------------------------------------------------------------------
# evaluate()
# ---------------------------------------------------------------------------


def test_evaluate_returns_everything_the_pass_mark_needs(profitable):
    result = evaluate(profitable, n_trials=120, label="test")
    for key in ("n_trades", "sharpe", "t_stat", "deflated_sharpe", "win_rate", "total_pct"):
        assert key in result
    assert result["n_trades"] == 500
    assert result["label"] == "test"


def test_evaluate_on_empty_input_is_safe():
    result = evaluate([], n_trials=10)
    assert result["n_trades"] == 0
    assert result["deflated_sharpe"] == 0.0
