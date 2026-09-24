from datetime import UTC, datetime

import pytest

from src.fyers.costs import FyersCosts
from src.fyers.journal import Journal
from src.fyers.models import Instrument, Quote, RuntimeConfig
from src.fyers.paper import PaperBroker
from src.shadow.continuous import StrategyCandidate
from src.shadow.shadow_scheduler import ShadowIntentScheduler


def candidate(**updates):
    data = {"id": "candidate_v1", "kind": "momentum", "symbols": ["NSE:SBIN-EQ", "NSE:INFY-EQ"],
            "interval_seconds": 60, "lookback_intervals": 2, "horizon_intervals": 1,
            "top_n": 1, "capital_inr": 2000, "lifecycle": "CANDIDATE", "shadow_enabled": True}
    data.update(updates)
    return StrategyCandidate.model_validate(data)


def instruments():
    return {s: Instrument(symbol=s, isin="INE000A00000", lot_size=1, tick_size="0.05")
            for s in ["NSE:SBIN-EQ", "NSE:INFY-EQ"]}


def test_unqualified_registered_candidate_creates_shadow_intent(tmp_path):
    journal = Journal(tmp_path / "runtime.db")
    scheduler = ShadowIntentScheduler(journal, RuntimeConfig.load(), instruments(), candidate())
    result = scheduler.schedule({"NSE:SBIN-EQ": 1}, {}, at=datetime.now(UTC),
                                market_regime="trending_up", decision_id="d1", reason="test")
    assert result[0].execution_mode == "SHADOW"
    assert journal.db.execute("SELECT COUNT(*) FROM scheduled_intents").fetchone()[0] == 1
    journal.close()


def test_disallowed_regime_produces_no_intent(tmp_path):
    journal = Journal(tmp_path / "runtime.db")
    scheduler = ShadowIntentScheduler(journal, RuntimeConfig.load(), instruments(), candidate())
    assert scheduler.schedule({"NSE:SBIN-EQ": 1}, {}, at=datetime.now(UTC),
                              market_regime="falling", decision_id="d1", reason="test") == []
    journal.close()


def test_non_shadow_candidate_is_rejected(tmp_path):
    journal = Journal(tmp_path / "runtime.db")
    with pytest.raises(ValueError, match="shadow execution"):
        ShadowIntentScheduler(journal, RuntimeConfig.load(), instruments(), candidate(shadow_enabled=False))
    journal.close()


def test_shadow_intent_reaches_paper_broker_without_qualification(tmp_path):
    journal = Journal(tmp_path / "runtime.db")
    config = RuntimeConfig.load().model_copy(update={"database": str(tmp_path / "runtime.db")})
    instrument = instruments()["NSE:SBIN-EQ"]
    intent = ShadowIntentScheduler(journal, config, instruments(), candidate()).schedule(
        {"NSE:SBIN-EQ": 1}, {}, at=datetime.fromtimestamp(1000, UTC),
        market_regime="trending_up", decision_id="d1", reason="test",
    )[0]
    quote = Quote(symbol="NSE:SBIN-EQ", bid=99.9, ask=100, bid_size=10, ask_size=10,
                  exchange_time=1000, received_time=1000)
    fill = PaperBroker(journal, config, FyersCosts.load()).fill(
        intent, instrument, {intent.symbol: quote}, now=1000, market_open=True,
    )
    assert fill["intent_id"] == intent.intent_id
    journal.close()
