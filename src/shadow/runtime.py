"""Replay explicit intents with the same risk/accounting used by FYERS paper.

No strategy generation, broker imports or qualification bypass. Use a dedicated
empty journal per replay; this function refuses journals containing prior fills.
"""

from __future__ import annotations

import math

from src.fyers.models import Instrument, Intent, Quote
from src.fyers.paper import PaperBroker


def replay_session(events: list[dict], instruments: dict[str, Instrument], intents: list[Intent],
                   paper: PaperBroker) -> dict:
    if paper.journal.db.execute("SELECT 1 FROM fills LIMIT 1").fetchone():
        raise ValueError("Replay requires an empty paper ledger")
    if len({intent.intent_id for intent in intents}) != len(intents):
        raise ValueError("Duplicate signal/intent IDs in replay input")
    pending = sorted(intents, key=lambda i: (i.created_at.timestamp(), i.intent_id))
    if any(i.created_at.tzinfo is None for i in pending):
        raise ValueError("Replay intents need timezone-aware timestamps")
    quotes = {}
    market_open, checked = False, 0.
    last = -math.inf
    decisions = []
    complete_equity = []
    for event in events:
        now = float(event["received"])
        if not math.isfinite(now) or now < last:
            raise ValueError("Replay clock must be finite and nondecreasing")
        last = now
        kind, data = event["kind"], event["data"]
        if kind in {"connected", "disconnected", "error", "recorder_stopped"}:
            quotes.clear()
            market_open = False
        elif kind == "account_check":
            market_open = data.get("market_open") is True
            checked = now
        elif kind == "tick" and data.get("symbol") in instruments:
            symbol = data["symbol"]
            try:
                quote = Quote(symbol=symbol, bid=data["bid_price"], ask=data["ask_price"],
                              bid_size=data["bid_size"], ask_size=data["ask_size"],
                              exchange_time=data["exch_feed_time"], received_time=now)
                old = quotes.get(symbol)
                if old and quote.exchange_time < old.exchange_time:
                    raise ValueError("Out-of-order quote")
                quotes[symbol] = quote
            except (ValueError, KeyError, TypeError):
                quotes.pop(symbol, None)
        for intent in pending[:]:
            if intent.created_at.timestamp() > now or intent.symbol not in quotes:
                continue
            decision = {"intent_id": intent.intent_id, "strategy": intent.strategy, "at": now}
            reference = quotes[intent.symbol].ask if intent.side.value == "BUY" else quotes[intent.symbol].bid
            try:
                fill = paper.fill(intent, instruments[intent.symbol], quotes, now=now,
                                  market_open=market_open and now - checked < paper.config.reconcile_seconds * 2)
                direction = 1 if intent.side.value == "BUY" else -1
                shortfall = direction * (fill["price"] - reference) / reference * 10000
                decision.update(status="filled", fill=fill, reference_price=reference,
                                execution_shortfall_bps=shortfall)
            except ValueError as exc:
                decision.update(status="rejected", reason=str(exc))
            decisions.append(decision)
            paper.journal.event("shadow_decision", decision, received=now)
            pending.remove(intent)
        valuation = paper.mark_to_market(quotes, now=now)
        if valuation.get("complete") and valuation.get("equity") is not None:
            complete_equity.append(valuation["equity"])
    filled = [decision for decision in decisions if decision["status"] == "filled"]
    rejected = [decision for decision in decisions if decision["status"] == "rejected"]
    peak = 0.0
    max_drawdown = 0.0
    for equity in complete_equity:
        peak = max(peak, equity)
        if peak:
            max_drawdown = max(max_drawdown, 1 - equity / peak)
    metrics = {
        "requested": len(intents), "filled": len(filled), "rejected": len(rejected),
        "unexecuted": len(pending),
        "fill_rate": len(filled) / len(intents) if intents else 0.0,
        "turnover_inr": sum(row["fill"]["price"] * row["fill"]["quantity"] for row in filled),
        "mean_execution_shortfall_bps": (
            sum(row["execution_shortfall_bps"] for row in filled) / len(filled) if filled else None),
        "max_drawdown_fraction": max_drawdown,
    }
    return {"decisions": decisions, "unexecuted_intents": [i.intent_id for i in pending],
            "valuation": paper.journal.get("paper_valuation"), "forward_metrics": metrics,
            "live_enabled": False}
