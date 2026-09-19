"""Technical indicator engine using the 'ta' library (compatible with Python 3.9+)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import ta
from ta.momentum import RSIIndicator, ROCIndicator
from ta.trend import (
    ADXIndicator,
    EMAIndicator,
    SMAIndicator,
    MACD,
)
from ta.volatility import AverageTrueRange, BollingerBands
from ta.volume import OnBalanceVolumeIndicator

# Bars required on each side of a fractal pivot before it counts as a swing.
# Also the lag by which pivots are shifted, since a swing high is not knowable
# until this many bars have failed to exceed it.
SWING_CONFIRMATION_BARS = 3

# Every price-action column compute_all emits. Strategies assert against this
# rather than probing for keys, because `_get_indicator` returns 0.0 for a
# missing column and callers test `if not x:` — so "column absent" and "feature
# is zero" are otherwise indistinguishable. That ambiguity is why
# volume_breakout fired zero trades for a year.
PRICE_ACTION_COLUMNS: tuple[str, ...] = (
    "pa_body_frac",
    "pa_upper_wick_frac",
    "pa_lower_wick_frac",
    "pa_range_pct",
    "pa_is_bull",
    "pa_swing_high",
    "pa_swing_low",
    "pa_dist_to_swing_high_pct",
    "pa_dist_to_swing_low_pct",
    "pa_bos_up",
    "pa_bos_down",
    "pa_bull_engulfing",
    "pa_bear_engulfing",
    "pa_bull_pin",
    "pa_bear_pin",
    "pa_inside_bar",
    "pa_outside_bar",
)


class IndicatorEngine:
    """Computes technical indicators on OHLCV DataFrames."""

    @staticmethod
    def candles_to_df(candles: list) -> pd.DataFrame:
        rows = [
            {
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ]
        df = pd.DataFrame(rows)
        if not df.empty:
            df.set_index("timestamp", inplace=True)
        return df

    def compute_all(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 20:
            return df

        result = df.copy()
        high = result["high"]
        low = result["low"]
        close = result["close"]
        volume = result["volume"]
        n = len(df)

        # EMAs
        for period in [9, 12, 21, 26, 50, 200]:
            if len(df) >= period:
                result[f"ema_{period}"] = EMAIndicator(close, window=period).ema_indicator()

        # SMA
        result["sma_20"] = SMAIndicator(close, window=20).sma_indicator()

        # RSI
        result["rsi_14"] = RSIIndicator(close, window=14).rsi()
        if len(df) >= 5:
            result["rsi_2"] = RSIIndicator(close, window=2).rsi()

        # MACD
        macd = MACD(close, window_slow=26, window_fast=12, window_sign=9)
        result["MACD_12_26_9"] = macd.macd()
        result["MACDs_12_26_9"] = macd.macd_signal()
        result["MACDh_12_26_9"] = macd.macd_diff()

        # ADX (requires at least 2*window rows)
        if n >= 28:
            adx_ind = ADXIndicator(high, low, close, window=14)
            result["ADX_14"] = adx_ind.adx()
            result["DI+_14"] = adx_ind.adx_pos()
            result["DI-_14"] = adx_ind.adx_neg()

        # ATR
        if n >= 14:
            atr = AverageTrueRange(high, low, close, window=14)
            result["atr_14"] = atr.average_true_range()

        # Bollinger Bands
        bb = BollingerBands(close, window=20, window_dev=2)
        result["BBU_20_2.0"] = bb.bollinger_hband()
        result["BBM_20_2.0"] = bb.bollinger_mavg()
        result["BBL_20_2.0"] = bb.bollinger_lband()
        bbm = result["BBM_20_2.0"].replace(0, np.nan)
        result["bbw"] = (result["BBU_20_2.0"] - result["BBL_20_2.0"]) / bbm

        # Donchian Channel (manual)
        if len(df) >= 20:
            result["DCHU_20"] = high.rolling(window=20).max()
            result["DCHL_20"] = low.rolling(window=20).min()
            result["DCHM_20"] = (result["DCHU_20"] + result["DCHL_20"]) / 2

        # ROC
        result["roc_10"] = ROCIndicator(close, window=10).roc()

        # OBV
        result["obv"] = OnBalanceVolumeIndicator(close, volume).on_balance_volume()

        # Volume metrics
        result["volume_sma_20"] = SMAIndicator(volume, window=20).sma_indicator()
        result["volume_ratio"] = volume / result["volume_sma_20"].replace(0, np.nan)

        # Supertrend (manual calculation)
        result = self._compute_supertrend(result, period=10, multiplier=3.0)

        # Price-action structure and candle anatomy
        result = self._compute_price_action(result, swing_k=SWING_CONFIRMATION_BARS)

        return result

    @staticmethod
    def _compute_price_action(df: pd.DataFrame, swing_k: int = 3) -> pd.DataFrame:
        """Swing structure, bar anatomy and candle patterns.

        Every column is float, never bool: `prepare_arrays` drops non-numeric
        dtypes, so a boolean flag would work live and silently vanish in
        backtests. See tests/test_train_serve_parity.py.

        Lookahead control is the delicate part. A fractal swing high at bar `i`
        is only *knowable* at `i + swing_k`, once k bars have failed to exceed
        it. Writing the pivot at bar `i` would let a backtest trade a level the
        live engine could not yet have seen — the classic way to manufacture an
        edge that evaporates in production. Pivots are therefore shifted by
        `swing_k` before being carried forward.
        """
        high, low, close, open_ = df["high"], df["low"], df["close"], df["open"]

        rng = (high - low).replace(0, np.nan)
        body = (close - open_).abs()

        df["pa_body_frac"] = (body / rng).fillna(0.0)
        df["pa_upper_wick_frac"] = ((high - close.combine(open_, max)) / rng).fillna(0.0)
        df["pa_lower_wick_frac"] = ((close.combine(open_, min) - low) / rng).fillna(0.0)
        df["pa_range_pct"] = (rng / close.replace(0, np.nan) * 100).fillna(0.0)
        df["pa_is_bull"] = (close > open_).astype(float)

        # --- fractal pivots, confirmed only ---
        window = 2 * swing_k + 1
        is_pivot_high = (
            high.rolling(window, center=True).max().eq(high)
            & high.notna()
        )
        is_pivot_low = (
            low.rolling(window, center=True).min().eq(low)
            & low.notna()
        )

        # Shift forward by k: the pivot becomes usable only after confirmation.
        confirmed_high = high.where(is_pivot_high).shift(swing_k).ffill()
        confirmed_low = low.where(is_pivot_low).shift(swing_k).ffill()

        df["pa_swing_high"] = confirmed_high.fillna(high)
        df["pa_swing_low"] = confirmed_low.fillna(low)
        df["pa_dist_to_swing_high_pct"] = (
            (df["pa_swing_high"] - close) / close.replace(0, np.nan) * 100
        ).fillna(0.0)
        df["pa_dist_to_swing_low_pct"] = (
            (close - df["pa_swing_low"]) / close.replace(0, np.nan) * 100
        ).fillna(0.0)

        # Break of structure: close beyond the last confirmed swing.
        df["pa_bos_up"] = (close > df["pa_swing_high"]).astype(float)
        df["pa_bos_down"] = (close < df["pa_swing_low"]).astype(float)

        # --- candle patterns ---
        prev_open, prev_close = open_.shift(1), close.shift(1)
        prev_high, prev_low = high.shift(1), low.shift(1)

        df["pa_bull_engulfing"] = (
            (close > open_)
            & (prev_close < prev_open)
            & (close >= prev_open)
            & (open_ <= prev_close)
        ).astype(float)
        df["pa_bear_engulfing"] = (
            (close < open_)
            & (prev_close > prev_open)
            & (close <= prev_open)
            & (open_ >= prev_close)
        ).astype(float)

        # Pin bar: a long rejection wick with a small body.
        df["pa_bull_pin"] = (
            (df["pa_lower_wick_frac"] > 0.55) & (df["pa_body_frac"] < 0.35)
        ).astype(float)
        df["pa_bear_pin"] = (
            (df["pa_upper_wick_frac"] > 0.55) & (df["pa_body_frac"] < 0.35)
        ).astype(float)

        df["pa_inside_bar"] = ((high <= prev_high) & (low >= prev_low)).astype(float)
        df["pa_outside_bar"] = ((high > prev_high) & (low < prev_low)).astype(float)

        return df

    def _compute_supertrend(
        self, df: pd.DataFrame, period: int = 10, multiplier: float = 3.0
    ) -> pd.DataFrame:
        """Compute Supertrend indicator manually."""
        atr_col = f"atr_{period}"
        if atr_col not in df.columns:
            atr = AverageTrueRange(df["high"], df["low"], df["close"], window=period)
            df[atr_col] = atr.average_true_range()

        atr_vals = df[atr_col]
        hl2 = (df["high"] + df["low"]) / 2
        upper = hl2 + multiplier * atr_vals
        lower = hl2 - multiplier * atr_vals

        supertrend = pd.Series(index=df.index, dtype=float)
        direction = pd.Series(index=df.index, dtype=float)

        supertrend.iloc[0] = upper.iloc[0]
        direction.iloc[0] = -1

        for i in range(1, len(df)):
            if df["close"].iloc[i] > upper.iloc[i - 1]:
                direction.iloc[i] = 1
            elif df["close"].iloc[i] < lower.iloc[i - 1]:
                direction.iloc[i] = -1
            else:
                direction.iloc[i] = direction.iloc[i - 1]

            if direction.iloc[i] == 1:
                supertrend.iloc[i] = max(lower.iloc[i], supertrend.iloc[i - 1]) if direction.iloc[i - 1] == 1 else lower.iloc[i]
            else:
                supertrend.iloc[i] = min(upper.iloc[i], supertrend.iloc[i - 1]) if direction.iloc[i - 1] == -1 else upper.iloc[i]

        df[f"SUPERT_10_3.0"] = supertrend
        df[f"SUPERTd_10_3.0"] = direction
        return df

    def get_latest_values(self, df: pd.DataFrame) -> dict[str, Any]:
        if df.empty:
            return {}
        latest = df.iloc[-1]
        values = {}
        for col in df.columns:
            val = latest[col]
            if pd.notna(val):
                values[col] = float(val) if isinstance(val, (np.floating, float, int, np.integer)) else val
        return values

    @staticmethod
    def prepare_arrays(df: pd.DataFrame) -> dict[str, np.ndarray]:
        """Convert a computed frame to numpy columns once, for row-wise reads.

        Backtests need per-bar indicator dicts. Doing that through
        `df.iloc[i]` pays pandas indexing overhead on every bar of every
        strategy of every symbol; this pays it once per frame.
        """
        arrays: dict[str, np.ndarray] = {}
        for col in df.columns:
            values = df[col].to_numpy()
            if np.issubdtype(values.dtype, np.number):
                arrays[col] = values.astype(float)
        return arrays

    @staticmethod
    def get_values_at(arrays: dict[str, np.ndarray], i: int) -> dict[str, Any]:
        """Row-wise equivalent of get_latest_values, from prepared arrays."""
        values: dict[str, Any] = {}
        for col, series in arrays.items():
            val = series[i]
            if not np.isnan(val):
                values[col] = float(val)
        return values

    def bbw_percentile(self, df: pd.DataFrame, lookback: int = 100) -> float:
        if "bbw" not in df.columns or len(df) < lookback:
            return 50.0
        recent = df["bbw"].dropna().tail(lookback)
        if recent.empty:
            return 50.0
        current = recent.iloc[-1]
        return float((recent < current).sum() / len(recent) * 100)

    def atr_pct_of_price(self, df: pd.DataFrame) -> float:
        if df.empty or "atr_14" not in df.columns:
            return 0.0
        atr = df["atr_14"].iloc[-1]
        price = df["close"].iloc[-1]
        if pd.isna(atr) or price == 0:
            return 0.0
        return float(atr / price * 100)

    def support_resistance(
        self, df: pd.DataFrame, lookback: int = 50, levels: int = 3
    ) -> tuple[list[float], list[float]]:
        if len(df) < lookback:
            return [], []
        recent = df.tail(lookback)
        highs = recent["high"].nlargest(levels).tolist()
        lows = recent["low"].nsmallest(levels).tolist()
        return sorted(lows), sorted(highs, reverse=True)
