"""Quote-level long protection exits for the shadow PaperBroker only."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from src.fyers.models import Instrument, Intent, Quote, Side
from src.fyers.observations import record_trade_observation
from src.fyers.paper import PaperBroker


class ShadowProtectionMonitor:
    """Trigger and execute one deterministic exit per active protected fill."""

    def __init__(self, paper: PaperBroker, instruments: dict[str, Instrument]) -> None:
        self.paper, self.instruments = paper, instruments

    def evaluate(self, quote: Quote, *, now: float, market_open: bool,
                 force_short_close: bool = False) -> list[dict]:
        if not market_open or not quote.usable(now, self.paper.config.stale_seconds):
            return []
        db, simulation_id = self.paper.journal.db, self.paper.journal.get("simulation_id")
        rows = db.execute(
            "SELECT * FROM position_protection WHERE simulation_id=? AND status='ACTIVE' AND symbol=?",
            (simulation_id, quote.symbol),
        ).fetchall()
        results = []
        for row in rows:
            stop, target = row["stop_loss_price"], row["take_profit_price"]
            executable = quote.ask if row["side"] == "SHORT" else quote.bid
            trigger_type = (("SESSION_CLOSE" if force_short_close and row["side"] == "SHORT" else
                             ("STOP_LOSS" if executable >= stop else
                              ("TAKE_PROFIT" if target is not None and executable <= target else None)))
                            if row["side"] == "SHORT" else
                            ("STOP_LOSS" if executable <= stop else
                             ("TAKE_PROFIT" if target is not None and executable >= target else None)))
            db.execute("UPDATE position_protection SET last_executable_price=? WHERE protection_id=?", (executable, row["protection_id"]))
            if trigger_type is None:
                db.commit()
                continue
            level = stop if trigger_type == "STOP_LOSS" else target
            if trigger_type == "SESSION_CLOSE":
                level = executable
            trigger_id = f"exit:{row['protection_id']}"
            existing = db.execute("SELECT * FROM position_exit_triggers WHERE exit_trigger_id=?", (trigger_id,)).fetchone()
            if existing and existing["status"] == "EXECUTED":
                continue
            if existing is None:
                previous = row["last_executable_price"]
                db.execute(
                    "INSERT INTO position_exit_triggers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (trigger_id, simulation_id, row["protection_id"], row["intent_id"],
                     f"{trigger_id}:intent", quote.symbol, row["side"], row["strategy_family"],
                     row["strategy_version"], trigger_type, level, previous, executable, executable,
                     "ASK" if row["side"] == "SHORT" else "BID", executable - level, now, "TRIGGERED", None, now),
                )
                db.commit()
            intent = Intent(
                intent_id=f"{trigger_id}:intent", strategy=row["strategy_family"],
                symbol=quote.symbol, side=Side.BUY if row["side"] == "SHORT" else Side.SELL,
                quantity=row["quantity"], position_side=row["side"], intraday_only=row["side"] == "SHORT",
                created_at=datetime.fromtimestamp(now, UTC), execution_mode="SHADOW",
                lifecycle_status="SHADOW_FORWARD", decision_id=row["decision_id"],
                strategy_version=row["strategy_version"], market_regime=row["market_regime"],
                reason=f"{trigger_type.lower()} crossing",
            )
            try:
                fill = self.paper.fill(intent, self.instruments[quote.symbol], {quote.symbol: quote},
                                       now=now, market_open=market_open)
            except ValueError as exc:
                db.execute("UPDATE position_exit_triggers SET status='FAILED' WHERE exit_trigger_id=?", (trigger_id,))
                db.commit()
                results.append({"trigger_id": trigger_id, "status": "FAILED", "reason": str(exc)[:200]})
                continue
            db.execute("UPDATE position_exit_triggers SET status='EXECUTED',exit_fill_id=? WHERE exit_trigger_id=?",
                       (fill["intent_id"], trigger_id))
            db.execute("UPDATE position_protection SET status='CLOSED' WHERE protection_id=?", (row["protection_id"],))
            record_trade_observation(self.paper.journal, protection_id=row["protection_id"],
                                     exit_trigger_id=trigger_id,
                                     benchmark_max_age_seconds=self.paper.config.benchmark_max_age_seconds)
            db.execute("INSERT INTO events(received,kind,payload) VALUES(?,?,?)",
                       (now, "shadow_exit", json.dumps({"trigger_id": trigger_id, "trigger_type": trigger_type,
                                                          "fill_id": fill["intent_id"]})))
            db.commit()
            results.append({"trigger_id": trigger_id, "status": "EXECUTED", "trigger_type": trigger_type,
                            "fill": fill})
        return results
