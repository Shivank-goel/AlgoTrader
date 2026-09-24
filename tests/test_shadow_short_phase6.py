from datetime import UTC, datetime
from unittest.mock import patch

from src.fyers.costs import FyersCosts
from src.fyers.journal import Journal
from src.fyers.models import Instrument, Intent, Quote, RuntimeConfig, Side
from src.fyers.paper import PaperBroker
from src.fyers.protection_monitor import ShadowProtectionMonitor


def test_short_entry_cover_profit_and_gap_stop(tmp_path):
    config = RuntimeConfig.load().model_copy(update={"database": str(tmp_path / "runtime.db")})
    journal = Journal(tmp_path / "runtime.db")
    paper = PaperBroker(journal, config, FyersCosts.load())
    instrument = Instrument(symbol="NSE:SBIN-EQ", isin="INE062A01020", lot_size=1, tick_size="0.1")
    entry_quote = Quote(symbol=instrument.symbol, bid=100, ask=100.1, bid_size=20, ask_size=20,
                        received_time=1000, exchange_time=1000)
    entry = Intent(intent_id="short-entry", strategy="fixture", symbol=instrument.symbol,
                   side=Side.SELL, quantity=5, created_at=datetime.fromtimestamp(1000, UTC),
                   position_side="SHORT", intraday_only=True, protection_required=True,
                   stop_loss=105, take_profit=90)
    with patch("src.fyers.paper.qualification", return_value=(True, "fixture")):
        paper.fill(entry, instrument, {instrument.symbol: entry_quote}, now=1001, market_open=True)
    assert journal.db.execute("SELECT quantity FROM short_positions").fetchone()[0] == 5
    monitor = ShadowProtectionMonitor(paper, {instrument.symbol: instrument})
    cover_quote = entry_quote.model_copy(update={"bid": 89, "ask": 89.1, "received_time": 1002, "exchange_time": 1002})
    result = monitor.evaluate(cover_quote, now=1002, market_open=True)
    assert result[0]["status"] == "EXECUTED"
    assert journal.db.execute("SELECT COUNT(*) FROM short_positions").fetchone()[0] == 0
    trigger = journal.db.execute("SELECT trigger_type,price_source FROM position_exit_triggers").fetchone()
    assert trigger[0] == "TAKE_PROFIT"
    assert trigger[1] == "ASK"


def test_short_stop_uses_ask_and_session_close_is_explicit(tmp_path):
    config = RuntimeConfig.load().model_copy(update={"database": str(tmp_path / "runtime.db")})
    journal = Journal(tmp_path / "runtime.db")
    paper = PaperBroker(journal, config, FyersCosts.load())
    instrument = Instrument(symbol="NSE:SBIN-EQ", isin="INE062A01020", lot_size=1, tick_size="0.1")
    quote = Quote(symbol=instrument.symbol, bid=100, ask=100, bid_size=20, ask_size=20,
                  received_time=1000, exchange_time=1000)
    intent = Intent(intent_id="short-entry", strategy="fixture", symbol=instrument.symbol,
                    side=Side.SELL, quantity=1, created_at=datetime.fromtimestamp(1000, UTC),
                    position_side="SHORT", intraday_only=True, protection_required=True,
                    stop_loss=105, take_profit=90)
    with patch("src.fyers.paper.qualification", return_value=(True, "fixture")):
        paper.fill(intent, instrument, {instrument.symbol: quote}, now=1001, market_open=True)
    monitor = ShadowProtectionMonitor(paper, {instrument.symbol: instrument})
    gap = quote.model_copy(update={"bid": 109, "ask": 110, "received_time": 1002, "exchange_time": 1002})
    result = monitor.evaluate(gap, now=1002, market_open=True)
    assert result[0]["status"] == "EXECUTED"
    row = journal.db.execute("SELECT trigger_type,trigger_price FROM position_exit_triggers").fetchone()
    assert row[0] == "STOP_LOSS" and row[1] == 110
