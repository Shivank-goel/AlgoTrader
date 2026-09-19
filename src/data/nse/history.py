"""Historical NSE OHLCV for research.

Deliberately separate from execution. Dhan's historical endpoint needs
credentials; Yahoo needs none, so the whole research pipeline — universe,
backfill, backtest, pass mark — runs before a broker account exists. Live
execution later uses Dhan and never touches this module.

Daily bars are enough for the seed strategy. Ranking on the prior session's
close-to-close return and holding open-to-close needs only daily OHLC, which
also means years of history rather than the 60 days of intraday most free
sources allow.

Yahoo's NSE prices are split- and dividend-adjusted. That matters: an
unadjusted series shows a 1:5 split as an -80% return, which a cross-sectional
reversal strategy would read as the day's biggest loser and buy with both hands.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_HIST_DIR = "data/nse/hist"
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass
class FetchReport:
    requested: int = 0
    fetched: int = 0
    failed: list[str] = None

    def __post_init__(self) -> None:
        if self.failed is None:
            self.failed = []


class NSEHistory:
    """Fetches and caches daily OHLCV per symbol as parquet."""

    def __init__(self, hist_dir: str = DEFAULT_HIST_DIR) -> None:
        self.hist_dir = Path(hist_dir)
        self.hist_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, symbol: str, timeframe: str = "1d") -> Path:
        return self.hist_dir / f"{symbol}_{timeframe}.parquet"

    # -- fetch ----------------------------------------------------------

    @staticmethod
    def _normalise(raw: pd.DataFrame) -> pd.DataFrame:
        """Yahoo's frame -> the repo's lowercase OHLCV convention."""
        if raw is None or raw.empty:
            return pd.DataFrame()

        df = raw.copy()
        # A single-ticker download can still come back column-multi-indexed.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
        keep = [c for c in OHLCV_COLUMNS if c in df.columns]
        if len(keep) < 5:
            return pd.DataFrame()

        df = df[keep].astype(float)
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df.index.name = "timestamp"

        # Zero-volume or zero-price rows are non-trading artefacts.
        df = df[(df["close"] > 0) & (df["volume"] > 0)]
        return df[~df.index.duplicated(keep="last")].sort_index()

    def fetch(
        self,
        symbol: str,
        *,
        period: str = "2y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Download one symbol. Returns an empty frame on any failure."""
        import yfinance as yf

        try:
            raw = yf.download(
                f"{symbol}.NS",
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True,   # split/dividend adjusted — see module docstring
                threads=False,
            )
        except Exception as exc:
            logger.warning("Fetch failed for %s: %s", symbol, exc)
            return pd.DataFrame()

        return self._normalise(raw)

    def backfill(
        self,
        symbols: Iterable[str],
        *,
        period: str = "2y",
        interval: str = "1d",
        pause_seconds: float = 0.0,
        min_bars: int = 250,
    ) -> FetchReport:
        """Fetch and store many symbols. Never raises on an individual failure."""
        symbols = list(symbols)
        report = FetchReport(requested=len(symbols))

        for i, symbol in enumerate(symbols, 1):
            df = self.fetch(symbol, period=period, interval=interval)
            if len(df) < min_bars:
                report.failed.append(symbol)
                logger.debug("%s: only %d bars, skipped", symbol, len(df))
            else:
                df.to_parquet(self.path_for(symbol, interval))
                report.fetched += 1

            if i % 25 == 0:
                logger.info("backfill %d/%d (%d ok)", i, len(symbols), report.fetched)
            if pause_seconds:
                time.sleep(pause_seconds)

        return report

    # -- load -----------------------------------------------------------

    def load(self, symbol: str, timeframe: str = "1d") -> pd.DataFrame:
        path = self.path_for(symbol, timeframe)
        if not path.exists():
            return pd.DataFrame()
        try:
            return pd.read_parquet(path)
        except Exception:
            logger.warning("Unreadable history for %s", symbol)
            return pd.DataFrame()

    def available(self, timeframe: str = "1d") -> list[str]:
        suffix = f"_{timeframe}.parquet"
        return sorted(
            p.name[: -len(suffix)] for p in self.hist_dir.glob(f"*{suffix}")
        )

    def load_panel(
        self,
        symbols: Optional[Iterable[str]] = None,
        *,
        timeframe: str = "1d",
        field: str = "close",
        min_coverage: float = 0.9,
    ) -> pd.DataFrame:
        """Aligned wide frame of one field across symbols.

        Names present for less than `min_coverage` of the union index are
        dropped *before* the join. Without that, one recently-listed symbol
        truncates the whole panel to its own short history — the panel is inner
        joined so every cross-sectional rank is computed on a complete row.
        """
        symbols = list(symbols) if symbols is not None else self.available(timeframe)

        series: dict[str, pd.Series] = {}
        for symbol in symbols:
            df = self.load(symbol, timeframe)
            if not df.empty and field in df.columns:
                series[symbol] = df[field]

        if not series:
            return pd.DataFrame()

        union_len = len(pd.Index(sorted(set().union(*(s.index for s in series.values())))))
        kept = {
            symbol: s for symbol, s in series.items()
            if len(s) >= union_len * min_coverage
        }
        dropped = len(series) - len(kept)
        if dropped:
            logger.info("Dropped %d symbols below %.0f%% coverage", dropped, min_coverage * 100)

        return pd.DataFrame(kept).dropna()

    def describe(self, timeframe: str = "1d") -> dict:
        symbols = self.available(timeframe)
        if not symbols:
            return {"symbols": 0}
        panel = self.load_panel(symbols, timeframe=timeframe)
        return {
            "symbols": len(symbols),
            "panel_symbols": panel.shape[1] if not panel.empty else 0,
            "panel_bars": len(panel),
            "start": panel.index.min() if not panel.empty else None,
            "end": panel.index.max() if not panel.empty else None,
        }
