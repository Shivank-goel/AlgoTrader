"""Allowlisted, deterministic adapters from frozen specifications to real backtests."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pandas as pd

from src.backtest.costs import CostModel
from src.backtest.nse_regime_backtest import run_regime_backtest
from src.backtest.portfolio_backtest import PortfolioBacktester
from src.fyers.costs import FyersCosts
from src.research.models import ExperimentOutput, ExperimentSpec
from src.research.validation import strategy_integrity
from src.strategies.nse_regime_selector import (
    FrozenFamilyEvidence,
    RegimeAwareSelector,
    SelectorConfig,
    StrategyFamily,
)
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


def _enforce_sealed_source(index: pd.DatetimeIndex, spec: ExperimentSpec, label: str) -> None:
    """Development artifacts must physically stop at the validation boundary."""
    if index.empty:
        raise ValueError(f"{label} is empty")
    boundary = pd.Timestamp(
        spec.validation_end if spec.evaluation_stage == "development" else spec.holdout_end,
    ).tz_convert("UTC")
    if index.max() > boundary:
        raise ValueError(f"{label} contains data beyond the registered {spec.evaluation_stage} boundary")


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
        _enforce_sealed_source(frames[symbol].index, spec, f"data file for {symbol}")
    panel = PricePanel.from_frames(frames)
    start = pd.Timestamp(spec.start).tz_convert("UTC")
    effective_end = spec.validation_end if spec.evaluation_stage == "development" else spec.holdout_end
    end = pd.Timestamp(effective_end).tz_convert("UTC")
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
    timestamps = [timestamp.to_pydatetime().astimezone(UTC) for timestamp in result.timestamps]
    output = ExperimentOutput(timestamps=timestamps, net_returns=result.period_returns,
                              baseline_returns=baseline, turnover=result.period_turnover)
    return _stage_output(output, spec)


def _stage_output(output: ExperimentOutput, spec: ExperimentSpec) -> ExperimentOutput:
    if spec.evaluation_stage != "holdout":
        return output
    keep = [index for index, stamp in enumerate(output.timestamps)
            if spec.validation_end < stamp <= spec.holdout_end]
    if len(keep) < 2:
        raise ValueError("Holdout produced too few observations")
    return ExperimentOutput(
        timestamps=[output.timestamps[i] for i in keep],
        net_returns=[output.net_returns[i] for i in keep],
        baseline_returns=[output.baseline_returns[i] for i in keep],
        turnover=[output.turnover[i] for i in keep],
    )


def csv_nse_regime(spec: ExperimentSpec, root: Path) -> ExperimentOutput:
    """Registered daily close artifacts through whole-share FYERS economics."""
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
        _enforce_sealed_source(frames[symbol].index, spec, f"data file for {symbol}")
    closes = pd.DataFrame({symbol: frame["close"] for symbol, frame in frames.items()})
    opens = pd.DataFrame({symbol: frame["open"] for symbol, frame in frames.items()
                          if "open" in frame})
    if set(opens) != set(closes):
        raise ValueError("NSE regime research requires open and close for every symbol")
    effective_end = spec.validation_end if spec.evaluation_stage == "development" else spec.holdout_end
    window = slice(pd.Timestamp(spec.start).tz_convert("UTC"), pd.Timestamp(effective_end).tz_convert("UTC"))
    closes, opens = closes.loc[window], opens.loc[window]
    benchmark_file = spec.parameters.get("benchmark_file")
    if not isinstance(benchmark_file, str) or benchmark_file not in spec.artifacts:
        raise ValueError("NSE regime research requires a registered benchmark_file")
    benchmark_frame = _csv_frame((root / benchmark_file).resolve())
    _enforce_sealed_source(benchmark_frame.index, spec, "benchmark file")
    benchmark = benchmark_frame["close"].reindex(closes.index)
    if benchmark.isna().any():
        raise ValueError("Registered benchmark does not cover every strategy bar")
    benchmark.name = spec.baseline
    family = StrategyFamily(spec.parameters.get("family"))
    selector = RegimeAwareSelector(SelectorConfig(
        min_regime_confidence=float(spec.parameters.get("min_regime_confidence", .60)),
        top_n=int(spec.parameters.get("top_n", 5)), capital_inr=10000, max_order_inr=2000,
        evidence=[FrozenFamilyEvidence(family=item, lower_confidence_bound=0, qualified=False)
                  for item in StrategyFamily]))
    output = run_regime_backtest(
        closes, selector, family,
        rebalance_bars=int(spec.parameters.get("rebalance_bars", 5)),
        half_spread_bps=float(spec.slippage.get("half_spread_bps", 0)), costs=FyersCosts.load(),
        opens=opens, benchmark_close=benchmark)
    return _stage_output(ExperimentOutput(
        timestamps=output.timestamps, net_returns=output.net_returns,
        baseline_returns=output.baseline_returns, turnover=output.turnover,
    ), spec)


def bhavcopy_nse_regime(spec: ExperimentSpec, root: Path) -> ExperimentOutput:
    """Survivorship-safe regime research from a registered ISIN-keyed long artifact."""
    from src.data.nse.bhavcopy import build_panel, point_in_time_universe
    from src.data.nse.corporate_actions import load_corporate_actions

    relative = spec.parameters.get("bhavcopy_file")
    if not isinstance(relative, str) or relative not in spec.artifacts:
        raise ValueError("bhavcopy_file must be a registered artifact")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Bhavcopy artifact escapes root")
    frame = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    required = {"date", "isin", "open", "close", "turnover"}
    if not required <= set(frame):
        raise ValueError("Bhavcopy artifact lacks point-in-time OHLC/liquidity fields")
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    _enforce_sealed_source(pd.DatetimeIndex(frame["date"]), spec, "bhavcopy file")
    effective_end = spec.validation_end if spec.evaluation_stage == "development" else spec.holdout_end
    frame = frame[(frame["date"] >= pd.Timestamp(spec.start)) &
                  (frame["date"] <= pd.Timestamp(effective_end))]
    if frame.duplicated(["date", "isin"]).any() or frame["isin"].isna().any():
        raise ValueError("Bhavcopy artifact has duplicate or missing ISIN identity")
    closes = build_panel(frame, "close")
    opens = build_panel(frame, "open").reindex_like(closes)
    if closes.shape[1] < 10:
        raise ValueError("Bhavcopy artifact needs at least 10 point-in-time securities")
    benchmark_file = spec.parameters.get("benchmark_file")
    if not isinstance(benchmark_file, str) or benchmark_file not in spec.artifacts:
        raise ValueError("Point-in-time regime research requires a registered benchmark_file")
    benchmark_frame = _csv_frame((root / benchmark_file).resolve())
    _enforce_sealed_source(benchmark_frame.index, spec, "benchmark file")
    benchmark = benchmark_frame["close"].reindex(closes.index)
    if benchmark.isna().any():
        raise ValueError("Registered benchmark does not cover every bhavcopy session")
    benchmark.name = spec.baseline
    actions_file = spec.parameters.get("corporate_actions_file")
    if not isinstance(actions_file, str) or actions_file not in spec.artifacts:
        raise ValueError("Point-in-time research requires a registered corporate_actions_file")
    actions = load_corporate_actions((root / actions_file).resolve())
    boundary = (spec.validation_end if spec.evaluation_stage == "development" else spec.holdout_end).date()
    if any(action.ex_date > boundary for action in actions):
        raise ValueError("corporate-action artifact crosses the sealed evaluation boundary")
    actions_by_date: dict[pd.Timestamp, list[dict]] = {}
    for action in actions:
        timestamp = pd.Timestamp(action.ex_date, tz="UTC")
        if timestamp in closes.index and action.isin in closes:
            actions_by_date.setdefault(timestamp, []).append(action.model_dump(mode="json"))
    # Build a point-in-time total-return signal index. An action changes the
    # return only on its ex-date; no future adjustment is visible beforehand.
    signal_closes = closes.copy()
    for symbol in closes:
        series = closes[symbol]
        synthetic = series.copy()
        valid = series.dropna()
        if valid.empty:
            continue
        level = float(valid.iloc[0])
        synthetic.loc[:] = float("nan")
        synthetic.loc[valid.index[0]] = level
        previous = float(valid.iloc[0])
        for timestamp, raw_close in valid.iloc[1:].items():
            multiplier, cash = 1, 0.0
            for action in actions_by_date.get(timestamp, []):
                if action["isin"] == symbol:
                    multiplier *= int(action["share_multiplier"])
                    cash += float(action["cash_per_share"])
            level *= (float(raw_close) * multiplier + cash) / previous
            synthetic.loc[timestamp] = level
            previous = float(raw_close)
        signal_closes[symbol] = synthetic
    family = StrategyFamily(spec.parameters.get("family"))
    selector = RegimeAwareSelector(SelectorConfig(
        min_regime_confidence=float(spec.parameters.get("min_regime_confidence", .60)),
        top_n=int(spec.parameters.get("top_n", 5)), capital_inr=10000, max_order_inr=2000,
        evidence=[FrozenFamilyEvidence(family=item, lower_confidence_bound=0, qualified=False)
                  for item in StrategyFamily]))

    def eligible(at: pd.Timestamp) -> set[str]:
        return set(point_in_time_universe(
            frame, at, lookback_days=int(spec.parameters.get("liquidity_lookback_days", 60)),
            top_n=int(spec.parameters.get("liquid_universe_size", 200)),
            min_price=float(spec.parameters.get("min_price", 5)),
        ))

    output = run_regime_backtest(
        closes, selector, family,
        rebalance_bars=int(spec.parameters.get("rebalance_bars", 5)),
        half_spread_bps=float(spec.slippage.get("half_spread_bps", 0)), costs=FyersCosts.load(),
        opens=opens, benchmark_close=benchmark, eligible_at=eligible,
        signal_closes=signal_closes, corporate_actions=actions_by_date,
    )
    return _stage_output(ExperimentOutput(
        timestamps=output.timestamps, net_returns=output.net_returns,
        baseline_returns=output.baseline_returns, turnover=output.turnover,
    ), spec)


ADAPTERS = {"csv_xs_momentum": csv_xs_momentum, "csv_nse_regime": csv_nse_regime,
            "bhavcopy_nse_regime": bhavcopy_nse_regime}


def run_adapter(name: str, spec: ExperimentSpec, root: Path) -> ExperimentOutput:
    try:
        adapter = ADAPTERS[name]
    except KeyError:
        raise ValueError(f"Unknown research adapter: {name}") from None
    return adapter(spec, root)
