"""Read-only FYERS dashboard projection; malformed sections fail independently."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time

from src.fyers.daily_data import load_completed_bar
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
    try:
        evidence = json.loads((ROOT / config.qualification_file).read_text())
        economics = evidence.get("economic_evidence") if isinstance(evidence, dict) else None
        if isinstance(economics, dict):
            result["sections"]["qualification_economics"] = {
                key: economics.get(key) for key in (
                    "benchmark_id", "excess_lower_confidence_bound",
                    "after_tax_and_infrastructure_excess", "break_even_capital_inr",
                )
            }
    except (OSError, TypeError, json.JSONDecodeError):
        pass
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
            "authentication": states.get("authentication"),
            "preflight": states.get("preflight"),
            "finalization": states.get("session_finalization"),
            "heartbeat": states.get("recorder_supervisor"),
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
        for row in db.execute("SELECT received,kind,payload FROM events WHERE kind IN ('paper_rejected','intent_scheduled','error','account_error','authentication_required','session_finalization','halt') ORDER BY id DESC LIMIT 50"):
            events.append({"received": row["received"], "kind": row["kind"], "data": _json(row["payload"], {})})
        result["sections"]["events"] = events
        result["sections"]["settlements"] = ([dict(row) for row in db.execute(
            "SELECT * FROM settlement_obligations ORDER BY due_day,intent_id")] if "settlement_obligations" in tables else [])
        result["sections"]["orders"] = ([dict(row) for row in db.execute(
            "SELECT intent_id,broker_id,status,filled FROM live_orders ORDER BY rowid DESC LIMIT 50")] if "live_orders" in tables else [])
        result["sections"]["paper_orders"] = ([dict(row) for row in db.execute(
            "SELECT intent_id,requested,filled,status,created,updated FROM paper_orders "
            "ORDER BY updated DESC LIMIT 50")] if "paper_orders" in tables else [])
        result["sections"]["tax_lots"] = ([dict(row) for row in db.execute(
            "SELECT symbol,acquired_at,remaining,unit_cost FROM tax_lots WHERE remaining>0 "
            "ORDER BY acquired_at")] if "tax_lots" in tables else [])
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
        result["sections"]["regime_observations"] = ([dict(row) for row in db.execute(
            "SELECT family,decision_day,entry_day,due_day,evaluated_day,outcome,net_return,"
            "benchmark_return,excess_return FROM regime_forward_observations_v2 "
            "ORDER BY decision_day DESC LIMIT 100")] if "regime_forward_observations_v2" in tables else [])
        daily_files = sorted((ROOT / config.daily_bars_directory).glob("????-??-??.json"))
        latest_bar = None
        if daily_files:
            try:
                artifact = load_completed_bar(daily_files[-1])
                latest_bar = {
                    "session_date": artifact.session_date.isoformat(),
                    "sha256": artifact.content_sha256,
                    "missing_symbols": artifact.missing_symbols,
                    "benchmark_available": artifact.benchmark is not None,
                }
            except (OSError, ValueError):
                result["errors"].append("latest completed bar is corrupt")
        usage = shutil.disk_usage(ROOT)
        result["sections"]["operations"] = {
            "last_completed_bar": latest_bar,
            "disk_free_bytes": usage.free,
            "disk_total_bytes": usage.total,
            "offsite_backup": states.get("offsite_backup"),
            "alert_delivery": states.get("alert_delivery"),
            "ledger_reconciliation": states.get("ledger_reconciliation"),
        }
    except (sqlite3.Error, TypeError, ValueError) as exc:
        result["errors"].append(f"journal projection unavailable: {type(exc).__name__}")
    finally:
        db.close()
    registry = ROOT / "data/research/experiments.sqlite3"
    if registry.exists():
        try:
            research = sqlite3.connect(registry.resolve().as_uri() + "?mode=ro", uri=True)
            research.row_factory = sqlite3.Row
            access = [dict(row) for row in research.execute(
                "SELECT campaign,experiment,accessed FROM holdout_access "
                "ORDER BY accessed DESC LIMIT 20")]
            unfinished = research.execute(
                "SELECT COUNT(*) FROM experiments x WHERE NOT EXISTS "
                "(SELECT 1 FROM events e WHERE e.experiment=x.id AND e.kind IN ('published','failed'))"
            ).fetchone()[0]
            research.close()
            result["sections"]["research"] = {
                "holdout_access": access, "unfinished_experiments": unfinished,
                "holdout_status": "CONSUMED" if access else "SEALED",
            }
        except sqlite3.Error:
            result["errors"].append("research registry unavailable")
    return result
