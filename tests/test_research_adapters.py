from datetime import datetime, timezone
import hashlib

import pandas as pd
import pytest

from src.research.adapters import run_adapter
from src.research.models import ExperimentSpec
from src.research.validation import strategy_integrity
from src.strategies.portfolio import PricePanel, PortfolioStrategy


def test_registered_csv_adapter_runs_real_backtest_without_future_rows(tmp_path):
    index = pd.date_range("2025-01-01", periods=40, freq="4h", tz="UTC")
    paths = {}
    artifacts = {}
    for number, symbol in enumerate(("A", "B", "C", "D"), 1):
        path = tmp_path / f"{symbol}.csv"
        pd.DataFrame({"timestamp": index, "close": [100 + number * i for i in range(40)]}).to_csv(path, index=False)
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
    assert all(timestamp.tzinfo == timezone.utc for timestamp in output.timestamps)


def test_adapter_is_allowlisted(tmp_path):
    with pytest.raises(ValueError, match="Unknown research adapter"):
        run_adapter("arbitrary_python", None, tmp_path)


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
