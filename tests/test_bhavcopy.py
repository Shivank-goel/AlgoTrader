"""Tests for the survivorship-free NSE universe.

The whole point of bhavcopy is that a NaN means something: "this security did
not trade that day", which for a delisted name means "it no longer exists".
Every test here defends that meaning. The failure mode we are guarding against
is a well-intentioned `dropna()` or `ffill()` that quietly reintroduces exactly
the bias this module exists to remove (K-73, K-80).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.data.nse.bhavcopy import (
    COLUMNS,
    UDIFF_CUTOVER,
    BhavcopyClient,
    build_panel,
    point_in_time_universe,
)


@pytest.fixture
def client(tmp_path) -> BhavcopyClient:
    return BhavcopyClient(str(tmp_path / "bhav"))


def _long_frame() -> pd.DataFrame:
    """Three securities; DEAD delists after day 3, NEW lists on day 3."""
    rows = []
    days = pd.date_range("2024-01-01", periods=5, freq="D")
    for i, d in enumerate(days):
        rows.append((d, "INE_ALIVE", "ALIVE", "EQ", 100 + i, 1_000_000))
        if i < 3:
            rows.append((d, "INE_DEAD", "DEAD", "EQ", 50 - i, 500_000))
        if i >= 3:
            rows.append((d, "INE_NEW", "NEW", "EQ", 200 + i, 2_000_000))
    df = pd.DataFrame(rows, columns=["date", "isin", "symbol", "series", "close", "turnover"])
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = df["close"] if c in ("open", "high", "low", "prev_close") else 1000.0
    return df[COLUMNS]


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def test_udiff_url_shape(client):
    url = client._url_udiff(date(2026, 8, 14))
    assert "BhavCopy_NSE_CM_0_0_0_20260814_F_0000.csv.zip" in url


def test_legacy_url_uses_uppercase_month(client):
    url = client._url_legacy(date(2018, 7, 2))
    assert "/2018/JUL/cm02JUL2018bhav.csv.zip" in url


def test_format_choice_follows_the_cutover(client):
    """UDiFF only exists from 2024-01; before that only legacy is published."""
    assert UDIFF_CUTOVER == date(2024, 1, 1)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_normalises_udiff_columns(client):
    raw = pd.DataFrame({
        "TckrSymb": ["RELIANCE"], "SctySrs": ["EQ"], "ISIN": ["INE002A01018"],
        "OpnPric": [100.0], "HghPric": [110.0], "LwPric": [99.0],
        "ClsPric": [105.0], "PrvsClsgPric": [98.0],
        "TtlTradgVol": [1000.0], "TtlTrfVal": [105000.0],
    })
    out = client._normalise(raw, date(2026, 8, 14))
    assert list(out.columns) == COLUMNS
    assert out.iloc[0]["symbol"] == "RELIANCE"
    assert out.iloc[0]["isin"] == "INE002A01018"
    assert out.iloc[0]["close"] == 105.0


def test_normalises_legacy_columns(client):
    raw = pd.DataFrame({
        "SYMBOL": ["RELIANCE"], "SERIES": ["EQ"], "ISIN": ["INE002A01018"],
        "OPEN": [100.0], "HIGH": [110.0], "LOW": [99.0],
        "CLOSE": [105.0], "PREVCLOSE": [98.0],
        "TOTTRDQTY": [1000.0], "TOTTRDVAL": [105000.0],
    })
    out = client._normalise(raw, date(2018, 7, 2))
    assert list(out.columns) == COLUMNS
    assert out.iloc[0]["close"] == 105.0


def test_both_formats_produce_the_same_schema(client):
    udiff = client._normalise(
        pd.DataFrame({"TckrSymb": ["X"], "SctySrs": ["EQ"], "ISIN": ["I1"], "ClsPric": [1.0]}),
        date(2026, 1, 1),
    )
    legacy = client._normalise(
        pd.DataFrame({"SYMBOL": ["X"], "SERIES": ["EQ"], "ISIN": ["I1"], "CLOSE": [1.0]}),
        date(2018, 1, 1),
    )
    assert list(udiff.columns) == list(legacy.columns)


def test_rows_without_isin_are_dropped(client):
    """Identity must be ISIN — a symbol freed by a delisting can be reassigned."""
    raw = pd.DataFrame({
        "SYMBOL": ["GOOD", "NOISIN"], "SERIES": ["EQ", "EQ"],
        "ISIN": ["INE001A01001", ""], "CLOSE": [10.0, 20.0],
    })
    out = client._normalise(raw, date(2020, 1, 1))
    assert list(out["symbol"]) == ["GOOD"]


def test_zero_and_negative_closes_are_dropped(client):
    raw = pd.DataFrame({
        "SYMBOL": ["A", "B"], "SERIES": ["EQ", "EQ"],
        "ISIN": ["I1", "I2"], "CLOSE": [10.0, 0.0],
    })
    assert list(client._normalise(raw, date(2020, 1, 1))["symbol"]) == ["A"]


# ---------------------------------------------------------------------------
# Holiday handling
# ---------------------------------------------------------------------------


def test_non_trading_days_are_cached_and_not_rerequested(client, monkeypatch):
    calls = []

    def fake_download(url):
        calls.append(url)
        return None

    monkeypatch.setattr(client, "_download", fake_download)

    assert client.fetch_day(date(2026, 8, 15)).empty
    first = len(calls)
    assert client.fetch_day(date(2026, 8, 15)).empty
    assert len(calls) == first, "a known holiday was requested twice"


def test_holiday_marker_survives_a_new_client(tmp_path, monkeypatch):
    c1 = BhavcopyClient(str(tmp_path / "b"))
    monkeypatch.setattr(c1, "_download", lambda url: None)
    c1.fetch_day(date(2026, 8, 15))

    c2 = BhavcopyClient(str(tmp_path / "b"))
    assert "2026-08-15" in c2._holidays


# ---------------------------------------------------------------------------
# The survivorship property — what this module exists for
# ---------------------------------------------------------------------------


def test_panel_keeps_nan_for_securities_that_did_not_trade():
    """NaN is information: 'not listed'. It must not be filled away."""
    panel = build_panel(_long_frame(), "close")

    assert pd.isna(panel.loc[panel.index[4], "INE_DEAD"]), "delisted name should be NaN after"
    assert pd.notna(panel.loc[panel.index[0], "INE_DEAD"]), "and present before"
    assert pd.isna(panel.loc[panel.index[0], "INE_NEW"]), "not-yet-listed name should be NaN"


def test_panel_is_keyed_on_isin_not_symbol():
    panel = build_panel(_long_frame(), "close")
    assert set(panel.columns) == {"INE_ALIVE", "INE_DEAD", "INE_NEW"}


def test_dropna_would_destroy_the_universe():
    """Documents the trap. A whole-frame dropna leaves only always-present names."""
    panel = build_panel(_long_frame(), "close")
    assert panel.dropna(axis=1).columns.tolist() == ["INE_ALIVE"], (
        "if this changes, the fixture no longer demonstrates the bias"
    )


# ---------------------------------------------------------------------------
# Point-in-time universe selection
# ---------------------------------------------------------------------------


def test_point_in_time_universe_cannot_see_the_future():
    long_df = _long_frame()
    early = point_in_time_universe(long_df, long_df["date"].iloc[0], top_n=10)
    assert "INE_NEW" not in early, "selected a security that had not listed yet"


def test_point_in_time_universe_includes_a_name_that_later_delists():
    """The correction that matters: DEAD must be selectable while it lived."""
    long_df = _long_frame()
    day2 = sorted(long_df["date"].unique())[1]
    assert "INE_DEAD" in point_in_time_universe(long_df, pd.Timestamp(day2), top_n=10)


def test_point_in_time_universe_ranks_by_turnover():
    long_df = _long_frame()
    last = pd.Timestamp(sorted(long_df["date"].unique())[-1])
    ranked = point_in_time_universe(long_df, last, top_n=1)
    assert ranked == ["INE_NEW"], "highest-turnover name should rank first"


def test_penny_stocks_are_excluded_by_min_price():
    long_df = _long_frame()
    last = pd.Timestamp(sorted(long_df["date"].unique())[-1])
    assert point_in_time_universe(long_df, last, top_n=10, min_price=1e9) == []


def test_empty_window_returns_empty():
    assert point_in_time_universe(_long_frame(), pd.Timestamp("2000-01-01")) == []


# ---------------------------------------------------------------------------
# The ISIN boundary
# ---------------------------------------------------------------------------


def test_dates_before_isin_are_refused_not_silently_empty(client):
    """Pre-2011-07 bhavcopy has no ISIN, so the panel cannot be keyed safely."""
    from src.data.nse.bhavcopy import ISIN_AVAILABLE_FROM

    with pytest.raises(ValueError, match="predates ISIN"):
        client.fetch_day(date(2010, 1, 4))
    assert ISIN_AVAILABLE_FROM == date(2011, 7, 1)


def test_download_range_clamps_to_the_isin_boundary(client, monkeypatch):
    from src.data.nse.bhavcopy import ISIN_AVAILABLE_FROM

    seen = []
    monkeypatch.setattr(client, "fetch_day", lambda d, **kw: seen.append(d) or pd.DataFrame(columns=COLUMNS))
    client.download_range(date(2009, 1, 1), date(2011, 7, 8), pause=0)
    assert seen and min(seen) >= ISIN_AVAILABLE_FROM
