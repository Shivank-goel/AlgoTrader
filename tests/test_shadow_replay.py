from datetime import datetime, timezone

import pytest

from src.fyers.costs import FyersCosts
from src.fyers.journal import Journal
from src.fyers.models import Instrument, Intent, RuntimeConfig
from src.fyers.paper import PaperBroker
from src.shadow.runtime import replay_session


def run(tmp_path, name):
    journal = Journal(tmp_path / name)
    config = RuntimeConfig.load()
    paper = PaperBroker(journal, config, FyersCosts.load())
    symbol = config.symbols[0]
    instrument = Instrument(symbol=symbol, isin="fixture", lot_size=1, tick_size=".1")
    intent = Intent(intent_id="fixture", strategy="synthetic", symbol=symbol, side="BUY", quantity=1,
                    created_at=datetime.fromtimestamp(1000, timezone.utc))
    events = [{"kind": "connected", "received": 1000, "data": {}},
              {"kind": "account_check", "received": 1000, "data": {"market_open": True}},
              {"kind": "tick", "received": 1001, "data": {"symbol": symbol, "bid_price": 99, "ask_price": 100,
                  "bid_size": 10, "ask_size": 10, "exch_feed_time": 1001}}]
    try:
        return replay_session(events, {symbol: instrument}, [intent], paper)
    finally:
        journal.close()


def test_shadow_replay_uses_existing_qualification_gate(tmp_path, monkeypatch):
    monkeypatch.setattr("src.fyers.paper.qualification", lambda *_: (False, "not qualified"))
    result = run(tmp_path, "blocked.db")
    assert result["decisions"][0]["status"] == "rejected"
    assert not result["live_enabled"]


def test_shadow_replay_is_deterministic_with_synthetic_authorization(tmp_path, monkeypatch):
    # Unit fixture only. Never writes a qualification artifact or repository trial.
    monkeypatch.setattr("src.fyers.paper.qualification", lambda *_: (True, "synthetic"))
    first, second = run(tmp_path, "first.db"), run(tmp_path, "second.db")
    assert first == second
    assert first["decisions"][0]["status"] == "filled"
    assert first["forward_metrics"]["filled"] == 1
    assert first["forward_metrics"]["fill_rate"] == 1
    assert first["forward_metrics"]["mean_execution_shortfall_bps"] > 0
    assert first["valuation"]["equity"] < RuntimeConfig.load().capital_inr
