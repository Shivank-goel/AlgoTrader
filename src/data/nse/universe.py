"""NSE tradeable universe, sourced from Dhan's public instrument master.

The F&O-eligible stock list is used as the liquidity filter. It is not a
liquidity measure in itself, but NSE only admits names to F&O that clear
turnover and market-width tests, and it reviews eligibility periodically — so it
is a maintained, survivorship-aware proxy that costs nothing to obtain.

208 names as of 2026-08, every one of which also trades in the cash segment.
That is the whole reason for moving here: Delta India offered a 4-name
cross-section, which is why every cross-sectional result in Rounds 1-2 was
underpowered.

The instrument master is a public 26 MB CSV requiring no credentials, so the
universe can be built before any broker account exists.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
DEFAULT_CACHE = "data/nse/universe.json"
DEFAULT_SCRIP_CACHE = "data/nse/scrip_master.csv"
CACHE_MAX_AGE_DAYS = 7

# Dummy instruments NSE keeps in the master for exchange testing.
_TEST_MARKER = "NSETEST"


@dataclass(frozen=True)
class NSEInstrument:
    symbol: str
    security_id: int
    lot_size: int = 0

    @property
    def yahoo_symbol(self) -> str:
        """Ticker for free historical data. NSE cash uses a .NS suffix."""
        return f"{self.symbol}.NS"


class NSEUniverse:
    """The F&O-eligible cash-segment universe."""

    def __init__(
        self,
        cache_path: str = DEFAULT_CACHE,
        scrip_cache_path: str = DEFAULT_SCRIP_CACHE,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.scrip_cache_path = Path(scrip_cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    # -- construction ---------------------------------------------------

    def _cache_is_fresh(self, path: Path, max_age_days: int = CACHE_MAX_AGE_DAYS) -> bool:
        if not path.exists():
            return False
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        )
        return age < timedelta(days=max_age_days)

    def _download_scrip_master(self) -> pd.DataFrame:
        """Fetch (or reuse) Dhan's public instrument master."""
        if self._cache_is_fresh(self.scrip_cache_path):
            logger.info("Using cached scrip master at %s", self.scrip_cache_path)
            return pd.read_csv(self.scrip_cache_path, low_memory=False)

        logger.info("Downloading Dhan scrip master (~26 MB)")
        df = pd.read_csv(SCRIP_MASTER_URL, low_memory=False)
        self.scrip_cache_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(self.scrip_cache_path, index=False)
        return df

    @staticmethod
    def _fno_underlyings(scrip: pd.DataFrame) -> set[str]:
        """Underlying symbols that have stock futures.

        Trading symbols look like `RELIANCE-Aug2026-FUT`, so the underlying is
        everything before the last two hyphen-separated parts. Splitting on the
        first hyphen would truncate names like `M&M-...` incorrectly if they
        ever contained one.
        """
        futures = scrip[
            (scrip["SEM_EXM_EXCH_ID"] == "NSE")
            & (scrip["SEM_INSTRUMENT_NAME"] == "FUTSTK")
        ]
        return {
            symbol.rsplit("-", 2)[0]
            for symbol in futures["SEM_TRADING_SYMBOL"].dropna()
            if _TEST_MARKER not in symbol
        }

    @staticmethod
    def _cash_instruments(scrip: pd.DataFrame) -> dict[str, tuple[int, int]]:
        """symbol -> (security_id, lot_size) for NSE cash-segment EQ series."""
        equity = scrip[
            (scrip["SEM_EXM_EXCH_ID"] == "NSE")
            & (scrip["SEM_INSTRUMENT_NAME"] == "EQUITY")
            & (scrip["SEM_SERIES"] == "EQ")
        ]
        out: dict[str, tuple[int, int]] = {}
        for row in equity.itertuples():
            symbol = getattr(row, "SEM_TRADING_SYMBOL", None)
            sec_id = getattr(row, "SEM_SMST_SECURITY_ID", None)
            if not symbol or pd.isna(sec_id):
                continue
            lot = getattr(row, "SEM_LOT_UNITS", 0)
            out[str(symbol)] = (int(sec_id), int(lot) if pd.notna(lot) else 0)
        return out

    def build(self, *, force: bool = False) -> list[NSEInstrument]:
        """Build the universe, using the cache unless `force`."""
        if not force and self._cache_is_fresh(self.cache_path):
            return self.load()

        scrip = self._download_scrip_master()
        underlyings = self._fno_underlyings(scrip)
        cash = self._cash_instruments(scrip)

        instruments = [
            NSEInstrument(symbol=sym, security_id=cash[sym][0], lot_size=cash[sym][1])
            for sym in sorted(underlyings)
            if sym in cash
        ]

        missing = len(underlyings) - len(instruments)
        if missing:
            logger.warning(
                "%d F&O underlyings had no NSE cash listing and were dropped", missing
            )

        self.save(instruments)
        logger.info("NSE universe built: %d instruments", len(instruments))
        return instruments

    # -- persistence ----------------------------------------------------

    def save(self, instruments: list[NSEInstrument]) -> None:
        payload = {
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(instruments),
            "instruments": [
                {"symbol": i.symbol, "security_id": i.security_id, "lot_size": i.lot_size}
                for i in instruments
            ],
        }
        self.cache_path.write_text(json.dumps(payload, indent=2))

    def load(self) -> list[NSEInstrument]:
        if not self.cache_path.exists():
            return []
        try:
            payload = json.loads(self.cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            logger.warning("Universe cache unreadable at %s", self.cache_path)
            return []
        return [
            NSEInstrument(
                symbol=row["symbol"],
                security_id=int(row["security_id"]),
                lot_size=int(row.get("lot_size", 0)),
            )
            for row in payload.get("instruments", [])
        ]

    def symbols(self) -> list[str]:
        return [i.symbol for i in self.load()]

    def security_id(self, symbol: str) -> Optional[int]:
        for instrument in self.load():
            if instrument.symbol == symbol.upper():
                return instrument.security_id
        return None


def scaled_turnover(
    close: pd.Series, volume: pd.Series, *, window: int = 20
) -> pd.Series:
    """Traded value relative to price level — a liquidity proxy without market cap.

    The 19-year NSE study that motivates this project defines scaled turnover as
    traded value / market cap, and finds momentum alpha concentrated in the
    *low* bucket: 19.43% CAGR against 8.51% for the liquid half. Market cap needs
    a shares-outstanding feed; rupee turnover alone is a usable stand-in for
    ranking within a universe that is already liquidity-filtered.

    Returned as a rolling mean so a single unusual session cannot reclassify a
    name.
    """
    return (close * volume).rolling(window).mean()
