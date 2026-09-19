"""Qualified target-to-intent scheduling; produces paper intents, never broker calls."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from src.fyers.journal import Journal
from src.fyers.models import Instrument, Intent, ROOT, RuntimeConfig, Side
from src.fyers.qualification import digest, qualification


class QualifiedIntentScheduler:
    """Turn reviewed target holdings into idempotent, auditable paper intents."""

    def __init__(self, journal: Journal, config: RuntimeConfig,
                 instruments: dict[str, Instrument], strategy: str) -> None:
        self.journal = journal
        self.config = config
        self.instruments = instruments
        self.strategy = strategy
        journal.db.execute("""CREATE TABLE IF NOT EXISTS scheduled_intents (
            intent_id TEXT PRIMARY KEY, evidence_sha256 TEXT NOT NULL,
            payload TEXT NOT NULL, created REAL NOT NULL)""")
        journal.db.commit()

    def schedule(self, targets: dict[str, int], current: dict[str, int], *, at: datetime) -> list[Intent]:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Schedule timestamp must be timezone-aware")
        passed, strategy = qualification(ROOT / self.config.qualification_file,
                                          ROOT / self.config.trials_file)
        if not passed or strategy != self.strategy:
            raise ValueError("Strategy evidence is not currently qualified")
        if self.journal.get("halt_reason"):
            raise ValueError("FYERS account is halted")
        symbols = set(targets) | set(current)
        if any(symbol not in self.instruments for symbol in symbols):
            raise ValueError("Target contains an unresolved instrument")
        if any(type(quantity) is not int or quantity < 0 for quantity in [*targets.values(), *current.values()]):
            raise ValueError("Targets and holdings must be non-negative integers")
        evidence = digest(ROOT / self.config.qualification_file)
        batch = json.dumps({"strategy": self.strategy, "at": at.astimezone(timezone.utc).isoformat(),
                            "targets": targets, "current": current, "evidence": evidence},
                           sort_keys=True, separators=(",", ":"))
        prefix = hashlib.sha256(batch.encode()).hexdigest()[:24]
        intents = []
        for symbol in sorted(symbols):
            delta = targets.get(symbol, 0) - current.get(symbol, 0)
            if not delta:
                continue
            instrument = self.instruments[symbol]
            if abs(delta) % instrument.lot_size:
                raise ValueError("Target delta violates instrument lot size")
            intent = Intent(intent_id=f"{prefix}:{symbol}", strategy=self.strategy, symbol=symbol,
                            side=Side.BUY if delta > 0 else Side.SELL, quantity=abs(delta), created_at=at)
            encoded = intent.model_dump_json()
            old = self.journal.db.execute("SELECT payload,evidence_sha256 FROM scheduled_intents WHERE intent_id=?",
                                          (intent.intent_id,)).fetchone()
            if old and (old["payload"] != encoded or old["evidence_sha256"] != evidence):
                raise ValueError("Scheduled intent identity conflict")
            if not old:
                with self.journal.db:
                    self.journal.db.execute("INSERT INTO scheduled_intents VALUES(?,?,?,?)",
                                            (intent.intent_id, evidence, encoded, at.timestamp()))
                    self.journal.db.execute("INSERT INTO events(received,kind,payload) VALUES(?,?,?)",
                                            (at.timestamp(), "intent_scheduled", encoded))
            intents.append(intent)
        return intents
