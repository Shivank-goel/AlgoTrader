"""Historical and real-time data management with local CSV cache."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from src.core.models import Candle
from src.data.indicators import IndicatorEngine
from src.execution.exchange import DeltaExchangeClient

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

# The hot CSV cache accumulates rather than truncating, but stays bounded so
# per-scan reads remain cheap. Deep history lives in parquet under data/hist/.
MAX_CACHE_BARS = 20_000

# Delta caps /v2/history/candles at 4000 bars per response; page well under it.
BACKFILL_CHUNK_BARS = 2_000

BAR_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "1d": 86400,
}


class DataManager:
    """Fetches, caches, and serves OHLCV data across multiple timeframes."""

    def __init__(
        self,
        exchange: DeltaExchangeClient,
        cache_dir: str = "data",
        indicator_engine: Optional[IndicatorEngine] = None,
    ) -> None:
        self.exchange = exchange
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.hist_dir = self.cache_dir / "hist"
        self.indicators = indicator_engine or IndicatorEngine()
        self._last_update: dict[str, datetime] = {}

    def _cache_path(self, symbol: str, timeframe: str) -> Path:
        return self.cache_dir / f"{symbol}_{timeframe}.csv"

    def hist_path(self, symbol: str, timeframe: str) -> Path:
        return self.hist_dir / f"{symbol}_{timeframe}.parquet"

    def _read_cache(self, cache_path: Path) -> pd.DataFrame:
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        return df

    @staticmethod
    def _merge_ohlcv(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
        """Union two OHLCV frames on the timestamp index, newest data winning.

        The partially-formed final bar of an earlier fetch must be replaced by
        the completed bar from a later one, so `new` takes precedence.
        """
        if existing is None or existing.empty:
            combined = new
        elif new is None or new.empty:
            combined = existing
        else:
            combined = pd.concat([existing, new])

        combined = combined[~combined.index.duplicated(keep="last")]
        return combined.sort_index()

    def _write_cache(self, df: pd.DataFrame, cache_path: Path) -> None:
        """Merge into the cache instead of overwriting it.

        This used to be a bare df.to_csv(), so each fetch replaced the file with
        only the freshly requested window. Every cache was therefore pinned to
        exactly the last `limit` bars (3001 lines for the 3000-bar universe
        scan), and no amount of running the bot ever accumulated history.
        """
        try:
            existing = self._read_cache(cache_path) if cache_path.exists() else None
        except Exception:
            logger.warning("Cache read failed during merge for %s; overwriting", cache_path.name)
            existing = None

        try:
            merged = self._merge_ohlcv(existing, df)
            if len(merged) > MAX_CACHE_BARS:
                merged = merged.tail(MAX_CACHE_BARS)
            merged.to_csv(cache_path)
        except Exception as exc:
            logger.warning("Cache write failed for %s: %s", cache_path.name, exc)

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        use_cache: bool = True,
    ) -> list[Candle]:
        cache_key = f"{symbol}_{timeframe}"
        cache_path = self._cache_path(symbol, timeframe)

        if use_cache and cache_path.exists():
            try:
                loop = asyncio.get_event_loop()
                df = await loop.run_in_executor(None, self._read_cache, cache_path)
                if len(df) >= limit:
                    cached_candles = self._df_to_candles(df.tail(limit), symbol, timeframe)
                    cache_mtime = datetime.utcfromtimestamp(cache_path.stat().st_mtime)
                    last_update = self._last_update.get(cache_key, cache_mtime)
                    age = datetime.utcnow() - last_update
                    if age < timedelta(minutes=15):
                        return cached_candles
            except Exception:
                logger.warning("Cache read failed for %s", cache_key)

        candles: list[Candle] = []
        try:
            candles = await self.exchange.get_candles(symbol, timeframe, limit=limit)
        except Exception as exc:
            logger.warning("Exchange candle fetch failed for %s: %s", cache_key, exc)

        if candles:
            df = self.indicators.candles_to_df(candles)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._write_cache, df, cache_path)
            self._last_update[cache_key] = datetime.utcnow()
            return candles

        if cache_path.exists():
            try:
                loop = asyncio.get_event_loop()
                df = await loop.run_in_executor(None, self._read_cache, cache_path)
                if not df.empty:
                    logger.warning("Using stale cache for %s (exchange unreachable)", cache_key)
                    return self._df_to_candles(df.tail(limit), symbol, timeframe)
            except Exception:
                logger.warning("Stale cache read failed for %s", cache_key)

        return []

    async def get_dataframe(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        with_indicators: bool = True,
    ) -> pd.DataFrame:
        cache_key = f"{symbol}_{timeframe}"
        candles = await self.fetch_candles(symbol, timeframe, limit=limit)
        if not candles:
            return pd.DataFrame()

        df = self.indicators.candles_to_df(candles)
        if with_indicators:
            try:
                df = self.indicators.compute_all(df)
            except Exception as exc:
                logger.warning(
                    "Indicator computation failed for %s %s: %s — using raw OHLCV",
                    symbol,
                    timeframe,
                exc,
            )
        return df

    async def get_multi_timeframe(
        self,
        symbol: str,
        timeframes: list[str],
        limit: int = 200,
    ) -> dict[str, pd.DataFrame]:
        result = {}
        for tf in timeframes:
            result[tf] = await self.get_dataframe(symbol, tf, limit=limit)
        return result

    # ------------------------------------------------------------------
    # Deep history (parquet) — for backtesting and walk-forward validation
    # ------------------------------------------------------------------

    def load_history(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """Read backfilled history. Returns an empty frame when absent."""
        path = self.hist_path(symbol, timeframe)
        if not path.exists():
            return pd.DataFrame()

        try:
            df = pd.read_parquet(path)
        except Exception as exc:
            logger.warning("History read failed for %s %s: %s", symbol, timeframe, exc)
            return pd.DataFrame()

        if start is not None:
            df = df[df.index >= start]
        if end is not None:
            df = df[df.index <= end]
        return df

    def _write_history(self, df: pd.DataFrame, symbol: str, timeframe: str) -> int:
        """Merge into the parquet history file. Returns total rows stored."""
        self.hist_dir.mkdir(parents=True, exist_ok=True)
        path = self.hist_path(symbol, timeframe)

        existing = self.load_history(symbol, timeframe)
        merged = self._merge_ohlcv(existing, df)
        merged.index.name = "timestamp"
        merged.to_parquet(path)
        return len(merged)

    async def backfill(
        self,
        symbol: str,
        timeframe: str,
        *,
        days: int = 365,
        end: Optional[datetime] = None,
        exchange: Optional[DeltaExchangeClient] = None,
        chunk_bars: int = BACKFILL_CHUNK_BARS,
        pause_seconds: float = 0.25,
    ) -> pd.DataFrame:
        """Page /v2/history/candles backwards and store the result as parquet.

        `exchange` overrides the instance client so history can be pulled from
        production while the engine itself stays pointed at testnet — real
        prices must not require flipping exchange.testnet.
        """
        client = exchange or self.exchange
        bar_seconds = BAR_SECONDS.get(timeframe)
        if bar_seconds is None:
            raise ValueError(f"Unsupported timeframe for backfill: {timeframe}")

        end = end or datetime.utcnow()
        target_start = end - timedelta(days=days)

        frames: list[pd.DataFrame] = []
        cursor = end
        empty_pages = 0

        while cursor > target_start:
            chunk_start = max(target_start, cursor - timedelta(seconds=chunk_bars * bar_seconds))
            try:
                candles = await client.get_candles(
                    symbol, timeframe, start=chunk_start, end=cursor, limit=chunk_bars
                )
            except Exception as exc:
                logger.warning(
                    "Backfill chunk failed for %s %s (%s..%s): %s",
                    symbol, timeframe, chunk_start, cursor, exc,
                )
                break

            if not candles:
                empty_pages += 1
                # Two consecutive empty pages means we've run past listing date.
                if empty_pages >= 2:
                    break
                cursor = chunk_start
                continue

            empty_pages = 0
            frames.append(self.indicators.candles_to_df(candles))

            oldest = min(c.timestamp for c in candles)
            # Guard against a server that ignores `start` and keeps returning
            # the same window — otherwise this loops forever.
            if oldest >= cursor:
                break
            cursor = oldest - timedelta(seconds=bar_seconds)

            if pause_seconds:
                await asyncio.sleep(pause_seconds)

        if not frames:
            logger.warning("Backfill produced no data for %s %s", symbol, timeframe)
            return pd.DataFrame()

        fetched = self._merge_ohlcv(pd.DataFrame(), pd.concat(frames))
        fetched = fetched[fetched.index >= target_start]

        total = self._write_history(fetched, symbol, timeframe)
        logger.info(
            "Backfilled %s %s: %d bars fetched, %d total stored (%s..%s)",
            symbol, timeframe, len(fetched), total,
            fetched.index.min(), fetched.index.max(),
        )
        return fetched

    @staticmethod
    def describe_history(df: pd.DataFrame) -> dict[str, object]:
        """Summarise a history frame, including flat-bar count.

        Flat bars (open == high == low == close) are the tell for a synthetic
        or dead feed — the testnet caches were full of them.
        """
        if df is None or df.empty:
            return {"bars": 0}

        flat = int(
            (
                (df["open"] == df["high"])
                & (df["high"] == df["low"])
                & (df["low"] == df["close"])
            ).sum()
        )
        span_days = (df.index.max() - df.index.min()).total_seconds() / 86400
        return {
            "bars": len(df),
            "start": df.index.min(),
            "end": df.index.max(),
            "days": round(span_days, 1),
            "flat_bars": flat,
            "flat_pct": round(100.0 * flat / len(df), 2),
            "zero_volume_bars": int((df["volume"] <= 0).sum()),
        }

    def detect_gaps(self, candles: list[Candle], expected_minutes: int) -> list[datetime]:
        gaps = []
        for i in range(1, len(candles)):
            delta = (candles[i].timestamp - candles[i - 1].timestamp).total_seconds() / 60
            if delta > expected_minutes * 1.5:
                gaps.append(candles[i - 1].timestamp)
        return gaps

    @staticmethod
    def _df_to_candles(df: pd.DataFrame, symbol: str, timeframe: str) -> list[Candle]:
        candles = []
        for ts, row in df.iterrows():
            candles.append(
                Candle(
                    timestamp=ts if isinstance(ts, datetime) else datetime.utcfromtimestamp(ts),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0)),
                    symbol=symbol,
                    timeframe=timeframe,
                )
            )
        return candles
