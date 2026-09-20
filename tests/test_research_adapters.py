import hashlib
from datetime import UTC

import pandas as pd
import pytest

from src.research.adapters import run_adapter
from src.research.models import ExperimentSpec
from src.research.validation import strategy_integrity
from src.strategies.portfolio import PortfolioStrategy, PricePanel


def test_registered_csv_adapter_runs_real_backtest_without_future_rows(tmp_path):
    index = pd.date_range("2025-01-01", periods=40, freq="4h", tz="UTC")
    paths = {}
    artifacts = {}
    for number, symbol in enumerate(("A", "B", "C", "D"), 1):
        path = tmp_path / f"{symbol}.csv"
        pd.DataFrame({"timestamp": index[:25],
                      "close": [100 + number * i for i in range(25)]}).to_csv(path, index=False)
        paths[symbol] = path.name
        artifacts[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    spec = ExperimentSpec(
        experiment_id="adapter-fixture", hypothesis="fixture", strategy="xs_momentum",
        strategy_version="1", dataset_version="fixture", git_sha="0" * 40,
        timeframe="4h", universe=list(paths), start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(), features=["close"],
        parameters={"data_files": paths, "strategy": {"formation_bars": 3, "vol_bars": 3,
                    "n_legs": 1}, "hold_bars": 2, "bars_per_year": 2190},
        search_space={}, baseline="equal_weight", cost_model={"taker_fee_bps": 5.9,
        "maker_fee_bps": 2.36, "base_slippage_bps": 2}, slippage={"kind": "fixed"},
        seed=1, execution_assumptions={"decision": "bar_close"}, artifacts=artifacts,
        environment={"python": "3.12"}, train_end=index[12].to_pydatetime(),
        validation_end=index[24].to_pydatetime(), holdout_end=index[-2].to_pydatetime(),
        max_drawdown=.9, bootstrap_block=2,
    )
    output = run_adapter("csv_xs_momentum", spec, tmp_path)
    assert len(output.net_returns) == len(output.baseline_returns) == len(output.turnover)
    assert output.timestamps == sorted(output.timestamps)
    assert all(timestamp.tzinfo == UTC for timestamp in output.timestamps)


def test_adapter_is_allowlisted(tmp_path):
    with pytest.raises(ValueError, match="Unknown research adapter"):
        run_adapter("arbitrary_python", None, tmp_path)


def test_bhavcopy_adapter_uses_registered_isin_point_in_time_artifacts(tmp_path):
    index = pd.date_range("2024-01-01", periods=320, freq="B", tz="UTC")
    rows = []
    isins = [f"INE{i:09d}" for i in range(12)]
    for offset, timestamp in enumerate(index):
        for rank, isin in enumerate(isins):
            price = 100 + rank + offset * (0.10 + rank / 1000)
            if rank == 11 and offset >= 270:
                price /= 2
            rows.append({"date": timestamp, "isin": isin, "open": price * .999,
                         "close": price, "turnover": 1_000_000 - rank * 1000})
    bhavcopy = tmp_path / "bhavcopy.parquet"
    pd.DataFrame(rows)[lambda data: data["date"] <= index[285]].to_parquet(bhavcopy, index=False)
    benchmark = tmp_path / "benchmark.csv"
    pd.DataFrame({"timestamp": index[:286], "close": [100 + i * .1 for i in range(286)]}).to_csv(
        benchmark, index=False,
    )
    actions = tmp_path / "actions.csv"
    actions.write_text(
        "isin,announced_date,ex_date,kind,share_multiplier,cash_per_share\n"
        f"{isins[11]},{index[260].date()},{index[270].date()},split,2,0\n"
    )
    artifacts = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in (bhavcopy, benchmark, actions)}
    spec = ExperimentSpec(
        experiment_id="pit-regime", hypothesis="fixture", strategy="momentum_6_12",
        strategy_version="1", dataset_version="fixture", git_sha="0" * 40,
        timeframe="1d", universe=["point-in-time-bhavcopy"], start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(), features=["open", "close", "turnover"],
        parameters={"bhavcopy_file": bhavcopy.name, "benchmark_file": benchmark.name,
                    "corporate_actions_file": actions.name, "family": "momentum_6_12",
                    "rebalance_bars": 5}, search_space={}, baseline="NIFTY200_MOMENTUM30_TRI",
        cost_model={"capital_inr": 10000}, slippage={"half_spread_bps": 2}, seed=1,
        execution_assumptions={"decision": "completed_close_next_open"}, artifacts=artifacts,
        environment={"python": "3.12"}, train_end=index[120].to_pydatetime(),
        validation_end=index[285].to_pydatetime(), holdout_end=index[310].to_pydatetime(),
        max_drawdown=.9, bootstrap_block=2,
    )
    output = run_adapter("bhavcopy_nse_regime", spec, tmp_path)
    assert output.timestamps and output.timestamps[-1] <= spec.validation_end
    assert min(output.net_returns) > -.5


def test_integrity_check_detects_future_data_dependency():
    class Cheater(PortfolioStrategy):
        def min_history(self):
            return 2

        def target_weights(self, panel, i):
            return panel.close.iloc[-1] / panel.close.iloc[-1].sum()

    index = pd.date_range("2025-01-01", periods=20, freq="D")
    close = pd.DataFrame({"A": range(1, 21), "B": range(21, 41)}, index=index)
    report = strategy_integrity(Cheater(), PricePanel(close, close.pct_change()))
    assert not report["passed"] and report["lookahead_failures"]
