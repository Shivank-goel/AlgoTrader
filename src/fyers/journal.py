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
