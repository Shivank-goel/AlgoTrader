import json
from pathlib import Path

import pytest

from src.fyers.costs import FyersCosts
from src.fyers.models import Side
from src.research.nse_engine import _slip
from src.strategies.nse_regime_selector import RegimeAwareSelector, SelectorConfig, StrategyFamily


def test_manual_buy_sell_slippage_and_fyers_costs():
    costs = FyersCosts.load()
    raw_entry, raw_exit, quantity = 100.0, 110.0, 19
    entry = raw_entry * 1.0002
    exit_price = raw_exit * 0.9998
    entry_notional = entry * quantity
    exit_notional = exit_price * quantity
    expected_entry_fee = costs.fee(entry_notional, Side.BUY, delivery=True)
    expected_exit_fee = costs.fee(exit_notional, Side.SELL, delivery=True)
    assert _slip(raw_entry, 2, buy=True) == pytest.approx(entry)
    assert _slip(raw_exit, 2, buy=False) == pytest.approx(exit_price)
    assert entry_notional <= 2000
    assert expected_entry_fee > 0 and expected_exit_fee > 0
    assert (exit_price - entry) * quantity - expected_entry_fee - expected_exit_fee > 0


def test_metric_contracts_are_identical_across_families():
    paths = [
        Path("config/research/nse_trials/momentum_6_12_v5.json"),
        Path("config/research/nse_trials/donchian_breakout_v6.json"),
        Path("config/research/nse_trials/residual_reversal_v7.json"),
    ]
    specs = [json.loads(path.read_text()) for path in paths]
    assert len({json.dumps(spec["equity_marking"], sort_keys=True) for spec in specs}) == 1
    assert len({json.dumps(spec["benchmark_evaluation"], sort_keys=True) for spec in specs}) == 1


def test_allocation_contract_preserves_no_redistribution():
    spec = json.loads(Path("config/research/nse_trials/momentum_6_12_v5.json").read_text())
    rule = spec["allocation_rule"]
    assert rule["position_budget_inr"] == 2000
    assert rule["max_positions"] == 5
    assert rule["unaffordable"] == "NO_TRADE_NO_REDISTRIBUTION"
    assert rule["entry_exit_order"] == "EXITS_BEFORE_ENTRIES"


def test_strategy_scores_are_point_in_time_and_deterministic():
    index = __import__("pandas").date_range("2024-01-01", periods=260, freq="D")
    closes = __import__("pandas").DataFrame({"NSE:A-EQ": range(100, 360), "NSE:B-EQ": range(100, 360)}, index=index)
    selector = RegimeAwareSelector(SelectorConfig(evidence=[]))
    before = selector.scores(StrategyFamily.MOMENTUM, closes.iloc[:253])
    after = selector.scores(StrategyFamily.MOMENTUM, closes.iloc[:254])
    assert list(before.index) == list(after.index)
    assert before.equals(selector.scores(StrategyFamily.MOMENTUM, closes.iloc[:253]))
    assert len(before) == 2
