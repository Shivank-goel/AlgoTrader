import json

import pytest

from src.data.session import export_session, load_session
from src.fyers.journal import Journal


SYMBOL = "NSE:TEST-EQ"
MASTER = {SYMBOL: {"symbol": SYMBOL, "isin": "INE000TEST", "lot_size": 1,
                   "tick_size": "0.05", "trading_session": "NORMAL", "master_updated": "fixture"}}


def make_source(path):
    journal = Journal(path)
    journal.event("instruments", MASTER, 99)
    journal.event("connected", {}, 100)
    journal.event("tick", {"symbol": SYMBOL, "bid_price": 99, "ask_price": 100,
                            "bid_size": 10, "ask_size": 8, "exch_feed_time": 101}, 101)
    journal.event("disconnected", {}, 106)
    journal.event("tick", {"symbol": SYMBOL, "bid_price": 98, "ask_price": 99,
                            "bid_size": 4, "ask_size": 5, "exch_feed_time": 201}, 201)
    journal.close()


def test_session_export_is_bounded_reproducible_and_self_validating(tmp_path):
    source, output = tmp_path / "feed.db", tmp_path / "session.json"
    make_source(source)
    exported = export_session(source, output, start=100, end=110,
                              symbols=[SYMBOL], stale_seconds=10)
    loaded = load_session(output)
    assert loaded == exported
    assert loaded.session_sha256 == exported.session_sha256
    assert [event["received"] for event in loaded.events] == [100, 101, 106]
    assert loaded.quality["symbols"][SYMBOL]["usable_seconds"] == 5
    assert loaded.manifest.master_event_id == 1


def test_session_export_rejects_cross_day_window_and_overwrite(tmp_path):
    source, output = tmp_path / "feed.db", tmp_path / "session.json"
    make_source(source)
    export_session(source, output, start=100, end=110, symbols=[SYMBOL], stale_seconds=10)
    with pytest.raises(ValueError, match="already exists"):
        export_session(source, output, start=100, end=110, symbols=[SYMBOL], stale_seconds=10)
    with pytest.raises(ValueError, match="one Asia/Kolkata date"):
        export_session(source, tmp_path / "cross.json", start=0, end=86400,
                       symbols=[SYMBOL], stale_seconds=10)


def test_session_export_detects_tampering(tmp_path):
    source, output = tmp_path / "feed.db", tmp_path / "session.json"
    make_source(source)
    export_session(source, output, start=100, end=110, symbols=[SYMBOL], stale_seconds=10)
    body = json.loads(output.read_text())
    body["events"][1]["data"]["ask_price"] = 101
    output.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="Event hash mismatch"):
        load_session(output)
