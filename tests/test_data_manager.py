"""Tests for OHLCV caching and history backfill.

src/data/manager.py had no tests. Its cache write was a bare df.to_csv() of
just the freshly-fetched window, so every cache file was permanently pinned to
the last `limit` bars and running the bot never accumulated history. That made
walk-forward validation impossible, not merely unwired.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.core.models import Candle
from src.data.manager import MAX_CACHE_BARS, DataManager


class _StubExchange:
    """Serves candles from a preloaded frame, honouring start/end."""

    def __init__(self, df: pd.DataFrame | None = None):
        self.df = df if df is not None else pd.DataFrame()
        self.calls: list[tuple[datetime, datetime]] = []

    async def get_candles(self, symbol, timeframe, start=None, end=None, limit=500):
        self.calls.append((start, end))
        window = self.df
        if start is not None:
            window = window[window.index >= start]
        if end is not None:
            window = window[window.index <= end]
        window = window.tail(limit)
        return [
            Candle(
                timestamp=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                symbol=symbol,
                timeframe=timeframe,
            )
            for ts, row in window.iterrows()
        ]

    async def close(self):
        return None


def _frame(n: int, *, start: datetime, minutes: int = 15, base: float = 100.0) -> pd.DataFrame:
    index = pd.date_range(start, periods=n, freq=f"{minutes}min")
    return pd.DataFrame(
        {
            "open": [base + i for i in range(n)],
            "high": [base + i + 1 for i in range(n)],
            "low": [base + i - 1 for i in range(n)],
            "close": [base + i + 0.5 for i in range(n)],
            "volume": [1000.0 + i for i in range(n)],
        },
        index=index,
    )


@pytest.fixture
def manager(tmp_path):
    return DataManager(_StubExchange(), cache_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# Cache merge
# ---------------------------------------------------------------------------


def test_cache_write_accumulates_instead_of_truncating(manager, tmp_path):
    """The regression: a later, smaller fetch must not erase earlier history."""
    path = tmp_path / "BTCUSD_15m.csv"
    first = _frame(300, start=datetime(2026, 1, 1))
    later = _frame(50, start=datetime(2026, 1, 4, 3, 0))

    manager._write_cache(first, path)
    manager._write_cache(later, path)

    stored = manager._read_cache(path)
    assert len(stored) > len(later), "the smaller second write truncated the cache"
    assert stored.index.min() == first.index.min()
    assert stored.index.max() == later.index.max()


def test_cache_merge_keeps_the_newer_copy_of_a_repeated_bar(manager, tmp_path):
    """A partially-formed bar must be replaced by its completed version."""
    path = tmp_path / "BTCUSD_15m.csv"
    ts = datetime(2026, 1, 1)

    partial = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5], "volume": [10.0]},
        index=pd.DatetimeIndex([ts]),
    )
    completed = pd.DataFrame(
        {"open": [100.0], "high": [105.0], "low": [98.0], "close": [104.0], "volume": [900.0]},
        index=pd.DatetimeIndex([ts]),
    )

    manager._write_cache(partial, path)
    manager._write_cache(completed, path)

    stored = manager._read_cache(path)
    assert len(stored) == 1
    assert stored.iloc[0]["close"] == 104.0
    assert stored.iloc[0]["volume"] == 900.0


def test_cache_stays_sorted_when_written_out_of_order(manager, tmp_path):
    path = tmp_path / "BTCUSD_15m.csv"
    manager._write_cache(_frame(20, start=datetime(2026, 2, 1)), path)
    manager._write_cache(_frame(20, start=datetime(2026, 1, 1)), path)

    stored = manager._read_cache(path)
    assert stored.index.is_monotonic_increasing
    assert len(stored) == 40


def test_cache_is_bounded(manager, tmp_path):
    """Accumulation must not make the per-scan read unbounded."""
    path = tmp_path / "BTCUSD_15m.csv"
    manager._write_cache(_frame(MAX_CACHE_BARS + 500, start=datetime(2026, 1, 1)), path)

    assert len(manager._read_cache(path)) == MAX_CACHE_BARS


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


async def test_backfill_pages_until_the_requested_span_is_covered(tmp_path):
    start = datetime(2026, 1, 1)
    source = _frame(5000, start=start)
    manager = DataManager(_StubExchange(source), cache_dir=str(tmp_path))

    end = source.index.max()
    df = await manager.backfill(
        "BTCUSD", "15m", days=30, end=end, chunk_bars=500, pause_seconds=0
    )

    assert len(df) > 500, "backfill stopped after a single page"
    span_days = (df.index.max() - df.index.min()).total_seconds() / 86400
    assert span_days >= 29


async def test_backfill_persists_and_reloads_as_parquet(tmp_path):
    source = _frame(1000, start=datetime(2026, 1, 1))
    manager = DataManager(_StubExchange(source), cache_dir=str(tmp_path))

    await manager.backfill(
        "BTCUSD", "15m", days=10, end=source.index.max(), chunk_bars=500, pause_seconds=0
    )

    assert manager.hist_path("BTCUSD", "15m").exists()
    reloaded = manager.load_history("BTCUSD", "15m")
    assert not reloaded.empty
    assert list(reloaded.columns[:5]) == ["open", "high", "low", "close", "volume"]


async def test_backfill_merges_with_previously_stored_history(tmp_path):
    source = _frame(3000, start=datetime(2026, 1, 1))
    manager = DataManager(_StubExchange(source), cache_dir=str(tmp_path))

    mid = source.index[1500]
    await manager.backfill(
        "BTCUSD", "15m", days=10, end=mid, chunk_bars=500, pause_seconds=0
    )
    before = len(manager.load_history("BTCUSD", "15m"))

    await manager.backfill(
        "BTCUSD", "15m", days=10, end=source.index.max(), chunk_bars=500, pause_seconds=0
    )
    after = len(manager.load_history("BTCUSD", "15m"))

    assert after > before, "second backfill replaced history instead of merging"


async def test_backfill_terminates_when_the_server_ignores_start(tmp_path):
    """A server that returns the same window forever must not hang the loop."""
    source = _frame(100, start=datetime(2026, 1, 1))

    class _StuckExchange(_StubExchange):
        async def get_candles(self, symbol, timeframe, start=None, end=None, limit=500):
            self.calls.append((start, end))
            return await super().get_candles(symbol, timeframe, start=None, end=None, limit=limit)

    manager = DataManager(_StuckExchange(source), cache_dir=str(tmp_path))
    df = await manager.backfill(
        "BTCUSD", "15m", days=365, end=datetime(2027, 1, 1), chunk_bars=100, pause_seconds=0
    )

    assert len(manager.exchange.calls) < 20, "backfill looped on a non-advancing cursor"
    assert isinstance(df, pd.DataFrame)


async def test_backfill_returns_empty_frame_when_source_has_nothing(tmp_path):
    manager = DataManager(_StubExchange(pd.DataFrame()), cache_dir=str(tmp_path))
    df = await manager.backfill("NEWCOIN", "15m", days=30, pause_seconds=0)
    assert df.empty


async def test_backfill_rejects_an_unknown_timeframe(tmp_path):
    manager = DataManager(_StubExchange(), cache_dir=str(tmp_path))
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        await manager.backfill("BTCUSD", "7s", days=1)


# ---------------------------------------------------------------------------
# Feed quality
# ---------------------------------------------------------------------------


def test_describe_history_flags_a_synthetic_feed():
    """Flat bars are the tell that separated testnet caches from real prices."""
    index = pd.date_range(datetime(2026, 1, 1), periods=100, freq="15min")
    df = pd.DataFrame(
        {
            "open": [100.0] * 100,
            "high": [100.0] * 100,
            "low": [100.0] * 100,
            "close": [100.0] * 100,
            "volume": [0.0] * 100,
        },
        index=index,
    )

    info = DataManager.describe_history(df)
    assert info["flat_bars"] == 100
    assert info["flat_pct"] == 100.0
    assert info["zero_volume_bars"] == 100


def test_describe_history_on_a_healthy_feed():
    info = DataManager.describe_history(_frame(96 * 30, start=datetime(2026, 1, 1)))
    assert info["flat_pct"] == 0.0
    assert info["days"] == pytest.approx(30, abs=0.1)


def test_describe_history_handles_no_data():
    assert DataManager.describe_history(pd.DataFrame()) == {"bars": 0}
