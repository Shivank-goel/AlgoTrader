from datetime import UTC, date, datetime

import pytest

from src.fyers.costs import FyersCosts
from src.fyers.daily_data import _artifact
from src.fyers.models import Side
from src.research.nse_engine import simulate
from src.strategies.nse_regime_selector import NseRegime


def _artifact_for(day: date, open_price: float, close: float, symbols=("NSE:TEST-EQ",)):
    raw = {
        "schema_version": 1, "session_date": day.isoformat(), "source": "synthetic",
        "captured_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(), "universe_sha256": "a" * 64,
        "bars": {symbol: {"open": open_price, "high": max(open_price, close), "low": min(open_price, close), "close": close, "volume": 1000} for symbol in symbols},
        "benchmark_symbol": "NSE:NIFTY50-INDEX", "benchmark": {"open": 100, "high": 110, "low": 99, "close": 100 + (day.day - 1), "volume": 1000}, "missing_symbols": [],
    }
    return _artifact(raw)


def test_complete_synthetic_trade_lifecycle(monkeypatch):
    import src.research.nse_engine as engine

    class FakeSelector:
        def __init__(self, config): pass
        def regime(self, closes, benchmark): return NseRegime.TRENDING_UP, 1.0, {}
        def scores(self, family, closes):
            import pandas as pd
            return pd.Series({"NSE:TEST-EQ": 2.5})

    monkeypatch.setattr(engine, "RegimeAwareSelector", FakeSelector)
    spec = {"strategy_family": "momentum_6_12", "initial_capital_inr": 10000,
            "slippage_model": {"slippage_bps": 2}, "allocation_rule": {"type": "FIXED_POSITION_BUDGET", "max_positions": 5}}
    days = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    costs = FyersCosts.load()
    result = simulate(spec, [_artifact_for(days[0], 100, 100), _artifact_for(days[1], 100, 105), _artifact_for(days[2], 110, 110)], dataset_hash="b" * 64, trial_id="synthetic", costs=costs)
    trade = result["trades"][0]
    assert (trade["signal_session"], trade["entry_session"], trade["exit_session"]) == ("2026-01-01", "2026-01-02", "2026-01-03")
    assert trade["raw_entry_open"] == 100 and trade["entry_execution_price"] == pytest.approx(100.02)
    assert trade["quantity"] == 19 and trade["raw_exit_open"] == 110
    assert trade["exit_execution_price"] == pytest.approx(109.978)
    expected_entry_fee = costs.fee(100.02 * 19, Side.BUY, delivery=True)
    expected_exit_fee = costs.fee(109.978 * 19, Side.SELL, delivery=True)
    assert trade["entry_costs"] == pytest.approx(expected_entry_fee)
    assert trade["exit_costs"] == pytest.approx(expected_exit_fee)
    assert trade["gross_pnl"] == pytest.approx((109.978 - 100.02) * 19)
    assert trade["net_pnl"] == pytest.approx(trade["gross_pnl"] - expected_entry_fee - expected_exit_fee)
    assert result["trade_count"] == 1 and result["total_costs"] == pytest.approx(expected_entry_fee + expected_exit_fee)


def test_six_signals_are_capped_at_five_and_repeat_deterministically(monkeypatch):
    import pandas as pd

    import src.research.nse_engine as engine

    symbols = tuple(f"NSE:{letter}-EQ" for letter in "ABCDEF")
    class FakeSelector:
        def __init__(self, config): pass
        def regime(self, closes, benchmark): return NseRegime.TRENDING_UP, 1.0, {}
        def scores(self, family, closes): return pd.Series({symbol: 6 - i for i, symbol in enumerate(symbols)})
    monkeypatch.setattr(engine, "RegimeAwareSelector", FakeSelector)
    spec = {"strategy_family": "momentum_6_12", "initial_capital_inr": 10000, "slippage_model": {"slippage_bps": 2}, "allocation_rule": {"type": "FIXED_POSITION_BUDGET", "max_positions": 5}}
    days = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    artifacts = [_artifact_for(day, 100, 100, symbols) for day in days]
    first = simulate(spec, artifacts, dataset_hash="b" * 64, trial_id="synthetic", costs=FyersCosts.load())
    second = simulate(spec, artifacts, dataset_hash="b" * 64, trial_id="synthetic", costs=FyersCosts.load())
    assert first == second
    assert len(first["trades"]) == 5
    assert {trade["symbol"] for trade in first["trades"]} == set(symbols[:5])


def test_incomplete_synthetic_artifacts_fail_closed():
    spec = {"strategy_family": "momentum_6_12", "allocation_rule": {"type": "FIXED_POSITION_BUDGET", "max_positions": 5}}
    artifact = _artifact_for(date(2026, 1, 1), 100, 100)
    artifact = artifact.model_copy(update={"missing_symbols": ["NSE:TEST-EQ"]})
    with pytest.raises(ValueError, match="DATA_INCOMPLETE"):
        simulate(spec, [artifact, artifact, artifact], dataset_hash="b" * 64, trial_id="synthetic")


def test_missing_entry_open_fails_closed_without_fallback():
    spec = {"strategy_family": "momentum_6_12", "allocation_rule": {"type": "FIXED_POSITION_BUDGET", "max_positions": 5}}
    artifact = _artifact_for(date(2026, 1, 1), 100, 100).model_copy(update={"missing_symbols": ["NSE:TEST-EQ"]})
    with pytest.raises(ValueError, match="DATA_INCOMPLETE"):
        simulate(spec, [artifact] * 3, dataset_hash="b" * 64, trial_id="missing-entry")


def test_missing_exit_open_fails_closed_without_fallback():
    spec = {"strategy_family": "momentum_6_12", "allocation_rule": {"type": "FIXED_POSITION_BUDGET", "max_positions": 5}}
    artifact = _artifact_for(date(2026, 1, 1), 100, 100).model_copy(update={"missing_symbols": ["NSE:TEST-EQ"]})
    with pytest.raises(ValueError, match="DATA_INCOMPLETE"):
        simulate(spec, [artifact] * 3, dataset_hash="b" * 64, trial_id="missing-exit")


def test_missing_marking_close_fails_closed():
    spec = {"strategy_family": "momentum_6_12", "allocation_rule": {"type": "FIXED_POSITION_BUDGET", "max_positions": 5}}
    artifact = _artifact_for(date(2026, 1, 1), 100, 100).model_copy(update={"missing_symbols": ["NSE:TEST-EQ"]})
    with pytest.raises(ValueError, match="DATA_INCOMPLETE"):
        simulate(spec, [artifact] * 3, dataset_hash="b" * 64, trial_id="missing-close")


def test_missing_benchmark_start_close_fails_closed():
    spec = {"strategy_family": "momentum_6_12", "allocation_rule": {"type": "FIXED_POSITION_BUDGET", "max_positions": 5}}
    artifact = _artifact_for(date(2026, 1, 1), 100, 100).model_copy(update={"benchmark": None})
    with pytest.raises(ValueError, match="DATA_INCOMPLETE"):
        simulate(spec, [artifact] * 3, dataset_hash="b" * 64, trial_id="missing-benchmark-start")


def test_missing_benchmark_end_close_fails_closed():
    spec = {"strategy_family": "momentum_6_12", "allocation_rule": {"type": "FIXED_POSITION_BUDGET", "max_positions": 5}}
    artifact = _artifact_for(date(2026, 1, 1), 100, 100).model_copy(update={"benchmark": None})
    with pytest.raises(ValueError, match="DATA_INCOMPLETE"):
        simulate(spec, [artifact] * 3, dataset_hash="b" * 64, trial_id="missing-benchmark-end")


def test_exit_before_entry_releases_cash_for_next_symbol(monkeypatch):
    import pandas as pd

    import src.research.nse_engine as engine

    class Selector:
        def __init__(self, config): pass
        def regime(self, closes, benchmark): return NseRegime.TRENDING_UP, 1.0, {}
        def scores(self, family, closes):
            return pd.Series({"NSE:A-EQ": 1.0} if len(closes) == 1 else {"NSE:B-EQ": 1.0})
    monkeypatch.setattr(engine, "RegimeAwareSelector", Selector)
    symbols = ("NSE:A-EQ", "NSE:B-EQ")
    spec = {"strategy_family": "momentum_6_12", "initial_capital_inr": 2000,
            "slippage_model": {"slippage_bps": 2}, "allocation_rule": {"type": "FIXED_POSITION_BUDGET", "max_positions": 5}}
    days = [date(2026, 1, d) for d in range(1, 5)]
    artifacts = [_artifact_for(day, 100, 100, symbols) for day in days]
    result = simulate(spec, artifacts, dataset_hash="b" * 64, trial_id="ordering", costs=FyersCosts.load())
    assert [trade["symbol"] for trade in result["trades"]] == ["NSE:A-EQ", "NSE:B-EQ"]
    assert result["unexecuted_signals"] == []


def test_same_symbol_exit_and_reentry_are_separate_lifecycles(monkeypatch):
    import pandas as pd

    import src.research.nse_engine as engine

    class Selector:
        def __init__(self, config): pass
        def regime(self, closes, benchmark): return NseRegime.TRENDING_UP, 1.0, {}
        def scores(self, family, closes): return pd.Series({"NSE:XYZ-EQ": 1.0})
    monkeypatch.setattr(engine, "RegimeAwareSelector", Selector)
    spec = {"strategy_family": "momentum_6_12", "initial_capital_inr": 10000,
            "slippage_model": {"slippage_bps": 2}, "allocation_rule": {"type": "FIXED_POSITION_BUDGET", "max_positions": 5}}
    days = [date(2026, 1, d) for d in range(1, 5)]
    artifacts = [_artifact_for(day, 100 + d, 100 + d, ("NSE:XYZ-EQ",)) for d, day in enumerate(days)]
    result = simulate(spec, artifacts, dataset_hash="b" * 64, trial_id="reentry", costs=FyersCosts.load())
    assert len(result["trades"]) == 2
    assert all(trade["symbol"] == "NSE:XYZ-EQ" for trade in result["trades"])
    assert result["trades"][0]["entry_session"] != result["trades"][1]["entry_session"]
