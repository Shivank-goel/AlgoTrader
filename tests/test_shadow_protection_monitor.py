from datetime import UTC, datetime

from src.fyers.costs import FyersCosts
from src.fyers.journal import Journal
from src.fyers.models import Instrument, Intent, Quote, RuntimeConfig, Side
from src.fyers.paper import PaperBroker
from src.fyers.protection_monitor import ShadowProtectionMonitor


def _open(tmp_path):
    config = RuntimeConfig.load().model_copy(update={"database": str(tmp_path / "runtime.db")})
    journal = Journal(tmp_path / "runtime.db")
    broker = PaperBroker(journal, config, FyersCosts.load())
    instrument = Instrument(symbol="NSE:SBIN-EQ", isin="INE062A01020", lot_size=1, tick_size="0.1")
    quote = Quote(symbol=instrument.symbol, bid=10, ask=10.1, bid_size=100, ask_size=100,
                  received_time=1000, exchange_time=1000)
    intent = Intent(intent_id="entry", strategy="fixture", symbol=instrument.symbol, side=Side.BUY,
                    quantity=10, created_at=datetime.fromtimestamp(1000, UTC),
                    stop_loss=8, take_profit=14, protection_required=True)
    from unittest.mock import patch
    with patch("src.fyers.paper.qualification", return_value=(True, "fixture")):
        broker.fill(intent, instrument, {instrument.symbol: quote}, now=1001, market_open=True)
    return journal, broker, instrument


def test_stop_crossing_uses_bid_gap_and_is_idempotent(tmp_path):
    journal, broker, instrument = _open(tmp_path)
    monitor = ShadowProtectionMonitor(broker, {instrument.symbol: instrument})
    for bid in (8.5, 8.2):
        quote = Quote(symbol=instrument.symbol, bid=bid, ask=bid + .1, bid_size=100, ask_size=100,
                      received_time=1002, exchange_time=1002)
        assert monitor.evaluate(quote, now=1002, market_open=True) == []
    quote = Quote(symbol=instrument.symbol, bid=7, ask=7.1, bid_size=100, ask_size=100,
                  received_time=1003, exchange_time=1003)
    result = monitor.evaluate(quote, now=1003, market_open=True)
    assert result[0]["status"] == "EXECUTED"
    trigger = journal.db.execute("SELECT * FROM position_exit_triggers").fetchone()
    assert trigger["trigger_type"] == "STOP_LOSS"
    assert trigger["protection_level"] == 8
    assert trigger["trigger_price"] == 7
    assert trigger["gap_from_level"] == -1
    assert journal.db.execute("SELECT quantity FROM positions").fetchone()[0] == 0
    assert journal.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2
    assert monitor.evaluate(quote, now=1004, market_open=True) == []
    assert journal.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2


def test_take_profit_crossing_and_stale_quotes_are_safe(tmp_path):
    journal, broker, instrument = _open(tmp_path)
    monitor = ShadowProtectionMonitor(broker, {instrument.symbol: instrument})
    stale = Quote(symbol=instrument.symbol, bid=15, ask=15.1, bid_size=100, ask_size=100,
                  received_time=1, exchange_time=1)
    assert monitor.evaluate(stale, now=1003, market_open=True) == []
    quote = Quote(symbol=instrument.symbol, bid=14.5, ask=14.6, bid_size=100, ask_size=100,
                  received_time=1003, exchange_time=1003)
    result = monitor.evaluate(quote, now=1003, market_open=True)
    assert result[0]["fill"]["price"] < 14.5
    assert journal.db.execute("SELECT trigger_type FROM position_exit_triggers").fetchone()[0] == "TAKE_PROFIT"
