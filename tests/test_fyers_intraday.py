from src.fyers.intraday import IntradayBarAggregator, IntradayFeatureEngine


def tick(symbol, exchange, price, volume):
    return {"symbol": symbol, "exch_feed_time": exchange, "ltp": price,
            "bid_price": price - .1, "ask_price": price + .1,
            "vol_traded_today": volume}


def test_aggregator_emits_only_closed_bars_and_preserves_volume_delta():
    agg = IntradayBarAggregator((60,))
    assert agg.update(tick("NSE:SBIN-EQ", 1000, 100, 10), received_time=1000.2) == []
    assert agg.update(tick("NSE:SBIN-EQ", 1010, 101, 15), received_time=1010.2) == []
    bars = agg.update(tick("NSE:SBIN-EQ", 1060, 102, 19), received_time=1060.2)
    assert len(bars) == 1
    bar = bars[0]
    assert bar.start_time == 960 and bar.end_time == 1020
    assert bar.open == 100 and bar.high == 101 and bar.close == 101
    assert bar.volume == 5 and bar.tick_count == 2 and bar.vwap == 101


def test_feature_engine_uses_completed_bars_and_detects_breakout():
    agg = IntradayBarAggregator((60,))
    engine = IntradayFeatureEngine()
    result = None
    for index in range(22):
        bars = agg.update(tick("NSE:SBIN-EQ", 60 * index, 100 + index, 10 + index),
                          received_time=60 * index + .1)
        for bar in bars:
            result = engine.update(bar)
    assert result is not None
    assert result.return_5m is not None and result.vwap_distance is not None
    assert result.latency_seconds is not None


def test_invalid_or_crossed_tick_is_ignored():
    agg = IntradayBarAggregator((60,))
    assert agg.update({"symbol": "NSE:SBIN-EQ", "exch_feed_time": 1000,
                       "ltp": 100, "bid_price": 101, "ask_price": 100}, received_time=1001) == []
