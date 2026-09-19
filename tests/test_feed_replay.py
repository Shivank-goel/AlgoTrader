from src.data.quality import quote_quality
from src.data.replay import read_events
from src.fyers.journal import Journal


def tick(at, **changes):
    data = {"symbol": "NSE:TEST-EQ", "bid_price": 99., "ask_price": 100.,
            "bid_size": 10, "ask_size": 10, "exch_feed_time": at}
    data.update(changes)
    return {"kind": "tick", "received": at, "data": data}


def quality(events):
    return quote_quality(events, ["NSE:TEST-EQ"], start=100, end=200, stale_seconds=10)


def test_staleness_and_disconnect_bound_coverage():
    events = [tick(100), {"kind": "disconnected", "received": 105, "data": {}}, tick(120)]
    row = quality(events)["symbols"]["NSE:TEST-EQ"]
    assert row["usable_seconds"] == 15
    assert row["coverage_fraction"] == .15
    assert row["unusable_seconds"] == 85


def test_invalid_duplicate_and_out_of_order_events():
    events = [tick(100), tick(101, exch_feed_time=100), tick(102, exch_feed_time=99), tick(120, ask_price=98)]
    row = quality(events)["symbols"]["NSE:TEST-EQ"]
    assert (row["duplicates"], row["out_of_order"], row["invalid"]) == (1, 1, 1)
    assert row["usable_seconds"] == 2


def test_replay_is_read_only_and_deterministic(tmp_path):
    path = tmp_path / "feed.db"
    journal = Journal(path)
    event = tick(100)
    journal.event(event["kind"], event["data"], event["received"])
    journal.close()
    first, digest = read_events(path)
    second, again = read_events(path)
    assert first == second and digest == again
    assert quality(first) == quality(second)


def test_empty_feed_is_not_healthy():
    row = quality([])["symbols"]["NSE:TEST-EQ"]
    assert row["coverage_fraction"] == 0
    assert row["median_spread_bps"] is None
