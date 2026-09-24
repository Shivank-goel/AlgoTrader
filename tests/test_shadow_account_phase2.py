from datetime import UTC, datetime

from src.fyers.costs import FyersCosts
from src.fyers.journal import Journal
from src.fyers.models import Instrument, Intent, Quote, RuntimeConfig, Side
from src.fyers.paper import PaperBroker


def _intent(name: str, side: Side = Side.BUY) -> Intent:
    return Intent(intent_id=name, strategy="qualified_test_fixture", symbol="NSE:SBIN-EQ",
                  side=side, quantity=1, created_at=datetime.fromtimestamp(1000, UTC))


def _setup(tmp_path):
    config = RuntimeConfig.load().model_copy(update={"database": str(tmp_path / "runtime.db")})
    journal = Journal(tmp_path / "runtime.db")
    broker = PaperBroker(journal, config, FyersCosts.load())
    instrument = Instrument(symbol="NSE:SBIN-EQ", isin="INE062A01020", lot_size=1, tick_size="0.1")
    quote = Quote(symbol=instrument.symbol, bid=99, ask=100, bid_size=100, ask_size=100,
                  received_time=1000, exchange_time=1000)
    return journal, broker, instrument, {instrument.symbol: quote}


def test_one_persistent_simulation_and_start_checkpoint(tmp_path):
    journal, broker, *_ = _setup(tmp_path)
    simulation = journal.db.execute("SELECT * FROM shadow_simulations").fetchall()
    assert len(simulation) == 1
    assert simulation[0]["initial_capital"] == 10_000
    assert journal.get("simulation_id") == simulation[0]["simulation_id"]
    first = journal.get("simulation_id")
    restarted = PaperBroker(journal, broker.config, broker.costs)
    assert journal.get("simulation_id") == first
    assert restarted.journal.db.execute("SELECT COUNT(*) FROM shadow_simulations").fetchone()[0] == 1


def test_fill_and_marks_create_idempotent_checkpoints(tmp_path, monkeypatch):
    journal, broker, instrument, quotes = _setup(tmp_path)
    monkeypatch.setattr("src.fyers.paper.qualification", lambda *_: (True, "qualified_test_fixture"))
    broker.fill(_intent("buy"), instrument, quotes, now=1001, market_open=True)
    count = journal.db.execute("SELECT COUNT(*) FROM equity_checkpoints WHERE event_type='FILL'").fetchone()[0]
    broker.fill(_intent("buy"), instrument, quotes, now=1001, market_open=True)
    assert journal.db.execute("SELECT COUNT(*) FROM equity_checkpoints WHERE event_type='FILL'").fetchone()[0] == count
    assert journal.db.execute("SELECT COUNT(*) FROM equity_checkpoints WHERE event_type='SIMULATION_START'").fetchone()[0] == 1
    assert journal.get("simulation_id")
