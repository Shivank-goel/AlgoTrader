"""Tests for the microstructure recorder.

Its only job is to not lose data and not crash the caller. It builds no
features, because these fields have no history and therefore cannot be
backtested — recording now is purely about starting the clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.market.microstructure import MicrostructureRecorder, MicrostructureSnapshot


@pytest.fixture
def recorder(tmp_path):
    return MicrostructureRecorder(str(tmp_path / "micro.parquet"), flush_every=1000)


def _snap(symbol="BTCUSD", ts=None, **kw):
    return MicrostructureSnapshot(
        timestamp=ts or datetime(2026, 8, 10, 12, 0),
        symbol=symbol,
        **kw,
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parses_a_real_ticker_payload():
    """Field names taken from a live Delta response."""
    ticker = {
        "mark_price": "64856.90415043",
        "spot_price": "64880.4",
        "mark_basis": "-0.00036214",
        "funding_rate": "0.010000000000000002",
        "oi": "1187.4770",
        "oi_value_usd": "77044220.2462",
        "oi_change_usd_6h": "2674462.9000",
        "turnover_usd": "469181374.22249967",
    }
    snap = MicrostructureSnapshot.from_ticker("BTCUSD", ticker)

    assert snap.symbol == "BTCUSD"
    assert snap.mark_price == pytest.approx(64856.90415043)
    assert snap.funding_rate == pytest.approx(0.01)
    assert snap.oi_value_usd == pytest.approx(77044220.2462)


def test_missing_and_malformed_fields_become_zero():
    snap = MicrostructureSnapshot.from_ticker("X", {"mark_price": "not-a-number"})
    assert snap.mark_price == 0.0
    assert snap.funding_rate == 0.0


def test_annualised_funding_conversion():
    """0.01% per 8h is three periods a day: ~11%/yr, the venue's floor rate."""
    snap = _snap(funding_rate=0.01)
    assert snap.annualised_funding_pct == pytest.approx(10.95, abs=0.01)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_records_and_reloads(recorder):
    recorder.record([_snap("BTCUSD", funding_rate=0.01), _snap("ETHUSD")])
    recorder.flush()

    df = recorder.load()
    assert len(df) == 2
    assert set(df["symbol"]) == {"BTCUSD", "ETHUSD"}


def test_appends_across_flushes(recorder):
    recorder.record([_snap("BTCUSD", ts=datetime(2026, 8, 10, 12, 0))])
    recorder.flush()
    recorder.record([_snap("BTCUSD", ts=datetime(2026, 8, 10, 13, 0))])
    recorder.flush()

    assert len(recorder.load()) == 2, "second flush overwrote the first"


def test_same_symbol_and_timestamp_is_deduped(recorder):
    """A restart mid-scan must not double-count a sample."""
    ts = datetime(2026, 8, 10, 12, 0)
    recorder.record([_snap("BTCUSD", ts=ts, mark_price=100.0)])
    recorder.flush()
    recorder.record([_snap("BTCUSD", ts=ts, mark_price=200.0)])
    recorder.flush()

    df = recorder.load()
    assert len(df) == 1
    assert df.iloc[0]["mark_price"] == 200.0, "later sample should win"


def test_rows_are_stored_in_time_order(recorder):
    recorder.record([
        _snap("BTCUSD", ts=datetime(2026, 8, 10, 14, 0)),
        _snap("BTCUSD", ts=datetime(2026, 8, 10, 12, 0)),
    ])
    recorder.flush()
    assert recorder.load()["timestamp"].is_monotonic_increasing


def test_auto_flush_at_the_buffer_limit(tmp_path):
    rec = MicrostructureRecorder(str(tmp_path / "m.parquet"), flush_every=3)
    base = datetime(2026, 8, 10, 12, 0)
    rec.record([_snap("A", ts=base + timedelta(minutes=i)) for i in range(3)])
    assert len(rec.load()) == 3, "buffer should have auto-flushed"


def test_flushing_an_empty_buffer_is_a_no_op(recorder):
    assert recorder.flush() == 0


def test_load_filters_by_symbol(recorder):
    recorder.record([_snap("BTCUSD"), _snap("ETHUSD")])
    recorder.flush()
    assert list(recorder.load("ETHUSD")["symbol"]) == ["ETHUSD"]


def test_load_on_a_missing_file_is_empty(tmp_path):
    assert MicrostructureRecorder(str(tmp_path / "nope.parquet")).load().empty


def test_snapshots_without_a_symbol_are_dropped(recorder):
    assert recorder.record([_snap(""), _snap("BTCUSD")]) == 1


def test_record_tickers_skips_empty_payloads(recorder):
    assert recorder.record_tickers({"BTCUSD": {"mark_price": "1"}, "ETHUSD": {}}) == 1


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats_on_empty(tmp_path):
    stats = MicrostructureRecorder(str(tmp_path / "e.parquet")).stats()
    assert stats == {"rows": 0, "symbols": 0, "span_hours": 0.0}


def test_stats_reports_span_and_symbol_count(recorder):
    base = datetime(2026, 8, 10, 12, 0)
    recorder.record([
        _snap("BTCUSD", ts=base),
        _snap("ETHUSD", ts=base),
        _snap("BTCUSD", ts=base + timedelta(hours=6)),
    ])
    recorder.flush()

    stats = recorder.stats()
    assert stats["rows"] == 3
    assert stats["symbols"] == 2
    assert stats["span_hours"] == pytest.approx(6.0)


def test_a_corrupt_file_does_not_raise(tmp_path):
    path = tmp_path / "bad.parquet"
    path.write_text("not parquet")
    assert MicrostructureRecorder(str(path)).load().empty
