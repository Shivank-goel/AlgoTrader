"""Allowlisted, deterministic adapters from frozen specifications to real backtests."""

from __future__ import annotations

from datetime import timezone
from pathlib import Path

import pandas as pd

from src.backtest.costs import CostModel
from src.backtest.portfolio_backtest import PortfolioBacktester
from src.research.models import ExperimentOutput, ExperimentSpec
from src.research.validation import strategy_integrity
from src.strategies.portfolio import PricePanel
from src.strategies.xs_momentum import CrossSectionalMomentum


def _csv_frame(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if "timestamp" not in frame or "close" not in frame:
        raise ValueError("Research CSV requires timestamp and close columns")
    timestamps = pd.to_datetime(frame.pop("timestamp"), utc=True, errors="raise")
    frame.index = timestamps
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError("Research timestamps must be unique and ordered")
    if frame["close"].isna().any() or (frame["close"] <= 0).any():
        raise ValueError("Research closes must be positive and complete")
    return frame


def csv_xs_momentum(spec: ExperimentSpec, root: Path) -> ExperimentOutput:
    """Run the repository's portfolio momentum backtest from registered CSV artifacts.

    `parameters.data_files` maps every registered universe symbol to a path which
    must also be present in the specification's hashed artifact manifest.
    """
    data_files = spec.parameters.get("data_files")
    if not isinstance(data_files, dict) or set(data_files) != set(spec.universe):
        raise ValueError("parameters.data_files must map every universe symbol")
    frames = {}
    for symbol, relative in data_files.items():
        if not isinstance(relative, str) or relative not in spec.artifacts:
            raise ValueError("Every research data file must be a registered artifact")
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("Research data file escapes root")
        frames[symbol] = _csv_frame(path)
    panel = PricePanel.from_frames(frames)
    start = pd.Timestamp(spec.start).tz_convert("UTC")
    end = pd.Timestamp(spec.end).tz_convert("UTC")
    panel = PricePanel(panel.close.loc[start:end], panel.returns.loc[start:end])
    strategy_parameters = spec.parameters.get("strategy", {})
    if not isinstance(strategy_parameters, dict):
        raise ValueError("parameters.strategy must be an object")
    hold_bars = spec.parameters.get("hold_bars")
    if type(hold_bars) is not int or hold_bars <= 0:
        raise ValueError("parameters.hold_bars must be a positive integer")
    costs = CostModel.from_config({"costs": spec.cost_model})
    strategy = CrossSectionalMomentum(strategy_parameters)
    integrity = strategy_integrity(strategy, panel)
    if not integrity["passed"]:
        raise ValueError(f"Strategy integrity check failed: {integrity}")
    result = PortfolioBacktester(costs, bars_per_year=float(spec.parameters.get("bars_per_year", 2190))).run(
        strategy, panel, hold_bars=hold_bars)
    if len(result.period_returns) < 2:
        raise ValueError("Backtest produced too few observations")
    locations = panel.close.index.get_indexer(result.timestamps)
    if (locations < 0).any():
        raise ValueError("Backtest timestamps do not map to registered data")
    baseline = []
    for location in locations:
        later = location + hold_bars
        if later >= len(panel.close):
            raise ValueError("Baseline extends beyond registered data")
        baseline.append(float((panel.close.iloc[later] / panel.close.iloc[location] - 1).mean()))
    timestamps = [timestamp.to_pydatetime().astimezone(timezone.utc) for timestamp in result.timestamps]
    return ExperimentOutput(timestamps=timestamps, net_returns=result.period_returns,
                            baseline_returns=baseline, turnover=result.period_turnover)


ADAPTERS = {"csv_xs_momentum": csv_xs_momentum}


def run_adapter(name: str, spec: ExperimentSpec, root: Path) -> ExperimentOutput:
    try:
        adapter = ADAPTERS[name]
    except KeyError:
        raise ValueError(f"Unknown research adapter: {name}") from None
    return adapter(spec, root)
