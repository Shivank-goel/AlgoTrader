"""Survivorship-free NSE universe from daily bhavcopy.

Every long-only equity result so far has been inflated because the universe was
*today's* F&O list backfilled: names promoted after a big run appear with their
whole run-up, names demoted for poor performance are absent entirely. Momentum
buys past winners, so it selects exactly what the bias inserts. Measured cost of
that bias: excess return fell from +17.78pp to +1.70pp when corrected with a
crude proxy (K-73).

Bhavcopy fixes it at the source. NSE publishes, for every trading day, one row
per security that actually traded. Read day by day, that *is* the point-in-time
universe — a delisted name is present until the day it delists and absent after,
with no reconstruction and no judgement.

Two formats, both public and unauthenticated:

  >= 2024-01   UDiFF   nsearchives.nseindia.com/content/cm/BhavCopy_..._F_0000.csv.zip
  <  2024-01   legacy  archives.nseindia.com/content/historical/EQUITIES/YYYY/MON/cmDDMONYYYYbhav.csv.zip

Identity is keyed on **ISIN**, not symbol. Symbols get reused and renamed
(a ticker freed by a delisting can be reassigned); ISIN does not. Both formats
carry it.
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

DEFAULT_CACHE = "data/nse/bhavcopy"
UDIFF_CUTOVER = date(2024, 1, 1)

# NSE only began publishing ISIN in bhavcopy around mid-2011. Earlier files carry
# SYMBOL only, and a symbol freed by a delisting can be reassigned to a different
# company later — so keying the panel on it would silently splice two securities
# into one history. Rather than degrade quietly, days before this are refused.
# 2011-07 still leaves ~15 years, far more than the >=100 monthly observations
# the pass mark needs.
ISIN_AVAILABLE_FROM = date(2011, 7, 1)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

# Normalised schema every day is stored in, regardless of source format.
COLUMNS = [
    "date", "isin", "symbol", "series",
    "open", "high", "low", "close", "prev_close",
    "volume", "turnover",
]

_UDIFF_MAP = {
    "TckrSymb": "symbol", "SctySrs": "series", "ISIN": "isin",
    "OpnPric": "open", "HghPric": "high", "LwPric": "low",
    "ClsPric": "close", "PrvsClsgPric": "prev_close",
    "TtlTradgVol": "volume", "TtlTrfVal": "turnover",
}
_LEGACY_MAP = {
    "SYMBOL": "symbol", "SERIES": "series", "ISIN": "isin",
    "OPEN": "open", "HIGH": "high", "LOW": "low",
    "CLOSE": "close", "PREVCLOSE": "prev_close",
    "TOTTRDQTY": "volume", "TOTTRDVAL": "turnover",
}


class BhavcopyClient:
    """Downloads, caches and normalises NSE daily bhavcopy."""

    def __init__(self, cache_dir: str = DEFAULT_CACHE, *, timeout: int = 30) -> None:
        self.cache_dir = Path(cache_dir)
        self.day_dir = self.cache_dir / "days"
        self.day_dir.mkdir(parents=True, exist_ok=True)
        # Non-trading days are cached as markers so a re-run does not re-request
        # ~100 holidays every time.
        self.holiday_file = self.cache_dir / "non_trading_days.txt"
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._holidays: set[str] = set()
        if self.holiday_file.exists():
            self._holidays = set(self.holiday_file.read_text().split())

    # -- urls -----------------------------------------------------------

    @staticmethod
    def _url_udiff(d: date) -> str:
        return (
            "https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
        )

    @staticmethod
    def _url_legacy(d: date) -> str:
        return (
            "https://archives.nseindia.com/content/historical/EQUITIES/"
            f"{d:%Y}/{d:%b}/cm{d:%d%b%Y}bhav.csv.zip".replace(
                f"{d:%b}", f"{d:%b}".upper()
            )
        )

    def _path(self, d: date) -> Path:
        return self.day_dir / f"{d:%Y-%m-%d}.parquet"

    # -- fetch ----------------------------------------------------------

    def _download(self, url: str) -> Optional[pd.DataFrame]:
        try:
            r = self._session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("request failed %s: %s", url, exc)
            return None
        if r.status_code != 200 or not r.content[:2] == b"PK":
            return None
        try:
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
                return pd.read_csv(z.open(name), low_memory=False)
        except (zipfile.BadZipFile, StopIteration, ValueError) as exc:
            logger.warning("unreadable archive %s: %s", url, exc)
            return None

    @staticmethod
    def _normalise(raw: pd.DataFrame, d: date) -> pd.DataFrame:
        raw = raw.rename(columns=lambda c: str(c).strip())
        mapping = _UDIFF_MAP if "TckrSymb" in raw.columns else _LEGACY_MAP
        present = {k: v for k, v in mapping.items() if k in raw.columns}
        df = raw[list(present)].rename(columns=present)

        df["date"] = pd.Timestamp(d)
        for col in ("open", "high", "low", "close", "prev_close", "volume", "turnover"):
            df[col] = pd.to_numeric(df.get(col), errors="coerce")
        for col in ("symbol", "series", "isin"):
            df[col] = df.get(col, pd.Series(dtype=object)).astype(str).str.strip()

        # A row with no ISIN cannot be tracked across a rename; drop it rather
        # than key on a symbol that may be reassigned later.
        df = df[(df["isin"].str.len() > 0) & (df["isin"].str.upper() != "NAN")]
        df = df[df["close"] > 0]
        return df.reindex(columns=COLUMNS)

    def fetch_day(self, d: date, *, force: bool = False) -> pd.DataFrame:
        """One trading day, normalised. Empty frame for holidays.

        Raises for dates before ISIN was published — see ISIN_AVAILABLE_FROM.
        """
        if d < ISIN_AVAILABLE_FROM:
            raise ValueError(
                f"{d} predates ISIN in bhavcopy (from {ISIN_AVAILABLE_FROM}). "
                "Keying on SYMBOL alone risks splicing two companies that shared "
                "a ticker; refuse rather than corrupt the panel."
            )
        path = self._path(d)
        if path.exists() and not force:
            return pd.read_parquet(path)
        if f"{d:%Y-%m-%d}" in self._holidays and not force:
            return pd.DataFrame(columns=COLUMNS)

        urls = (
            [self._url_udiff(d), self._url_legacy(d)]
            if d >= UDIFF_CUTOVER
            else [self._url_legacy(d)]
        )
        raw = None
        for url in urls:
            raw = self._download(url)
            if raw is not None:
                break

        if raw is None:
            self._holidays.add(f"{d:%Y-%m-%d}")
            self.holiday_file.write_text("\n".join(sorted(self._holidays)))
            return pd.DataFrame(columns=COLUMNS)

        df = self._normalise(raw, d)
        df.to_parquet(path, index=False)
        return df

    def download_range(
        self,
        start: date,
        end: date,
        *,
        pause: float = 0.15,
        log_every: int = 100,
    ) -> dict:
        """Fetch every weekday in [start, end]. Returns a summary."""
        if start < ISIN_AVAILABLE_FROM:
            logger.warning(
                "start %s predates ISIN in bhavcopy; clamping to %s",
                start, ISIN_AVAILABLE_FROM,
            )
            start = ISIN_AVAILABLE_FROM
        days = [
            start + timedelta(days=i)
            for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5
        ]
        traded = holidays = cached = 0
        for i, d in enumerate(days, 1):
            if self._path(d).exists():
                cached += 1
            else:
                df = self.fetch_day(d)
                if df.empty:
                    holidays += 1
                else:
                    traded += 1
                if pause:
                    time.sleep(pause)
            if i % log_every == 0:
                logger.info("bhavcopy %d/%d (%d new, %d cached)", i, len(days), traded, cached)
        return {
            "weekdays": len(days), "downloaded": traded,
            "non_trading": holidays, "already_cached": cached,
        }

    # -- load -----------------------------------------------------------

    def load_all(self, series: Iterable[str] = ("EQ",)) -> pd.DataFrame:
        """Every cached day, concatenated. The survivorship-free long frame."""
        wanted = set(series)
        frames = []
        for path in sorted(self.day_dir.glob("*.parquet")):
            df = pd.read_parquet(path)
            if not df.empty:
                frames.append(df[df["series"].isin(wanted)] if wanted else df)
        if not frames:
            return pd.DataFrame(columns=COLUMNS)
        return pd.concat(frames, ignore_index=True).sort_values(["date", "isin"])

    def cached_days(self) -> int:
        return len(list(self.day_dir.glob("*.parquet")))


def build_panel(
    long_df: pd.DataFrame,
    field: str = "close",
    *,
    key: str = "isin",
) -> pd.DataFrame:
    """Wide date x security frame. **NaN means 'not listed or not traded'.**

    Do not forward-fill or dropna across the whole frame: a NaN carries the
    survivorship information this entire module exists to preserve. Callers must
    handle presence explicitly, per date.
    """
    return long_df.pivot_table(index="date", columns=key, values=field, aggfunc="last")


def point_in_time_universe(
    long_df: pd.DataFrame,
    as_of: pd.Timestamp,
    *,
    lookback_days: int = 60,
    top_n: int = 200,
    min_price: float = 5.0,
) -> list[str]:
    """The `top_n` most liquid securities as known **on** `as_of`.

    Ranked on median rupee turnover over the trailing window, using only rows
    dated at or before `as_of`. This is the replacement for "today's F&O list":
    it can only ever see what had already traded.
    """
    window = long_df[
        (long_df["date"] <= as_of)
        & (long_df["date"] > as_of - pd.Timedelta(days=lookback_days))
    ]
    if window.empty:
        return []

    recent_price = window.groupby("isin")["close"].last()
    liquidity = window.groupby("isin")["turnover"].median()
    eligible = liquidity[recent_price.reindex(liquidity.index) >= min_price]
    return list(eligible.sort_values(ascending=False).head(top_n).index)
