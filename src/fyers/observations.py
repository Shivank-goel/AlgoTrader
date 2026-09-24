"""Immutable completed-trade forward evidence."""

from __future__ import annotations

import json

from src.fyers.journal import Journal


def record_trade_observation(journal: Journal, *, protection_id: str, exit_trigger_id: str,
                             evidence_source: str = "LIVE_FORWARD", benchmark_symbol: str = "NSE:NIFTY50-INDEX",
                             benchmark_max_age_seconds: float = 30) -> dict | None:
    """Materialise exactly one observation from an entry protection and exit fill."""
    db = journal.db
    protection = db.execute("SELECT * FROM position_protection WHERE protection_id=?", (protection_id,)).fetchone()
    trigger = db.execute("SELECT * FROM position_exit_triggers WHERE exit_trigger_id=?", (exit_trigger_id,)).fetchone()
    if not protection or not trigger or trigger["status"] != "EXECUTED" or not trigger["exit_fill_id"]:
        return None
    existing = db.execute("SELECT * FROM trade_forward_observations WHERE trade_id=?", (protection_id,)).fetchone()
    if existing:
        return dict(existing)
    entry = db.execute("SELECT * FROM fills WHERE intent_id=?", (protection["intent_id"],)).fetchone()
    exit_fill = db.execute("SELECT * FROM fills WHERE intent_id=?", (trigger["exit_fill_id"],)).fetchone()
    if not entry or not exit_fill or exit_fill["timestamp"] < entry["timestamp"]:
        return None
    quantity = min(entry["quantity"], exit_fill["quantity"])
    if protection["side"] == "SHORT":
        gross = (entry["price"] - exit_fill["price"]) * quantity
    else:
        gross = (exit_fill["price"] - entry["price"]) * quantity
    fees = entry["fee"] + exit_fill["fee"]
    net = gross - fees
    reference_entry = protection["entry_reference_price"]
    reference_exit = trigger["executable_price"]
    gross_return = gross / (entry["price"] * quantity)
    net_return = net / (entry["price"] * quantity)
    before = db.execute("SELECT current_equity FROM equity_checkpoints WHERE timestamp<? ORDER BY timestamp DESC LIMIT 1",
                        (entry["timestamp"],)).fetchone()
    after = db.execute("SELECT current_equity FROM equity_checkpoints WHERE timestamp>=? ORDER BY timestamp LIMIT 1",
                       (exit_fill["timestamp"],)).fetchone()
    try:
        entry_payload = json.loads(entry["intent"])
    except (TypeError, json.JSONDecodeError):
        entry_payload = {}
    row = (f"trade:{protection['protection_id']}", protection["simulation_id"], protection["protection_id"],
           entry_payload.get("execution_mode", "SHADOW"), evidence_source,
           protection["strategy_family"], protection["strategy_version"], protection["symbol"], protection["side"],
           protection["market_regime"], protection["stock_state"], protection["decision_id"], protection["intent_id"],
           entry["timestamp"], reference_entry, entry["price"], exit_fill["timestamp"], reference_exit,
           exit_fill["price"], quantity, protection["stop_loss_price"], protection["take_profit_price"],
           trigger["trigger_type"], gross, gross_return, entry["fee"], exit_fill["fee"], fees, 0.0, 0.0, fees,
           net, net_return, before[0] if before else None, after[0] if after else None, None, None, None, None, None,
           exit_fill["timestamp"] - entry["timestamp"], protection["code_hash"], protection["config_hash"],
           protection["cost_model_hash"], "nse_forward_universe", json.dumps({"source": evidence_source}),
           exit_fill["timestamp"], "GENERATED", None)
    db.execute("INSERT INTO trade_forward_observations VALUES(" + ",".join("?" for _ in row) + ")", row)
    def benchmark(at: float):
        rows = db.execute("SELECT received,payload FROM events WHERE kind='tick' AND received<=? ORDER BY received DESC LIMIT 100",
                          (at,)).fetchall()
        for tick in rows:
            try:
                payload = json.loads(tick["payload"])
                if payload.get("symbol") == benchmark_symbol and float(payload.get("ltp", 0)) > 0:
                    age = at - float(tick["received"])
                    return float(payload["ltp"]), float(tick["received"]), age
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return None
    entry_b, exit_b = benchmark(entry["timestamp"]), benchmark(exit_fill["timestamp"])
    status, reason, br, excess = "COMPLETE", None, None, None
    if not entry_b:
        status, reason = "MISSING_ENTRY", "no prior benchmark tick"
    elif entry_b[2] > benchmark_max_age_seconds:
        status, reason = "STALE_ENTRY", "benchmark entry tick exceeded freshness policy"
    elif not exit_b:
        status, reason = "MISSING_EXIT", "no prior benchmark tick"
    elif exit_b[2] > benchmark_max_age_seconds:
        status, reason = "STALE_EXIT", "benchmark exit tick exceeded freshness policy"
    else:
        br = exit_b[0] / entry_b[0] - 1
        excess = net_return - br
    db.execute("INSERT OR IGNORE INTO trade_benchmark_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (f"trade:{protection['protection_id']}", benchmark_symbol,
                entry_b[0] if entry_b else None, entry_b[1] if entry_b else None, entry_b[2] if entry_b else None,
                exit_b[0] if exit_b else None, exit_b[1] if exit_b else None, exit_b[2] if exit_b else None,
                br, excess, status, reason, exit_fill["timestamp"]))
    db.commit()
    return dict(db.execute("SELECT * FROM trade_forward_observations WHERE trade_id=?", (protection_id,)).fetchone())
