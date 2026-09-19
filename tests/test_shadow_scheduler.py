from datetime import datetime, timezone

import pytest

from src.fyers.journal import Journal
from src.fyers.models import Instrument, RuntimeConfig
from src.shadow.scheduler import QualifiedIntentScheduler


def test_scheduler_is_qualification_gated_and_idempotent(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "scheduler.db")
    config = RuntimeConfig.load()
    symbol = config.symbols[0]
    instrument = Instrument(symbol=symbol, isin="fixture", lot_size=1, tick_size=".05")
    monkeypatch.setattr("src.shadow.scheduler.qualification", lambda *_: (True, "fixture"))
    monkeypatch.setattr("src.shadow.scheduler.digest", lambda *_: "a" * 64)
    scheduler = QualifiedIntentScheduler(journal, config, {symbol: instrument}, "fixture")
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = scheduler.schedule({symbol: 3}, {symbol: 1}, at=at)
    second = scheduler.schedule({symbol: 3}, {symbol: 1}, at=at)
    assert first == second and first[0].quantity == 2
    assert journal.db.execute("SELECT COUNT(*) FROM scheduled_intents").fetchone()[0] == 1
    journal.close()


def test_scheduler_fails_closed_without_qualification(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "scheduler.db")
    config = RuntimeConfig.load()
    symbol = config.symbols[0]
    instrument = Instrument(symbol=symbol, isin="fixture", lot_size=1, tick_size=".05")
    monkeypatch.setattr("src.shadow.scheduler.qualification", lambda *_: (False, "blocked"))
    scheduler = QualifiedIntentScheduler(journal, config, {symbol: instrument}, "fixture")
    with pytest.raises(ValueError, match="not currently qualified"):
        scheduler.schedule({symbol: 1}, {}, at=datetime.now(timezone.utc))
    journal.close()
