import hashlib
from datetime import UTC, datetime

import pandas as pd

from src.fyers.daily_data import _artifact, _publish
from src.fyers.data_readiness import daily_data_readiness
from src.fyers.models import ROOT, RuntimeConfig
from src.fyers.universe import ForwardUniverse


def _write_days(directory, count: int, *, missing_symbol: str | None = None,
                benchmark_symbol: str = "NSE:NIFTY50-INDEX"):
    config = RuntimeConfig.load()
    universe = ForwardUniverse.load(ROOT / "config/nse_forward_universe.yaml")
    universe_hash = hashlib.sha256((ROOT / "config/nse_forward_universe.yaml").read_bytes()).hexdigest()
    days = pd.date_range("2025-01-01", periods=count, freq="B")
    for index, day in enumerate(days):
        bars = {member.symbol: {"open": 100 + index, "high": 102 + index,
                                "low": 99 + index, "close": 101 + index, "volume": 10}
                for member in universe.members if member.symbol != missing_symbol}
        payload = {"schema_version": 1, "session_date": day.date().isoformat(), "source": "fixture",
                   "captured_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
                   "universe_sha256": universe_hash, "bars": bars,
                   "benchmark_symbol": benchmark_symbol,
                   "benchmark": {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 10},
                   "missing_symbols": [missing_symbol] if missing_symbol else []}
        _publish(directory / f"{day.date().isoformat()}.json", _artifact(payload))
    return config, universe


def test_daily_readiness_reports_reproducible_complete_dataset(tmp_path):
    config, universe = _write_days(tmp_path, 253)
    result = daily_data_readiness(config, directory=tmp_path, universe=universe, now=2_000_000_000)
    assert result["data_ready"] and result["state"] == "DATA_READY"
    assert result["minimum_completed_bars"] == 253
    assert result["benchmark"]["completed_bars"] == 253
    assert len(result["dataset"]["artifact_sha256"]) == 64


def test_daily_readiness_identifies_insufficient_and_missing_symbol_history(tmp_path):
    config, universe = _write_days(tmp_path, 2, missing_symbol="NSE:SBIN-EQ")
    result = daily_data_readiness(config, directory=tmp_path, universe=universe, now=2_000_000_000)
    assert not result["data_ready"]
    assert {"insufficient_bars", "symbol_coverage_failure"} <= set(result["reasons"])
    assert result["symbols"]["NSE:SBIN-EQ"]["missing_dates"]


def test_daily_readiness_rejects_benchmark_mismatch_and_corrupt_artifact(tmp_path):
    config, universe = _write_days(tmp_path, 1, benchmark_symbol="NSE:OTHER-INDEX")
    (tmp_path / "2026-09-01.json").write_text("not json")
    result = daily_data_readiness(config, directory=tmp_path, universe=universe, now=2_000_000_000)
    assert {"benchmark_missing", "data_quality_failure", "insufficient_bars"} <= set(result["reasons"])
