from datetime import UTC, datetime

import pytest

from src.fyers.costs import FyersCosts
from src.fyers.journal import Journal
from src.fyers.models import Instrument, Intent, Quote, RuntimeConfig, Side
from src.fyers.paper import PaperBroker
from src.strategies.protection import atr_protection, structural_protection


def _case(tmp_path, *, stop=95.0, target=110.0, required=True):
    config = RuntimeConfig.load().model_copy(update={"database": str(tmp_path / "runtime.db")})
    journal = Journal(tmp_path / "runtime.db")
    broker = PaperBroker(journal, config, FyersCosts.load())
    instrument = Instrument(symbol="NSE:SBIN-EQ", isin="INE062A01020", lot_size=1, tick_size="0.1")
    quote = Quote(symbol=instrument.symbol, bid=99, ask=100, bid_size=100, ask_size=100,
                  received_time=1000, exchange_time=1000)
    intent = Intent(intent_id="protected", strategy="fixture", symbol=instrument.symbol,
                    side=Side.BUY, quantity=10, created_at=datetime.fromtimestamp(1000, UTC),
                    stop_loss=stop, take_profit=target, protection_required=required,
                    decision_id="decision-1", strategy_version="v1", market_regime="trending_up")
    return journal, broker, instrument, {instrument.symbol: quote}, intent


def test_protection_uses_actual_fill_price_and_persists(tmp_path, monkeypatch):
    journal, broker, instrument, quotes, intent = _case(tmp_path)
    monkeypatch.setattr("src.fyers.paper.qualification", lambda *_: (True, "fixture"))
    fill = broker.fill(intent, instrument, quotes, now=1001, market_open=True)
    row = journal.db.execute("SELECT * FROM position_protection").fetchone()
    assert fill["price"] == 100.1
    assert row["entry_fill_price"] == fill["price"]
    assert row["quantity"] == 10
    assert row["initial_risk_per_share"] == pytest.approx(5.1)
    assert row["initial_risk_rupees"] == pytest.approx(51)
    assert row["capital_committed"] == pytest.approx(1001)
    assert row["status"] == "ACTIVE"
    assert journal.db.execute("SELECT COUNT(*) FROM position_protection").fetchone()[0] == 1
    PaperBroker(journal, broker.config, broker.costs)
    assert journal.db.execute("SELECT COUNT(*) FROM position_protection").fetchone()[0] == 1


@pytest.mark.parametrize("stop,target", [(100.1, 110), (95, 100.1)])
def test_invalid_long_protection_rejected(tmp_path, monkeypatch, stop, target):
    journal, broker, instrument, quotes, intent = _case(tmp_path, stop=stop, target=target)
    monkeypatch.setattr("src.fyers.paper.qualification", lambda *_: (True, "fixture"))
    with pytest.raises(ValueError, match="protection"):
        broker.fill(intent, instrument, quotes, now=1001, market_open=True)
    assert journal.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert journal.db.execute("SELECT COUNT(*) FROM position_protection").fetchone()[0] == 0


def test_required_protection_missing_fails_closed(tmp_path, monkeypatch):
    journal, broker, instrument, quotes, intent = _case(tmp_path, stop=None, target=None, required=True)
    monkeypatch.setattr("src.fyers.paper.qualification", lambda *_: (True, "fixture"))
    with pytest.raises(ValueError, match="required"):
        broker.fill(intent, instrument, quotes, now=1001, market_open=True)
    assert journal.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_strategy_protection_plans_are_deterministic_and_point_in_time():
    first = atr_protection(family="momentum_6_12", entry_reference_price=100,
                           atr=2, stop_atr_multiplier=2, reward_risk_multiple=2)
    second = atr_protection(family="momentum_6_12", entry_reference_price=100,
                            atr=2, stop_atr_multiplier=2, reward_risk_multiple=2)
    assert first == second
    assert first.stop_loss_price == 96
    assert first.take_profit_price == 108
    structural = structural_protection(family="donchian_breakout", entry_reference_price=100,
                                       invalidation_level=95, target_level=None)
    assert structural.take_profit_price is None
    with pytest.raises(ValueError):
        structural_protection(family="residual_reversal", entry_reference_price=100,
                              invalidation_level=101, target_level=105)
