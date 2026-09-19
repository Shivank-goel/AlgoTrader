"""Read-only FYERS dashboard projection; malformed sections fail independently."""

from __future__ import annotations

import json
import sqlite3
import time

from src.fyers.models import ROOT, RuntimeConfig
from src.fyers.operations import readiness


def _json(value: str | None, default=None):
    try:
        return json.loads(value) if value is not None else default
    except (TypeError, json.JSONDecodeError):
        return default


def dashboard_snapshot(config: RuntimeConfig | None = None, *, now: float | None = None) -> dict:
    config = config or RuntimeConfig.load()
    now = time.time() if now is None else now
    result = {"as_of": now, "broker": "FYERS", "market": "NSE cash",
              "mode": "paper/shadow", "live_enabled": False,
              "readiness": readiness(config, now=now), "sections": {}, "errors": []}
    path = ROOT / config.database
    if not path.exists():
        result["errors"].append("journal unavailable")
        return result
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
    except sqlite3.Error:
        result["errors"].append("journal unreadable")
        return result
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        states = {row["key"]: _json(row["value"]) for row in db.execute("SELECT key,value FROM state")}
        result["sections"]["service"] = {
            "last_event": dict(db.execute("SELECT received,kind FROM events ORDER BY id DESC LIMIT 1").fetchone() or {}),
            "account": states.get("account_check"), "halt_reason": states.get("halt_reason"),
            "valuation": states.get("paper_valuation"), "stale_seconds": config.stale_seconds,
        }
        ticks = {}
        for row in db.execute("SELECT received,payload FROM events WHERE kind='tick' ORDER BY id DESC LIMIT 500"):
            data = _json(row["payload"], {})
            symbol = data.get("symbol")
            if symbol not in ticks:
                exchange = data.get("exch_feed_time")
                latency = row["received"] - exchange if isinstance(exchange, int | float) else None
                bid, ask = data.get("bid_price"), data.get("ask_price")
                spread = ((ask - bid) / ((ask + bid) / 2) * 10000
                          if all(isinstance(v, int | float) and v > 0 for v in (bid, ask)) and ask >= bid else None)
                ticks[symbol] = {**data, "received": row["received"], "age_seconds": now - row["received"],
                                 "latency_seconds": latency, "spread_bps": spread,
                                 "fresh": now - row["received"] <= config.stale_seconds}
        result["sections"]["quotes"] = [ticks[s] for s in sorted(ticks) if s]
        result["sections"]["positions"] = [dict(row) for row in db.execute("SELECT * FROM positions WHERE quantity!=0 ORDER BY symbol")]
        result["sections"]["fills"] = [dict(row) for row in db.execute("SELECT * FROM fills ORDER BY timestamp DESC LIMIT 50")]
        events = []
        for row in db.execute("SELECT received,kind,payload FROM events WHERE kind IN ('paper_rejected','intent_scheduled','error','account_error','halt') ORDER BY id DESC LIMIT 50"):
            events.append({"received": row["received"], "kind": row["kind"], "data": _json(row["payload"], {})})
        result["sections"]["events"] = events
        result["sections"]["settlements"] = ([dict(row) for row in db.execute(
            "SELECT * FROM settlement_obligations ORDER BY due_day,intent_id")] if "settlement_obligations" in tables else [])
        result["sections"]["orders"] = ([dict(row) for row in db.execute(
            "SELECT intent_id,broker_id,status,filled FROM live_orders ORDER BY rowid DESC LIMIT 50")] if "live_orders" in tables else [])
        result["sections"]["scheduled_intents"] = ([dict(row) for row in db.execute(
            "SELECT intent_id,evidence_sha256,created FROM scheduled_intents ORDER BY created DESC LIMIT 50")] if "scheduled_intents" in tables else [])
        result["sections"]["reconciliation"] = states.get("broker_reconciliation")
        result["sections"]["accounting"] = {
            "cash": states.get("cash"), "initial_capital": states.get("initial_capital"),
            "realized_pnl": states.get("realized_pnl"), "total_fees": states.get("total_fees"),
        }
        result["sections"]["strategy_lab"] = states.get("strategy_lab_status") or {
            "enabled": False, "reason": "strategy lab has not started", "candidates": []}
        result["sections"]["strategy_observations"] = ([dict(row) for row in db.execute(
            "SELECT candidate_id,decision_at,due_at,kind,selected,evaluated_at,outcome,net_return,"
            "benchmark_return,excess_return FROM strategy_observations "
            "ORDER BY decision_at DESC LIMIT 50")] if "strategy_observations" in tables else [])
    except (sqlite3.Error, TypeError, ValueError) as exc:
        result["errors"].append(f"journal projection unavailable: {type(exc).__name__}")
    finally:
        db.close()
    return result
