"""Deterministic completed intraday bars and point-in-time features from FYERS ticks."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class IntradayBar:
    symbol: str
    interval_seconds: int
    start_time: float
    end_time: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    tick_count: int
    vwap: float | None
    average_spread_bps: float | None
    max_spread_bps: float | None
    average_latency_seconds: float | None
    complete: bool = True


@dataclass(frozen=True)
class IntradayFeatures:
    symbol: str
    interval_seconds: int
    end_time: float
    return_5m: float | None
    return_15m: float | None
    return_30m: float | None
    vwap_distance: float | None
    volatility: float | None
    volume_ratio: float | None
    breakout: bool
    breakdown: bool
    spread_bps: float | None
    latency_seconds: float | None
    warm: bool


def _number(value: object, *, positive: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    if positive and value <= 0:
        return None
    return float(value)


class IntradayBarAggregator:
    """Aggregate ticks into closed exchange-time bars; never emits an open bar."""

    def __init__(self, intervals: tuple[int, ...] = (60, 300)) -> None:
        if not intervals or any(type(value) is not int or value <= 0 for value in intervals):
            raise ValueError("intervals must contain positive seconds")
        self.intervals = intervals
        self._active: dict[tuple[str, int], dict] = {}
        self._last_volume: dict[str, float] = {}

    def update(self, tick: dict, *, received_time: float) -> list[IntradayBar]:
        symbol = tick.get("symbol")
        exchange = _number(tick.get("exch_feed_time"), positive=True)
        price = _number(tick.get("ltp"), positive=True)
        bid, ask = _number(tick.get("bid_price"), positive=True), _number(tick.get("ask_price"), positive=True)
        if not isinstance(symbol, str) or exchange is None or price is None or bid is None or ask is None or ask < bid:
            return []
        received = _number(received_time, positive=True)
        if received is None:
            return []
        cumulative = _number(tick.get("vol_traded_today"), positive=False)
        previous_volume = self._last_volume.get(symbol)
        volume = 0.0 if cumulative is None else max(0.0, cumulative - previous_volume) if previous_volume is not None else 0.0
        if cumulative is not None:
            self._last_volume[symbol] = cumulative
        spread = (ask - bid) / ((ask + bid) / 2) * 10000 if ask + bid > 0 else None
        latency = max(0.0, received - exchange)
        output: list[IntradayBar] = []
        for interval in self.intervals:
            start = math.floor(exchange / interval) * interval
            key = (symbol, interval)
            current = self._active.get(key)
            if current is not None and start < current["start_time"]:
                continue
            if current is not None and start > current["start_time"]:
                output.append(self._close(current))
                current = None
            if current is None:
                current = {"symbol": symbol, "interval_seconds": interval, "start_time": start,
                           "open": price, "high": price, "low": price, "close": price,
                           "volume": volume, "tick_count": 1, "vwap_value": price * volume,
                           "spreads": [spread] if spread is not None else [],
                           "latencies": [latency]}
                self._active[key] = current
            else:
                current["high"] = max(current["high"], price)
                current["low"] = min(current["low"], price)
                current["close"] = price
                current["volume"] += volume
                current["tick_count"] += 1
                current["vwap_value"] += price * volume
                if spread is not None:
                    current["spreads"].append(spread)
                current["latencies"].append(latency)
        return output

    @staticmethod
    def _close(current: dict) -> IntradayBar:
        volume = current["volume"]
        spreads, latencies = current["spreads"], current["latencies"]
        return IntradayBar(
            symbol=current["symbol"], interval_seconds=current["interval_seconds"],
            start_time=current["start_time"], end_time=current["start_time"] + current["interval_seconds"],
            open=current["open"], high=current["high"], low=current["low"], close=current["close"],
            volume=volume, tick_count=current["tick_count"],
            vwap=current["vwap_value"] / volume if volume > 0 else None,
            average_spread_bps=sum(spreads) / len(spreads) if spreads else None,
            max_spread_bps=max(spreads) if spreads else None,
            average_latency_seconds=sum(latencies) / len(latencies) if latencies else None,
        )


class IntradayFeatureEngine:
    """Calculate a bounded, interpretable feature set from completed bars only."""

    def __init__(self, *, history: int = 60) -> None:
        self.history = history
        self._bars: dict[tuple[str, int], deque[IntradayBar]] = defaultdict(lambda: deque(maxlen=history))

    def update(self, bar: IntradayBar) -> IntradayFeatures:
        if not bar.complete:
            raise ValueError("features require a completed bar")
        bars = self._bars[(bar.symbol, bar.interval_seconds)]
        previous = list(bars)
        bars.append(bar)
        closes = [row.close for row in previous] + [bar.close]
        def ret(lookback: int) -> float | None:
            return closes[-1] / closes[-1 - lookback] - 1 if len(closes) > lookback else None
        returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0]
        volatility = (sum((value - sum(returns[-20:]) / len(returns[-20:])) ** 2 for value in returns[-20:])
                      / (len(returns[-20:]) - 1)) ** .5 if len(returns) >= 3 else None
        prior = previous[-20:]
        breakout = bool(prior and bar.close > max(row.high for row in prior))
        breakdown = bool(prior and bar.close < min(row.low for row in prior))
        volumes = [row.volume for row in previous[-20:] if row.volume > 0]
        volume_ratio = bar.volume / (sum(volumes) / len(volumes)) if volumes and bar.volume > 0 else None
        return IntradayFeatures(
            symbol=bar.symbol, interval_seconds=bar.interval_seconds, end_time=bar.end_time,
            return_5m=ret(5), return_15m=ret(15), return_30m=ret(30),
            vwap_distance=(bar.close / bar.vwap - 1 if bar.vwap else None), volatility=volatility,
            volume_ratio=volume_ratio, breakout=breakout, breakdown=breakdown,
            spread_bps=bar.average_spread_bps, latency_seconds=bar.average_latency_seconds,
            warm=len(closes) >= 30,
        )
