"""Durable event journal and paper account; a single writer owns each connection."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path


class Journal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, received REAL NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fills (
                intent_id TEXT PRIMARY KEY, intent TEXT NOT NULL, symbol TEXT NOT NULL,
                side TEXT NOT NULL, quantity INTEGER NOT NULL, price REAL NOT NULL,
                fee REAL NOT NULL, timestamp REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS dp_charges (
                isin TEXT NOT NULL, day TEXT NOT NULL, PRIMARY KEY(isin, day));
            CREATE TABLE IF NOT EXISTS settlement_obligations (
                intent_id TEXT PRIMARY KEY, trade_day TEXT NOT NULL, due_day TEXT NOT NULL,
                symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
                cash_amount REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS tax_lots (
                lot_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, acquired_at REAL NOT NULL,
                remaining INTEGER NOT NULL CHECK(remaining>=0), unit_cost REAL NOT NULL CHECK(unit_cost>0));
            CREATE TABLE IF NOT EXISTS realized_tax_lots (
                execution_id TEXT NOT NULL, lot_id TEXT NOT NULL, symbol TEXT NOT NULL,
                sold_at REAL NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0),
                proceeds REAL NOT NULL, cost_basis REAL NOT NULL, gain REAL NOT NULL,
                holding_days INTEGER NOT NULL CHECK(holding_days>=0),
                PRIMARY KEY(execution_id,lot_id));
            CREATE TABLE IF NOT EXISTS paper_orders (
                intent_id TEXT PRIMARY KEY, intent TEXT NOT NULL, requested INTEGER NOT NULL,
                filled INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_intent_dispatch (
                intent_id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT,
                updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS shadow_simulations (
                simulation_id TEXT PRIMARY KEY, execution_mode TEXT NOT NULL,
                initial_capital REAL NOT NULL, started_at REAL NOT NULL,
                status TEXT NOT NULL, code_hash TEXT NOT NULL,
                config_hash TEXT NOT NULL, cost_model_hash TEXT NOT NULL,
                universe_id TEXT NOT NULL, data_provenance TEXT NOT NULL,
                created_at REAL NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_shadow_simulation
                ON shadow_simulations(status) WHERE status='ACTIVE';
            CREATE TABLE IF NOT EXISTS equity_checkpoints (
                checkpoint_id TEXT PRIMARY KEY, simulation_id TEXT NOT NULL,
                timestamp REAL NOT NULL, event_type TEXT NOT NULL,
                source_event_id TEXT NOT NULL, cash REAL NOT NULL,
                market_value REAL NOT NULL, realized_pnl REAL NOT NULL,
                unrealized_pnl REAL NOT NULL, fees REAL NOT NULL,
                current_equity REAL NOT NULL, peak_equity REAL NOT NULL,
                drawdown REAL NOT NULL, gross_exposure REAL NOT NULL,
                net_exposure REAL NOT NULL, long_exposure REAL NOT NULL,
                UNIQUE(simulation_id,event_type,source_event_id));
            CREATE TABLE IF NOT EXISTS position_protection (
                protection_id TEXT PRIMARY KEY, simulation_id TEXT NOT NULL,
                intent_id TEXT NOT NULL UNIQUE, decision_id TEXT, symbol TEXT NOT NULL,
                strategy_family TEXT NOT NULL, strategy_version TEXT,
                side TEXT NOT NULL, entry_time REAL NOT NULL,
                entry_reference_price REAL NOT NULL, entry_fill_price REAL NOT NULL,
                quantity INTEGER NOT NULL, stop_loss_price REAL,
                take_profit_price REAL, initial_risk_per_share REAL,
                initial_risk_rupees REAL, initial_risk_percent REAL,
                account_equity_at_entry REAL NOT NULL, capital_committed REAL NOT NULL,
                market_regime TEXT, stock_state TEXT, status TEXT NOT NULL,
                created_at REAL NOT NULL, code_hash TEXT, config_hash TEXT,
                cost_model_hash TEXT);
            CREATE INDEX IF NOT EXISTS position_protection_active
                ON position_protection(simulation_id,status,symbol);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(position_protection)")}
        if "protection_method" not in columns:
            self.db.execute("ALTER TABLE position_protection ADD COLUMN protection_method TEXT")
        if "protection_inputs" not in columns:
            self.db.execute("ALTER TABLE position_protection ADD COLUMN protection_inputs TEXT")
        if "last_executable_price" not in columns:
            self.db.execute("ALTER TABLE position_protection ADD COLUMN last_executable_price REAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS position_exit_triggers (
                exit_trigger_id TEXT PRIMARY KEY, simulation_id TEXT NOT NULL,
                protection_id TEXT NOT NULL, position_id TEXT, intent_id TEXT NOT NULL,
                symbol TEXT NOT NULL, side TEXT NOT NULL, strategy_family TEXT NOT NULL,
                strategy_version TEXT, trigger_type TEXT NOT NULL,
                protection_level REAL NOT NULL, previous_price REAL,
                trigger_price REAL NOT NULL, executable_price REAL NOT NULL,
                price_source TEXT NOT NULL, gap_from_level REAL NOT NULL,
                triggered_at REAL NOT NULL, status TEXT NOT NULL,
                exit_fill_id TEXT, created_at REAL NOT NULL,
                UNIQUE(protection_id));
            CREATE INDEX IF NOT EXISTS position_exit_triggers_lookup
                ON position_exit_triggers(simulation_id,symbol,status);
            CREATE TABLE IF NOT EXISTS short_positions (
                symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL CHECK(quantity>0),
                entry_price REAL NOT NULL, entry_fee REAL NOT NULL,
                reserved_capital REAL NOT NULL, entry_time REAL NOT NULL,
                intent_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trade_forward_observations (
                observation_id TEXT PRIMARY KEY, simulation_id TEXT NOT NULL,
                trade_id TEXT NOT NULL UNIQUE, execution_mode TEXT NOT NULL,
                evidence_source TEXT NOT NULL, strategy_family TEXT NOT NULL,
                strategy_version TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL,
                market_regime TEXT, stock_state TEXT, decision_id TEXT,
                intent_id TEXT NOT NULL, entry_time REAL NOT NULL,
                entry_reference_price REAL NOT NULL, entry_fill_price REAL NOT NULL,
                exit_time REAL NOT NULL, exit_reference_price REAL NOT NULL,
                exit_fill_price REAL NOT NULL, quantity INTEGER NOT NULL,
                stop_loss_price REAL, take_profit_price REAL, exit_reason TEXT NOT NULL,
                gross_pnl REAL NOT NULL, gross_return REAL NOT NULL,
                entry_costs REAL NOT NULL, exit_costs REAL NOT NULL,
                fees REAL NOT NULL, taxes REAL NOT NULL, slippage_cost REAL NOT NULL,
                total_costs REAL NOT NULL, net_pnl REAL NOT NULL, net_return REAL NOT NULL,
                account_equity_before REAL, account_equity_after REAL,
                benchmark_symbol TEXT, benchmark_entry_price REAL,
                benchmark_exit_price REAL, benchmark_return REAL,
                excess_return REAL, holding_duration REAL NOT NULL,
                code_hash TEXT, config_hash TEXT, cost_model_hash TEXT,
                universe_id TEXT, feed_provenance TEXT, created_at REAL NOT NULL,
                review_status TEXT NOT NULL, invalid_reason TEXT);
            CREATE INDEX IF NOT EXISTS trade_forward_lookup
                ON trade_forward_observations(strategy_family,strategy_version,evidence_source);
            CREATE TABLE IF NOT EXISTS trade_benchmark_evidence (
                observation_id TEXT PRIMARY KEY, benchmark_symbol TEXT NOT NULL,
                entry_price REAL, entry_timestamp REAL, entry_age_seconds REAL,
                exit_price REAL, exit_timestamp REAL, exit_age_seconds REAL,
                benchmark_return REAL, excess_return REAL, status TEXT NOT NULL,
                reason TEXT, created_at REAL NOT NULL);
        """)

    def event(self, kind: str, payload: dict, received: float | None = None) -> None:
        with self.db:
            self.db.execute("INSERT INTO events(received,kind,payload) VALUES(?,?,?)",
                            (time.time() if received is None else received, kind,
                             json.dumps(payload, allow_nan=False)))

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, json.dumps(value, allow_nan=False)))

    def halt(self, reason: str) -> None:
        with self.db:
            self.put("halt_reason", reason)
        self.event("halt", {"reason": reason})

    def close(self) -> None:
        self.db.close()
