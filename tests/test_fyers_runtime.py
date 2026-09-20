import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from src.fyers.costs import FyersCosts
from src.fyers.instruments import parse_instruments
from src.fyers.journal import Journal
from src.fyers.models import Instrument, Intent, Quote, RuntimeConfig, Side
from src.fyers.paper import PaperBroker
from src.fyers.qualification import qualification
from src.fyers.runtime import nse_open, parse_quote


@pytest.fixture
def setup(tmp_path):
    config = RuntimeConfig.load()
    journal = Journal(tmp_path / "paper.db")
    broker = PaperBroker(journal, config, FyersCosts.load())
    instrument = Instrument(symbol="NSE:SBIN-EQ", isin="INE062A01020", lot_size=1, tick_size="0.1")
    quote = Quote(symbol=instrument.symbol, bid=99, ask=100, bid_size=100, ask_size=100,
                  received_time=1000, exchange_time=1000)
    with patch("src.fyers.paper.qualification", return_value=(True, "qualified_test_fixture")):
        yield journal, broker, instrument, {instrument.symbol: quote}
    journal.close()


def intent(side=Side.BUY, key="one", qty=1):
    return Intent(intent_id=key, strategy="qualified_test_fixture", symbol="NSE:SBIN-EQ", side=side,
                  quantity=qty, created_at=datetime.fromtimestamp(1000, UTC))


def test_duplicate_fill_and_restart_are_idempotent(setup):
    journal, broker, instrument, quotes = setup
    first = broker.fill(intent(), instrument, quotes, now=1001, market_open=True)
    cash = journal.get("cash")
    restarted = PaperBroker(journal, broker.config, broker.costs)
    assert restarted.fill(intent(), instrument, quotes, now=1001, market_open=True) == first
    assert journal.get("cash") == cash
    assert first["price"] == 100.1  # ask + adverse slippage rounded up to tick
    assert journal.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    with pytest.raises(ValueError, match="reused"):
        broker.fill(intent(qty=2), instrument, quotes, now=1001, market_open=True)


@pytest.mark.parametrize("case", ["stale", "future", "crossed", "missing", "closed", "liquidity", "short", "size", "unqualified", "halt"])
def test_fill_risk_rejections_leave_cash_unchanged(setup, case):
    journal, broker, instrument, quotes = setup
    request = intent()
    now, market_open = 1001, True
    if case == "stale":
        now = 2000
    elif case == "future":
        quotes[instrument.symbol].exchange_time = 1100
    elif case == "crossed":
        quotes[instrument.symbol].bid = 110
    elif case == "missing":
        quotes.clear()
    elif case == "closed":
        market_open = False
    elif case == "liquidity":
        request = intent(qty=101)
    elif case == "short":
        request = intent(Side.SELL)
    elif case == "size":
        request = intent(qty=30)
    elif case == "unqualified":
        request.strategy = "unqualified"
    elif case == "halt":
        journal.halt("operator")
    with pytest.raises(ValueError):
        broker.fill(request, instrument, quotes, now=now, market_open=market_open)
    assert journal.get("cash") == 10000
    assert journal.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_dp_charged_once_per_isin_day(setup):
    journal, broker, instrument, quotes = setup
    broker.fill(intent(qty=2), instrument, quotes, now=1001, market_open=True)
    first = broker.fill(intent(Side.SELL, "sell1"), instrument, quotes, now=1002, market_open=True)
    second = broker.fill(intent(Side.SELL, "sell2"), instrument, quotes, now=1003, market_open=True)
    assert first["fee"] - second["fee"] == pytest.approx(14.75)
    assert journal.db.execute("SELECT quantity FROM positions").fetchone()[0] == 0
    assert journal.db.execute("SELECT COUNT(*) FROM settlement_obligations").fetchone()[0] == 3
    assert journal.db.execute("SELECT SUM(remaining) FROM tax_lots").fetchone()[0] == 0
    assert journal.db.execute("SELECT SUM(quantity) FROM realized_tax_lots").fetchone()[0] == 2


def test_persisted_halt_survives_database_reopen(tmp_path):
    path = tmp_path / "paper.db"
    journal = Journal(path)
    journal.halt("manual stop")
    journal.close()
    reopened = Journal(path)
    assert reopened.get("halt_reason") == "manual stop"
    reopened.close()


def test_daily_loss_latches_halt(setup):
    journal, broker, instrument, quotes = setup
    with journal.db:
        journal.put("day_equity", {"day": "1970-01-01", "value": 10300})
    with pytest.raises(ValueError, match="loss limit"):
        broker.fill(intent(), instrument, quotes, now=1001, market_open=True)
    assert journal.get("halt_reason") == "daily paper loss limit"


def test_published_fyers_tariff():
    costs = FyersCosts.load()
    assert costs.fee(10000, Side.BUY, delivery=False) + costs.fee(10000, Side.SELL, delivery=False) == pytest.approx(10.62812)
    assert costs.fee(10000, Side.BUY, delivery=True) + costs.fee(10000, Side.SELL, delivery=True, charge_dp=True) == pytest.approx(84.19812)
    with pytest.raises(ValueError):
        costs.fee(float("nan"), Side.BUY, delivery=True)


def test_master_lookup_does_not_guess():
    master = {"NSE:SBIN-EQ": {"isin": "INE062A01020", "minLotSize": 1, "tickSize": .1}}
    assert parse_instruments(master, ["NSE:SBIN-EQ"])["NSE:SBIN-EQ"].lot_size == 1
    with pytest.raises(ValueError):
        parse_instruments(master, ["NSE:UNKNOWN-EQ"])


def test_market_session_fails_closed():
    assert not nse_open({})
    row = {"exchange": 10, "segment": 10, "market_type": "NORMAL", "status": "OPEN"}
    assert nse_open({"marketStatus": [row]})
    row["status"] = "PREOPEN"
    assert not nse_open({"marketStatus": [row]})


def test_quote_requires_exchange_timestamp():
    with pytest.raises(KeyError):
        parse_quote({"symbol": "NSE:SBIN-EQ", "bid_price": 99, "ask_price": 100,
                     "bid_size": 10, "ask_size": 10}, 1000)


def test_qualification_missing_or_corrupt_fails_closed(tmp_path):
    path = tmp_path / "evidence.json"
    registry = tmp_path / "trials.json"
    assert not qualification(path, registry)[0]
    path.write_text(json.dumps({"passed": True}))
    registry.write_text(json.dumps({"count": 0, "trials": []}))
    assert not qualification(path, registry)[0]


def test_qualification_recomputes_and_invalidates_changed_registry(tmp_path, monkeypatch):
    from src.backtest.statistics import sharpe_ratio
    from src.fyers.models import ROOT
    from src.fyers.qualification import digest

    original_root = ROOT
    (tmp_path / "config").mkdir()
    (tmp_path / "config/fyers_costs.yaml").write_bytes((original_root / "config/fyers_costs.yaml").read_bytes())
    (tmp_path / "config/fyers_economics.yaml").write_bytes((original_root / "config/fyers_economics.yaml").read_bytes())
    monkeypatch.setattr("src.fyers.qualification.ROOT", tmp_path)

    # Synthetic unit fixture, never registered in the repository's trial history.
    returns = [0.4, -0.1, 0.3, 0.2] * 100
    data = tmp_path / "data.json"
    data.write_text(json.dumps(returns))
    trials = tmp_path / "trials.json"
    trials.write_text(json.dumps({"count": 2, "trials": [
        {"name": "fixture", "n_obs": len(returns), "sharpe": sharpe_ratio(returns)},
        {"name": "fixture2", "n_obs": len(returns), "sharpe": sharpe_ratio(returns) + .01},
    ]}))
    evidence = tmp_path / "evidence.json"
    code = tmp_path / "strategy.py"
    code.write_text("# reviewed fixture\n")
    config = tmp_path / "strategy.yaml"
    config.write_text("enabled: false\n")
    evidence.write_text(json.dumps({"broker": "fyers", "strategy": "fixture",
        "trial_name": "fixture", "registry_sha256": digest(trials),
        "costs_sha256": digest(tmp_path / "config/fyers_costs.yaml"),
        "data_path": str(data), "data_sha256": digest(data),
        "reviewed_net_costs_and_data": True, "net_returns": returns,
        "forward_evidence": {"accepted": True, "observations": 20,
                             "selector_sha256": "a" * 64,
                             "start": "2025-01-01T00:00:00+00:00",
                             "end": "2025-07-03T00:00:00+00:00"},
        "capital_inr": 10000,
        "economic_evidence": {"benchmark_id": "NIFTY200_MOMENTUM30_TRI",
                              "benchmark_artifact_path": str(data),
                              "benchmark_artifact_sha256": digest(data),
                              "excess_lower_confidence_bound": 0.01,
                              "after_tax_and_infrastructure_excess": 0.1,
                              "tax_policy_sha256": digest(tmp_path / "config/fyers_economics.yaml"),
                              "break_even_capital_inr": 5000},
        "holdout_evidence": {"untouched_before_evaluation": True,
                             "experiment_id": "fixture-holdout",
                             "mean_excess_return": 0.1,
                             "report_path": str(data), "report_sha256": digest(data)},
        "code_artifacts": {str(code.relative_to(tmp_path)): digest(code)},
        "config_artifacts": {str(config.relative_to(tmp_path)): digest(config)}}))
    assert qualification(evidence, trials) == (True, "fixture")
    trials.write_text(trials.read_text() + "\n")
    assert not qualification(evidence, trials)[0]


def test_paper_cash_cannot_be_overdrawn(setup):
    journal, broker, instrument, quotes = setup
    broker.config.max_order_inr = 20000
    with pytest.raises(ValueError, match="Insufficient paper cash"):
        broker.fill(intent(qty=100), instrument, quotes, now=1001, market_open=True)
    assert journal.get("cash") == 10000


@pytest.mark.parametrize("restriction", ["halt", "qualification", "order_cap", "removed_symbol"])
def test_valid_exit_remains_available_under_entry_restrictions(setup, restriction):
    journal, broker, instrument, quotes = setup
    broker.fill(intent(qty=2), instrument, quotes, now=1001, market_open=True)
    if restriction == "halt":
        journal.halt("operator")
    elif restriction == "order_cap":
        broker.config.max_order_inr = 1
    elif restriction == "removed_symbol":
        broker.config.symbols = ["NSE:INFY-EQ"]
    # Every reduction must work with invalid qualification, never checking a gate
    # intended to approve new risk. This is not permission for uncovered sells.
    with patch("src.fyers.paper.qualification", return_value=(False, "expired")) as gate:
        result = broker.fill(intent(Side.SELL, "exit", qty=2), instrument, quotes,
                             now=1002, market_open=True)
    gate.assert_not_called()
    assert result["quantity"] == 2
    assert journal.db.execute("SELECT quantity FROM positions").fetchone()[0] == 0
    with pytest.raises(ValueError, match="short selling"):
        broker.fill(intent(Side.SELL, "extra"), instrument, quotes, now=1003, market_open=True)


@pytest.mark.parametrize("problem", ["stale", "closed", "over_sell"])
def test_halted_exit_still_requires_a_valid_fill(setup, problem):
    journal, broker, instrument, quotes = setup
    broker.fill(intent(), instrument, quotes, now=1001, market_open=True)
    journal.halt("operator")
    market_open = problem != "closed"
    if problem == "stale":
        quotes[instrument.symbol].exchange_time = 500
    request = intent(Side.SELL, "exit", qty=2 if problem == "over_sell" else 1)
    with pytest.raises(ValueError):
        broker.fill(request, instrument, quotes, now=1002, market_open=market_open)
    assert journal.db.execute("SELECT quantity FROM positions").fetchone()[0] == 1


def test_stale_unrelated_holding_blocks_entry_but_not_exit(setup):
    journal, broker, instrument, quotes = setup
    broker.fill(intent(), instrument, quotes, now=1001, market_open=True)
    other = Instrument(symbol="NSE:INFY-EQ", isin="OTHER", tick_size="0.1", lot_size=1)
    quotes[other.symbol] = quotes[instrument.symbol].model_copy(update={"symbol": other.symbol})
    buy = intent(key="other").model_copy(update={"symbol": other.symbol})
    broker.fill(buy, other, quotes, now=1002, market_open=True)
    del quotes[other.symbol]
    valuation = broker.mark_to_market(quotes, now=1003)
    assert not valuation["complete"] and valuation["equity"] is None
    with pytest.raises(ValueError, match="Incomplete valuation"):
        broker.fill(intent(key="new"), instrument, quotes, now=1004, market_open=True)
    broker.fill(intent(Side.SELL, "exit"), instrument, quotes, now=1005, market_open=True)
    assert journal.get("paper_valuation")["missing_symbols"] == [other.symbol]


def test_mark_to_market_halts_without_an_order(setup):
    journal, broker, instrument, quotes = setup
    broker.fill(intent(qty=10), instrument, quotes, now=1001, market_open=True)
    quotes[instrument.symbol].bid = 50
    valuation = broker.mark_to_market(quotes, now=1002)
    assert valuation["daily_pnl"] < -200
    assert journal.get("halt_reason") == "daily paper loss limit"
    assert journal.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    broker.mark_to_market(quotes, now=1003)
    assert journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='halt'").fetchone()[0] == 1


def test_overnight_gap_uses_persisted_prior_equity(setup):
    journal, broker, instrument, quotes = setup
    broker.fill(intent(qty=10), instrument, quotes, now=1001, market_open=True)
    prior = broker.mark_to_market(quotes, now=1002)["equity"]
    restarted = PaperBroker(journal, broker.config, broker.costs)
    quotes[instrument.symbol] = quotes[instrument.symbol].model_copy(update={
        "bid": 50, "ask": 51, "exchange_time": 87400, "received_time": 87400,
    })
    value = restarted.mark_to_market(quotes, now=87401)
    assert journal.get("day_equity")["value"] == prior
    assert value["daily_pnl"] == pytest.approx(-490)
    assert journal.get("halt_reason") == "daily paper loss limit"


def test_pnl_and_fees_reconcile_and_legacy_accounting_recovers(setup):
    journal, broker, instrument, quotes = setup
    buy = broker.fill(intent(qty=2), instrument, quotes, now=1001, market_open=True)
    sell = broker.fill(intent(Side.SELL, "exit"), instrument, quotes, now=1002, market_open=True)
    value = broker.mark_to_market(quotes, now=1003)
    assert value["equity"] - 10000 == pytest.approx(value["realized_pnl"] + value["unrealized_pnl"])
    assert value["total_fees"] == pytest.approx(buy["fee"] + sell["fee"])
    with journal.db:
        journal.db.execute("DELETE FROM state WHERE key IN ('position_cost_basis','realized_pnl','total_fees')")
    recovered = PaperBroker(journal, broker.config, broker.costs)
    restored = recovered.mark_to_market(quotes, now=1004)
    for key in ("equity", "realized_pnl", "unrealized_pnl", "total_fees"):
        assert restored[key] == pytest.approx(value[key])


def test_entry_cannot_spend_remaining_daily_loss_budget(setup):
    journal, broker, instrument, quotes = setup
    broker.config.max_daily_loss_inr = 0.5
    with pytest.raises(ValueError, match="Entry costs"):
        broker.fill(intent(), instrument, quotes, now=1001, market_open=True)
    assert journal.get("cash") == 10000
    assert journal.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_observer_monitors_existing_position_without_intents(tmp_path, monkeypatch):
    import asyncio
    import time
    from unittest.mock import AsyncMock, MagicMock

    from src.fyers import runtime

    config = RuntimeConfig.load().model_copy(update={"database": str(tmp_path / "runtime.db")})
    now = time.time()
    symbol = "NSE:SBIN-EQ"
    instrument = Instrument(symbol=symbol, isin="ISIN", lot_size=1, tick_size="0.1")
    journal = Journal(tmp_path / "runtime.db")
    broker = PaperBroker(journal, config, FyersCosts.load())
    quote = Quote(symbol=symbol, bid=99, ask=100, bid_size=100, ask_size=100,
                  received_time=now, exchange_time=now)
    request = intent(qty=10).model_copy(update={"created_at": datetime.fromtimestamp(now, UTC)})
    with patch("src.fyers.paper.qualification", return_value=(True, "qualified_test_fixture")):
        broker.fill(request, instrument, {symbol: quote}, now=now, market_open=True)
    journal.close()

    client = MagicMock()
    client.get_profile = AsyncMock(return_value={"s": "ok"})
    client.get_positions = AsyncMock(return_value={"netPositions": []})
    client.get_orders = AsyncMock(return_value={"orderBook": []})
    client.get_trades = AsyncMock(return_value={"tradeBook": []})
    client.get_market_status = AsyncMock(return_value={"marketStatus": []})
    client.close = AsyncMock()
    events = [
        {"kind": "connected", "received": now, "data": {}},
        {"kind": "tick", "received": now, "data": {
            "symbol": symbol, "bid_price": 50, "ask_price": 51,
            "bid_size": 100, "ask_size": 100, "exch_feed_time": now,
        }},
    ]

    async def readline():
        if events:
            return (json.dumps(events.pop(0)) + "\n").encode()
        await asyncio.Event().wait()

    process = MagicMock(returncode=None)
    process.stdout.readline = readline
    process.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(runtime.RuntimeConfig, "load", lambda: config)
    monkeypatch.setattr(runtime, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.FyersClient, "from_env", lambda: client)
    monkeypatch.setattr(runtime, "resolve_instruments", AsyncMock(return_value={symbol: instrument}))
    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    summary = await runtime.observe(duration=0.05)
    assert summary["ticks"] == 1
    process.terminate.assert_called_once()
    journal = Journal(tmp_path / "runtime.db")
    assert journal.get("halt_reason") == "daily paper loss limit"
    assert journal.get("paper_valuation")["daily_pnl"] < -200
    assert journal.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    journal.close()
