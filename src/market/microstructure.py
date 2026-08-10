"""Append-only recorder for exchange microstructure.

The Delta ticker publishes `funding_rate`, `spot_price`, `mark_basis`, `oi`,
`oi_value_usd`, `oi_change_usd_6h` and `turnover_usd` on every call. Nothing in
the system reads any of them; only `_extract_ticker_price` is used.

These are not backtestable — no historical series exists and Delta does not
serve one. So this deliberately builds **no features and no signals**. It only
records, because the one thing that cannot be compressed later is elapsed time:
a strategy that needs six months of funding history can only start collecting
today.

Live funding on this venue, sampled 2026-08-10 across all liquid perps: the
median is exactly 0.0100% per 8h — pinned to the baseline floor rather than
market-driven — and only three symbols exceed 0.05%/8h, all with under $500k of
open interest. Any future funding strategy has to reckon with that.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_PATH = "data/microstructure.parquet"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class MicrostructureSnapshot:
    """One symbol at one instant."""

    timestamp: datetime
    symbol: str
    mark_price: float = 0.0
    spot_price: float = 0.0
    mark_basis: float = 0.0
    funding_rate: float = 0.0
    open_interest: float = 0.0
    oi_value_usd: float = 0.0
    oi_change_usd_6h: float = 0.0
    turnover_usd: float = 0.0

    @classmethod
    def from_ticker(cls, symbol: str, ticker: dict[str, Any]) -> MicrostructureSnapshot:
        return cls(
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            symbol=symbol,
            mark_price=_f(ticker.get("mark_price")),
            spot_price=_f(ticker.get("spot_price")),
            mark_basis=_f(ticker.get("mark_basis")),
            funding_rate=_f(ticker.get("funding_rate")),
            open_interest=_f(ticker.get("oi")),
            oi_value_usd=_f(ticker.get("oi_value_usd")),
            oi_change_usd_6h=_f(ticker.get("oi_change_usd_6h")),
            turnover_usd=_f(ticker.get("turnover_usd")),
        )

    @property
    def annualised_funding_pct(self) -> float:
        """Funding rate expressed as an annual percentage (three 8h periods/day)."""
        return self.funding_rate * 3 * 365


class MicrostructureRecorder:
    """Appends snapshots to a parquet file. Never raises into the caller."""

    def __init__(self, path: str = DEFAULT_PATH, *, flush_every: int = 50) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.flush_every = flush_every
        self._buffer: list[MicrostructureSnapshot] = []

    def record(self, snapshots: Iterable[MicrostructureSnapshot]) -> int:
        """Buffer snapshots, flushing once the buffer is full."""
        added = [s for s in snapshots if s.symbol]
        self._buffer.extend(added)
        if len(self._buffer) >= self.flush_every:
            self.flush()
        return len(added)

    def record_tickers(self, tickers: dict[str, dict[str, Any]]) -> int:
        return self.record(
            MicrostructureSnapshot.from_ticker(symbol, ticker)
            for symbol, ticker in tickers.items()
            if ticker
        )

    def flush(self) -> int:
        """Merge the buffer into the parquet file. Returns rows written."""
        if not self._buffer:
            return 0

        new = pd.DataFrame([asdict(s) for s in self._buffer])
        self._buffer.clear()

        try:
            if self.path.exists():
                existing = pd.read_parquet(self.path)
                combined = pd.concat([existing, new], ignore_index=True)
            else:
                combined = new

            # One row per (symbol, timestamp); a restart mid-scan must not
            # duplicate a sample.
            combined = combined.drop_duplicates(
                subset=["symbol", "timestamp"], keep="last"
            ).sort_values(["timestamp", "symbol"], ignore_index=True)

            combined.to_parquet(self.path, index=False)
            return len(new)
        except Exception:
            logger.exception("Microstructure flush failed; %d rows dropped", len(new))
            return 0

    def load(self, symbol: Optional[str] = None) -> pd.DataFrame:
        if not self.path.exists():
            return pd.DataFrame()
        try:
            df = pd.read_parquet(self.path)
        except Exception:
            logger.warning("Microstructure file unreadable at %s", self.path)
            return pd.DataFrame()
        return df[df["symbol"] == symbol] if symbol else df

    def stats(self) -> dict[str, Any]:
        df = self.load()
        if df.empty:
            return {"rows": 0, "symbols": 0, "span_hours": 0.0}
        span = (df["timestamp"].max() - df["timestamp"].min()).total_seconds() / 3600
        return {
            "rows": len(df),
            "symbols": int(df["symbol"].nunique()),
            "span_hours": round(span, 2),
            "first": df["timestamp"].min(),
            "last": df["timestamp"].max(),
        }
