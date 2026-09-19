"""Explicit-window quote coverage; does not infer trading sessions from silence."""

from __future__ import annotations

import math
import statistics

from src.fyers.models import Quote


def quote_quality(events: list[dict], symbols: list[str], *, start: float, end: float, stale_seconds: float) -> dict:
    if not all(math.isfinite(x) for x in (start, end, stale_seconds)) or end <= start or stale_seconds <= 0:
        raise ValueError("Invalid quality window")
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("Unique symbols required")
    state: dict[str, Quote] = {}
    stats = {s: {"ticks": 0, "invalid": 0, "out_of_order": 0, "duplicates": 0,
                 "usable_seconds": 0., "spreads_bps": [], "latencies": []} for s in symbols}
    last = start
    clock_regressions = 0
    connections = 0

    def accrue(until):
        for symbol, quote in state.items():
            valid_end = min(until, end, quote.received_time + stale_seconds, quote.exchange_time + stale_seconds)
            stats[symbol]["usable_seconds"] += max(0., valid_end - max(last, start, quote.received_time, quote.exchange_time))

    for event in events:
        received = float(event["received"])
        if not math.isfinite(received):
            raise ValueError("Nonfinite journal clock")
        if received > end:
            continue
        if received < start:
            continue  # deliberately no assumed warm-start book
        if received < last:
            clock_regressions += 1
            state.clear()
            continue
        accrue(received)
        last = received
        kind, data = event["kind"], event["data"]
        if kind in {"connected", "disconnected", "error", "recorder_stopped"}:
            state.clear()
            connections += int(kind == "connected")
        elif kind == "tick" and data.get("symbol") in stats:
            symbol = data["symbol"]
            row = stats[symbol]
            row["ticks"] += 1
            try:
                quote = Quote(symbol=symbol, bid=data["bid_price"], ask=data["ask_price"],
                              bid_size=data["bid_size"], ask_size=data["ask_size"],
                              exchange_time=data["exch_feed_time"], received_time=received)
                if not quote.usable(received, stale_seconds):
                    raise ValueError("Non-executable quote")
            except (ValueError, KeyError, TypeError):
                row["invalid"] += 1
                state.pop(symbol, None)
                continue
            previous = state.get(symbol)
            if previous and quote.exchange_time < previous.exchange_time:
                row["out_of_order"] += 1
                state.pop(symbol, None)
                continue
            if previous and quote.model_dump(exclude={"received_time"}) == previous.model_dump(exclude={"received_time"}):
                row["duplicates"] += 1
                continue
            state[symbol] = quote
            row["spreads_bps"].append((quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2) * 10000)
            row["latencies"].append(quote.received_time - quote.exchange_time)
    accrue(end)
    for row in stats.values():
        spreads = row.pop("spreads_bps")
        latencies = row.pop("latencies")
        row["median_spread_bps"] = statistics.median(spreads) if spreads else None
        row["median_latency_seconds"] = statistics.median(latencies) if latencies else None
        row["max_latency_seconds"] = max(latencies) if latencies else None
        row["coverage_fraction"] = row["usable_seconds"] / (end - start)
        row["unusable_seconds"] = end - start - row["usable_seconds"]
    return {"start": start, "end": end, "symbols": stats, "connections": connections,
            "clock_regressions": clock_regressions, "session_inference": False,
            "note": "Caller selects the eligible session window; missing intervals are never forward-filled beyond freshness."}
