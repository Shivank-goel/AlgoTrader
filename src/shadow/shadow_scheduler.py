"""Explicit candidate-to-PaperBroker scheduler; never reaches a broker gateway."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from src.fyers.journal import Journal
from src.fyers.models import ROOT, Instrument, Intent, RuntimeConfig, Side
from src.fyers.qualification import digest
from src.fyers.sizing import DynamicShadowSizer, SizingDecision
from src.shadow.continuous import CandidateLifecycle, StrategyCandidate
from src.strategies.protection import ProtectionPlan


class ShadowIntentScheduler:
    """Schedule registered candidate signals for simulation only."""

    def __init__(self, journal: Journal, config: RuntimeConfig,
                 instruments: dict[str, Instrument], candidate: StrategyCandidate) -> None:
        self.journal, self.config, self.instruments, self.candidate = journal, config, instruments, candidate
        if candidate.lifecycle not in {CandidateLifecycle.CANDIDATE, CandidateLifecycle.SHADOW_FORWARD}:
            raise ValueError("candidate is not in a shadow lifecycle")
        if not candidate.shadow_enabled:
            raise ValueError("candidate shadow execution is disabled")
        journal.db.execute("""CREATE TABLE IF NOT EXISTS scheduled_intents (
            intent_id TEXT PRIMARY KEY, evidence_sha256 TEXT NOT NULL,
            payload TEXT NOT NULL, created REAL NOT NULL)""")
        journal.db.commit()

    def schedule(self, targets: dict[str, int], current: dict[str, int], *, at: datetime,
                 market_regime: str, decision_id: str, reason: str,
                 protection: ProtectionPlan | None = None,
                 quotes: dict[str, object] | None = None,
                 sizing: DynamicShadowSizer | None = None,
                 risk_multiplier: float = 1.0, action: str = "LONG") -> list[Intent]:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Schedule timestamp must be timezone-aware")
        if self.journal.get("halt_reason"):
            raise ValueError("FYERS account is halted")
        if market_regime not in {"trending_up", "ranging"}:
            return []
        symbols = set(targets) | set(current)
        if any(symbol not in self.instruments for symbol in symbols):
            raise ValueError("Target contains an unresolved instrument")
        if any(type(quantity) is not int or quantity < 0 for quantity in [*targets.values(), *current.values()]):
            raise ValueError("Targets and holdings must be non-negative integers")
        config_hash = digest(ROOT / self.config.strategy_lab_file)
        batch = json.dumps({"candidate": self.candidate.model_dump(mode="json"), "targets": targets,
                            "current": current, "market_regime": market_regime, "decision_id": decision_id},
                           sort_keys=True, separators=(",", ":"))
        evidence = hashlib.sha256(batch.encode()).hexdigest()
        intents = []
        for symbol in sorted(symbols):
            delta = targets.get(symbol, 0) - current.get(symbol, 0)
            if not delta:
                continue
            if delta < 0:
                raise ValueError("Targets must be positive quantities")
            instrument = self.instruments[symbol]
            if delta % instrument.lot_size:
                raise ValueError("Target delta violates instrument lot size")
            sizing_decision: SizingDecision | None = None
            if sizing is not None:
                if protection is None or quotes is None or symbol not in quotes:
                    raise ValueError("dynamic sizing requires protection and a current quote")
                size_method = sizing.size_long if action == "LONG" else sizing.size_short
                sizing_decision = size_method(
                    symbol=symbol, requested_quantity=delta,
                    reference_price=quotes[symbol].ask if action == "LONG" else quotes[symbol].bid,
                    stop_loss=protection.stop_loss_price, instrument=instrument,
                    quote=quotes[symbol], risk_multiplier=risk_multiplier,
                )
                if sizing_decision.final_quantity <= 0:
                    self.journal.event("shadow_sizing", sizing_decision.model_dump(mode="json"), at.timestamp())
                    continue
                delta = sizing_decision.final_quantity
                self.journal.event("shadow_sizing", sizing_decision.model_dump(mode="json"), at.timestamp())
            intent = Intent(
                intent_id=f"shadow:{self.candidate.id}:{decision_id}:{symbol}",
                strategy=self.candidate.id, symbol=symbol,
                side=Side.BUY if action == "LONG" else Side.SELL, quantity=delta, created_at=at,
                execution_mode="SHADOW", lifecycle_status=self.candidate.lifecycle.value,
                market_regime=market_regime, decision_id=decision_id,
                config_hash=config_hash, reason=reason,
                protection_required=self.candidate.protection_required,
                strategy_version=self.candidate.version,
                position_side=action, intraday_only=action == "SHORT",
                stop_loss=protection.stop_loss_price if protection else None,
                take_profit=protection.take_profit_price if protection else None,
                protection_method=protection.stop_method if protection else None,
                protection_inputs=protection.calculation_inputs if protection else None,
                sizing=sizing_decision.model_dump(mode="json") if sizing_decision else None,
            )
            encoded = intent.model_dump_json()
            old = self.journal.db.execute(
                "SELECT payload,evidence_sha256 FROM scheduled_intents WHERE intent_id=?", (intent.intent_id,)
            ).fetchone()
            if old and (old["payload"] != encoded or old["evidence_sha256"] != evidence):
                raise ValueError("Shadow intent identity conflict")
            if not old:
                with self.journal.db:
                    self.journal.db.execute("INSERT INTO scheduled_intents VALUES(?,?,?,?)",
                                            (intent.intent_id, evidence, encoded, at.timestamp()))
                    journal_payload = {"intent_id": intent.intent_id, "execution_mode": "SHADOW",
                                       "strategy": self.candidate.id, "symbol": symbol}
                    self.journal.event("shadow_intent_scheduled", journal_payload, at.timestamp())
            intents.append(intent)
        return intents
