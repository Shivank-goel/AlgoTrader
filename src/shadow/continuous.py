"""Continuous, observation-only forward strategy evaluation on FYERS quotes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import time
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.fyers.costs import FyersCosts
from src.fyers.daily_data import completed_bar_panel
from src.fyers.data_readiness import daily_data_readiness
from src.fyers.journal import Journal
from src.fyers.models import ROOT, RuntimeConfig, Side
from src.fyers.sessions import NseSessionCalendar
from src.fyers.universe import ForwardUniverse
from src.strategies.nse_regime_selector import RegimeAwareSelector, SelectorConfig, StrategyFamily


class CandidateKind(str, Enum):
    MOMENTUM = "momentum"
    REVERSAL = "reversal"


class StrategyCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    kind: CandidateKind
    symbols: list[str] = Field(min_length=2, max_length=50)
    interval_seconds: int = Field(ge=60, le=86400)
    lookback_intervals: int = Field(ge=2, le=1000)
    horizon_intervals: int = Field(ge=1, le=1000)
    top_n: int = Field(ge=1, le=25)
    capital_inr: float = Field(gt=0)

    @model_validator(mode="after")
    def valid_universe(self) -> StrategyCandidate:
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("candidate symbols must be unique")
        if self.top_n >= len(self.symbols):
            raise ValueError("top_n must be smaller than the candidate universe")
        return self


class StrategyLabConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    candidates: list[StrategyCandidate] = Field(default_factory=list, max_length=50)
    universe_file: str | None = None
    regime_selector: SelectorConfig | None = None
    forward_holding_sessions: int = Field(default=5, ge=1, le=252)

    @model_validator(mode="after")
    def unique_candidates(self) -> StrategyLabConfig:
        identifiers = [candidate.id for candidate in self.candidates]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("candidate IDs must be unique")
        return self

    @classmethod
    def load(cls, path: Path) -> StrategyLabConfig:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class ContinuousStrategyLab:
    """Collect forward evidence without creating intents or calling a broker."""

    def __init__(self, runtime: RuntimeConfig | None = None) -> None:
        self.runtime = runtime or RuntimeConfig.load()
        self.config = StrategyLabConfig.load(ROOT / self.runtime.strategy_lab_file)
        self.universe = (ForwardUniverse.load(ROOT / self.config.universe_file)
                         if self.config.universe_file else None)
        self.selector = (RegimeAwareSelector(self.config.regime_selector)
                         if self.config.regime_selector else None)
        configured = set(self.runtime.symbols)
        if any(not set(candidate.symbols) <= configured for candidate in self.config.candidates):
            raise ValueError("strategy candidate uses a symbol outside the recorder universe")
        if self.selector is not None and self.universe is None:
            raise ValueError("regime selector requires a versioned forward universe")
        if self.universe is not None and {row.symbol for row in self.universe.members} != configured:
            raise ValueError("runtime symbols must exactly match the versioned forward universe")
        self.costs = FyersCosts.load()

    @staticmethod
    def _tables(journal: Journal) -> None:
        with journal.db:
            journal.db.executescript("""
                CREATE TABLE IF NOT EXISTS strategy_snapshots (
                    candidate_id TEXT NOT NULL, bucket REAL NOT NULL,
                    candidate_sha256 TEXT NOT NULL, prices TEXT NOT NULL,
                    PRIMARY KEY(candidate_id,bucket));
                CREATE TABLE IF NOT EXISTS strategy_observations (
                    candidate_id TEXT NOT NULL, decision_at REAL NOT NULL,
                    candidate_sha256 TEXT NOT NULL, due_at REAL NOT NULL,
                    kind TEXT NOT NULL, selected TEXT NOT NULL,
                    entry_prices TEXT NOT NULL, benchmark_prices TEXT NOT NULL,
                    evaluated_at REAL, outcome TEXT, gross_return REAL, net_return REAL,
                    benchmark_return REAL, excess_return REAL,
                    PRIMARY KEY(candidate_id,decision_at));
                CREATE TABLE IF NOT EXISTS regime_decisions (
                    decision_day TEXT PRIMARY KEY, selector_sha256 TEXT NOT NULL,
                    universe_id TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS regime_family_observations (
                    family TEXT NOT NULL, decision_day TEXT NOT NULL, due_at REAL NOT NULL,
                    targets TEXT NOT NULL, entry_prices TEXT NOT NULL, benchmark_prices TEXT NOT NULL,
                    evaluated_at REAL, net_return REAL, benchmark_return REAL, excess_return REAL,
                    PRIMARY KEY(family,decision_day));
                CREATE TABLE IF NOT EXISTS regime_forward_observations_v2 (
                    family TEXT NOT NULL, decision_day TEXT NOT NULL,
                    selector_sha256 TEXT NOT NULL, decision_bar_sha256 TEXT NOT NULL,
                    entry_day TEXT NOT NULL, due_day TEXT NOT NULL, targets TEXT NOT NULL,
                    entry_prices TEXT, benchmark_entry REAL, evaluated_day TEXT,
                    outcome TEXT, net_return REAL, benchmark_return REAL, excess_return REAL,
                    PRIMARY KEY(family,decision_day));
            """)

    def _evaluate_regime_families(self, journal: Journal, closes: pd.DataFrame,
                                  opens: pd.DataFrame, benchmark: pd.Series,
                                  artifacts: list) -> None:
        if not artifacts:
            return
        available = {artifact.session_date.isoformat(): index
                     for index, artifact in enumerate(artifacts)}
        latest = artifacts[-1].session_date.isoformat()
        rows = journal.db.execute(
            "SELECT * FROM regime_forward_observations_v2 WHERE outcome IS NULL "
            "ORDER BY decision_day"
        ).fetchall()
        for row in rows:
            targets = json.loads(row["targets"])
            entry = json.loads(row["entry_prices"]) if row["entry_prices"] else None
            if entry is None and row["entry_day"] in available:
                index = available[row["entry_day"]]
                prices = opens.iloc[index]
                if any(pd.isna(prices.get(symbol)) for symbol in targets) or pd.isna(benchmark.iloc[index]):
                    with journal.db:
                        journal.db.execute(
                            "UPDATE regime_forward_observations_v2 SET outcome='ENTRY_MISSING' "
                            "WHERE family=? AND decision_day=?", (row["family"], row["decision_day"]),
                        )
                    continue
                entry = {symbol: float(prices[symbol]) * (1 + self.runtime.slippage_bps / 10000)
                         for symbol in targets}
                with journal.db:
                    journal.db.execute(
                        "UPDATE regime_forward_observations_v2 SET entry_prices=?,benchmark_entry=? "
                        "WHERE family=? AND decision_day=?",
                        (json.dumps(entry, sort_keys=True), float(benchmark.iloc[index]),
                         row["family"], row["decision_day"]),
                    )
            elif entry is None and latest > row["entry_day"]:
                with journal.db:
                    journal.db.execute(
                        "UPDATE regime_forward_observations_v2 SET outcome='ENTRY_SESSION_MISSING' "
                        "WHERE family=? AND decision_day=?", (row["family"], row["decision_day"]),
                    )
                continue
            if entry is None or latest < row["due_day"]:
                continue
            if row["due_day"] not in available:
                with journal.db:
                    journal.db.execute(
                        "UPDATE regime_forward_observations_v2 SET outcome='EXIT_SESSION_MISSING' "
                        "WHERE family=? AND decision_day=?", (row["family"], row["decision_day"]),
                    )
                continue
            index = available[row["due_day"]]
            exits = closes.iloc[index]
            if any(pd.isna(exits.get(symbol)) for symbol in targets) or pd.isna(benchmark.iloc[index]):
                outcome = "EXIT_MISSING"
                with journal.db:
                    journal.db.execute(
                        "UPDATE regime_forward_observations_v2 SET outcome=? WHERE family=? AND decision_day=?",
                        (outcome, row["family"], row["decision_day"]),
                    )
                continue
            capital = self.config.regime_selector.capital_inr
            entry_cost = 0.0
            exit_value = 0.0
            fees = 0.0
            for symbol, quantity in targets.items():
                buy_notional = quantity * entry[symbol]
                exit_price = float(exits[symbol]) * (1 - self.runtime.slippage_bps / 10000)
                sell_notional = quantity * exit_price
                entry_cost += buy_notional
                exit_value += sell_notional
                fees += self.costs.fee(buy_notional, Side.BUY, delivery=True)
                fees += self.costs.fee(sell_notional, Side.SELL, delivery=True, charge_dp=True)
            net = (capital - entry_cost + exit_value - fees) / capital - 1
            baseline = float(benchmark.iloc[index]) / float(row["benchmark_entry"]) - 1
            with journal.db:
                journal.db.execute(
                    "UPDATE regime_forward_observations_v2 SET evaluated_day=?,outcome='EVALUATED',"
                    "net_return=?,benchmark_return=?,excess_return=? WHERE family=? AND decision_day=?",
                    (row["due_day"], net, baseline, net - baseline,
                     row["family"], row["decision_day"]),
                )

    @staticmethod
    def _family_metrics(journal: Journal) -> dict:
        result = {}
        families = journal.db.execute(
            "SELECT DISTINCT family FROM regime_forward_observations_v2 ORDER BY family").fetchall()
        for family_row in families:
            family = family_row[0]
            rows = journal.db.execute(
                "SELECT net_return,benchmark_return,excess_return FROM regime_forward_observations_v2 "
                "WHERE family=? AND outcome='EVALUATED' ORDER BY decision_day", (family,)).fetchall()
            values = [row["net_return"] for row in rows]
            pending = journal.db.execute(
                "SELECT COUNT(*) FROM regime_forward_observations_v2 "
                "WHERE family=? AND outcome IS NULL", (family,)).fetchone()[0]
            rejected = journal.db.execute(
                "SELECT COUNT(*) FROM regime_forward_observations_v2 "
                "WHERE family=? AND outcome IS NOT NULL AND outcome!='EVALUATED'", (family,)).fetchone()[0]
            equity = benchmark_equity = peak = 1.0
            drawdown = 0.0
            for row in rows:
                equity *= 1 + row["net_return"]
                benchmark_equity *= 1 + row["benchmark_return"]
                peak = max(peak, equity)
                drawdown = max(drawdown, 1 - equity / peak)
            result[family] = {"completed": len(values), "pending": pending,
                              "rejected": rejected,
                              "net_return": equity - 1 if rows else None,
                              "benchmark_return": benchmark_equity - 1 if rows else None,
                              "excess_return": equity - benchmark_equity if rows else None,
                              "max_drawdown": drawdown if rows else None,
                              "mean_net_return": sum(values) / len(values) if values else None,
                              "win_rate": sum(value > 0 for value in values) / len(values) if values else None}
        return result

    def _record_family_previews(self, journal: Journal, previews: list[dict], day: str,
                                selector_sha256: str, bar_sha256: str) -> None:
        calendar = NseSessionCalendar(ROOT / self.runtime.holidays_file,
                                      self.runtime.session_start, self.runtime.session_end)
        decision_day = datetime.fromisoformat(day).date()
        entry_day = calendar.trading_day_offset(decision_day, 1)
        due_day = calendar.trading_day_offset(
            entry_day, self.config.forward_holding_sessions - 1,
        )
        for preview in previews:
            family, targets = preview["family"], preview["hypothetical_targets"]
            if not targets or journal.db.execute(
                "SELECT 1 FROM regime_forward_observations_v2 WHERE family=? AND outcome IS NULL",
                (family,),
            ).fetchone():
                continue
            with journal.db:
                journal.db.execute(
                    "INSERT OR IGNORE INTO regime_forward_observations_v2 "
                    "(family,decision_day,selector_sha256,decision_bar_sha256,entry_day,due_day,targets) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (family, day, selector_sha256, bar_sha256, entry_day.isoformat(),
                     due_day.isoformat(), json.dumps(targets, sort_keys=True)),
                )

    def _selector_status(self, journal: Journal, now: float) -> dict:
        if self.selector is None:
            return {"state": "disabled", "selected_family": None}
        assert self.universe is not None
        symbols = [row.symbol for row in self.universe.members]
        data_readiness = daily_data_readiness(
            self.runtime, directory=ROOT / self.runtime.daily_bars_directory,
            universe=self.universe, now=now,
        )
        if not data_readiness["data_ready"]:
            return {"state": "waiting_for_data_readiness", "selected_family": None,
                    "reason": ",".join(data_readiness["reasons"]),
                    "data_readiness": data_readiness}
        panel, opens, benchmark, artifacts = completed_bar_panel(
            ROOT / self.runtime.daily_bars_directory, symbols,
        )
        if not artifacts:
            return {"state": "waiting_for_completed_bars", "selected_family": None,
                    "reason": "run fyers sync-daily after the market closes",
                    "data_readiness": data_readiness}
        self._evaluate_regime_families(journal, panel, opens, benchmark, artifacts)
        decision = self.selector.select(panel, benchmark)
        data_manifest_sha256 = hashlib.sha256(json.dumps(
            [(row.session_date.isoformat(), row.content_sha256) for row in artifacts],
            separators=(",", ":"),
        ).encode()).hexdigest()
        previews = []
        for name in decision["eligible_families"]:
            family = StrategyFamily(name)
            scores = self.selector.scores(family, panel)
            targets, rejected = self.selector.whole_share_targets(scores, panel.iloc[-1])
            previews.append({"family": name, "scores": scores.head(10).to_dict(),
                             "hypothetical_targets": targets, "rejected": rejected})
        payload = {**decision, "state": "observation_only", "universe_id": self.universe.universe_id,
                   "warmup_bars": len(panel), "required_warmup_bars": 253,
                   "last_completed_bar": artifacts[-1].session_date.isoformat(),
                   "bar_data_sha256": data_manifest_sha256,
                   "data_readiness": data_readiness,
                   "missing_symbols": artifacts[-1].missing_symbols,
                   "family_previews": previews, "execution": "observation_only"}
        assert self.config.regime_selector is not None
        identity = hashlib.sha256(self.config.regime_selector.model_dump_json().encode()).hexdigest()
        identity = hashlib.sha256(
            (identity + self.universe.source_sha256).encode()
        ).hexdigest()
        day = artifacts[-1].session_date.isoformat()
        old = journal.db.execute("SELECT selector_sha256,payload FROM regime_decisions WHERE decision_day=?",
                                 (day,)).fetchone()
        if old and old["selector_sha256"] != identity:
            raise ValueError("regime decision identity conflict")
        if old:
            current = json.loads(old["payload"])
            current["family_metrics"] = self._family_metrics(journal)
            current["paper_schedule"] = self._maybe_schedule_latest(journal, current, now)
            return current
        self._record_family_previews(
            journal, previews, day, identity, data_manifest_sha256,
        )
        payload["family_metrics"] = self._family_metrics(journal)
        payload["paper_schedule"] = self._maybe_schedule_latest(journal, payload, now)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with journal.db:
            journal.db.execute("INSERT INTO regime_decisions VALUES(?,?,?,?,?)",
                               (day, identity, self.universe.universe_id, encoded, now))
            journal.db.execute("INSERT INTO events(received,kind,payload) VALUES(?,?,?)",
                               (now, "regime_decision", encoded))
        return payload

    def _maybe_schedule_latest(self, journal: Journal, decision: dict, now: float) -> dict:
        if not decision.get("selected_family"):
            return {"status": "blocked", "reason": decision.get("reason")}
        if not self._market_open(journal, now):
            return {"status": "waiting_for_next_session"}
        calendar = NseSessionCalendar(ROOT / self.runtime.holidays_file,
                                      self.runtime.session_start, self.runtime.session_end)
        expected = calendar.trading_day_offset(
            datetime.fromisoformat(decision["last_completed_bar"]).date(), 1,
        )
        today = pd.Timestamp.fromtimestamp(now, tz="Asia/Kolkata").date()
        if today != expected:
            return {"status": "missed_entry_session", "expected": expected.isoformat()}
        selected = next(
            row for row in decision.get("family_previews", [])
            if row["family"] == decision["selected_family"]
        )
        return self._schedule_paper(
            journal, selected["hypothetical_targets"], decision["selected_family"], now,
        )

    def _schedule_paper(self, journal: Journal, targets: dict[str, int],
                        strategy: str, now: float) -> dict:
        """Existing qualification gate is authoritative; this never fills or submits."""
        from src.fyers.models import Instrument
        from src.shadow.scheduler import QualifiedIntentScheduler

        row = journal.db.execute(
            "SELECT payload FROM events WHERE kind='instruments' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"status": "blocked", "reason": "instrument_master_unavailable"}
        try:
            instruments = {symbol: Instrument.model_validate(value)
                           for symbol, value in json.loads(row["payload"]).items()}
            current = {item["symbol"]: item["quantity"] for item in journal.db.execute(
                "SELECT symbol,quantity FROM positions WHERE quantity>0")}
            intents = QualifiedIntentScheduler(journal, self.runtime, instruments, strategy).schedule(
                targets, current, at=datetime.fromtimestamp(now, UTC))
            return {"status": "qualified_paper", "intent_count": len(intents)}
        except (KeyError, TypeError, ValueError):
            return {"status": "blocked", "reason": "qualification_or_risk_gate"}

    @staticmethod
    def _identity(candidate: StrategyCandidate) -> str:
        return hashlib.sha256(candidate.model_dump_json().encode()).hexdigest()

    def _check_identity(self, journal: Journal, candidate: StrategyCandidate) -> str:
        identity = self._identity(candidate)
        hashes = set()
        for table in ("strategy_snapshots", "strategy_observations"):
            hashes.update(row[0] for row in journal.db.execute(
                f"SELECT DISTINCT candidate_sha256 FROM {table} WHERE candidate_id=?",
                (candidate.id,),
            ))
        if hashes and hashes != {identity}:
            raise ValueError(f"candidate ID {candidate.id} was reused with changed parameters")
        return identity

    def _quotes(self, journal: Journal, symbols: list[str], now: float) -> dict[str, dict]:
        latest = {}
        for row in journal.db.execute(
            "SELECT received,payload FROM events WHERE kind='tick' ORDER BY id DESC LIMIT 2000"
        ):
            try:
                data = json.loads(row["payload"])
                symbol = data.get("symbol")
                if symbol in symbols and symbol not in latest:
                    bid, ask = float(data["bid_price"]), float(data["ask_price"])
                    if bid > 0 and ask >= bid and now - float(row["received"]) <= self.runtime.stale_seconds:
                        latest[symbol] = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return latest

    def _market_open(self, journal: Journal, now: float) -> bool:
        account = journal.get("account_check", {})
        return (isinstance(account, dict) and account.get("market_open") is True
                and isinstance(account.get("at"), int | float)
                and 0 <= now - account["at"] <= self.runtime.reconcile_seconds * 2)

    def _evaluate(self, journal: Journal, candidate: StrategyCandidate,
                  quotes: dict[str, dict], now: float) -> int:
        completed = 0
        rows = journal.db.execute(
            "SELECT * FROM strategy_observations WHERE candidate_id=? "
            "AND evaluated_at IS NULL AND due_at<=? ORDER BY decision_at",
            (candidate.id, now),
        ).fetchall()
        for row in rows:
            if now > row["due_at"] + candidate.interval_seconds:
                with journal.db:
                    journal.db.execute(
                        "UPDATE strategy_observations SET evaluated_at=?,outcome='EXPIRED' "
                        "WHERE candidate_id=? AND decision_at=?",
                        (now, candidate.id, row["decision_at"]),
                    )
                    journal.db.execute(
                        "INSERT INTO events(received,kind,payload) VALUES(?,?,?)",
                        (now, "strategy_expired", json.dumps({"candidate_id": candidate.id,
                         "decision_at": row["decision_at"], "due_at": row["due_at"]})),
                    )
                continue
            selected = json.loads(row["selected"])
            entry = json.loads(row["entry_prices"])
            benchmark = json.loads(row["benchmark_prices"])
            if any(symbol not in quotes for symbol in set(selected) | set(benchmark)):
                continue
            gross = sum(quotes[symbol]["bid"] / entry[symbol] - 1 for symbol in selected) / len(selected)
            per_symbol = candidate.capital_inr / len(selected)
            fees = sum(
                self.costs.fee(per_symbol, Side.BUY, delivery=True)
                + self.costs.fee(per_symbol * quotes[symbol]["bid"] / entry[symbol], Side.SELL,
                                 delivery=True, charge_dp=True)
                for symbol in selected
            )
            net = gross - fees / candidate.capital_inr
            baseline = sum(quotes[symbol]["mid"] / benchmark[symbol] - 1
                           for symbol in benchmark) / len(benchmark)
            with journal.db:
                journal.db.execute(
                    "UPDATE strategy_observations SET evaluated_at=?,outcome='EVALUATED',gross_return=?,net_return=?,"
                    "benchmark_return=?,excess_return=? WHERE candidate_id=? AND decision_at=?",
                    (now, gross, net, baseline, net - baseline, candidate.id, row["decision_at"]),
                )
                journal.db.execute(
                    "INSERT INTO events(received,kind,payload) VALUES(?,?,?)",
                    (now, "strategy_evaluated", json.dumps({"candidate_id": candidate.id,
                     "decision_at": row["decision_at"], "net_return": net,
                     "benchmark_return": baseline, "excess_return": net - baseline})),
                )
            completed += 1
        return completed

    def _decide(self, journal: Journal, candidate: StrategyCandidate,
                quotes: dict[str, dict], bucket: float) -> bool:
        identity = self._check_identity(journal, candidate)
        encoded = json.dumps(quotes, sort_keys=True, separators=(",", ":"))
        with journal.db:
            journal.db.execute("INSERT OR IGNORE INTO strategy_snapshots VALUES(?,?,?,?)",
                               (candidate.id, bucket, identity, encoded))
        if journal.db.execute(
            "SELECT 1 FROM strategy_observations WHERE candidate_id=? AND evaluated_at IS NULL",
            (candidate.id,),
        ).fetchone():
            return False
        old_row = journal.db.execute(
            "SELECT prices FROM strategy_snapshots WHERE candidate_id=? AND bucket=?",
            (candidate.id, bucket - candidate.lookback_intervals * candidate.interval_seconds),
        ).fetchone()
        if old_row is None:
            return False
        old = json.loads(old_row["prices"])
        if any(symbol not in old for symbol in candidate.symbols):
            return False
        scores = {symbol: quotes[symbol]["mid"] / old[symbol]["mid"] - 1
                  for symbol in candidate.symbols}
        reverse = candidate.kind is CandidateKind.MOMENTUM
        selected = sorted(scores, key=scores.get, reverse=reverse)[:candidate.top_n]
        entry = {symbol: quotes[symbol]["ask"] for symbol in selected}
        benchmark = {symbol: quotes[symbol]["mid"] for symbol in candidate.symbols}
        due = bucket + candidate.horizon_intervals * candidate.interval_seconds
        with journal.db:
            cursor = journal.db.execute(
                "INSERT OR IGNORE INTO strategy_observations "
                "(candidate_id,decision_at,candidate_sha256,due_at,kind,selected,entry_prices,benchmark_prices) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (candidate.id, bucket, identity, due, candidate.kind.value, json.dumps(selected),
                 json.dumps(entry, sort_keys=True), json.dumps(benchmark, sort_keys=True)),
            )
            if cursor.rowcount:
                journal.db.execute(
                    "INSERT INTO events(received,kind,payload) VALUES(?,?,?)",
                    (bucket, "strategy_decision", json.dumps({"candidate_id": candidate.id,
                     "kind": candidate.kind.value, "selected": selected, "scores": scores,
                     "due_at": due, "execution": "observation_only"})),
                )
                return True
        return False

    @staticmethod
    def _metrics(journal: Journal, candidate_id: str) -> dict:
        rows = journal.db.execute(
            "SELECT net_return,benchmark_return,excess_return FROM strategy_observations "
            "WHERE candidate_id=? AND outcome='EVALUATED' ORDER BY decision_at",
            (candidate_id,),
        ).fetchall()
        pending = journal.db.execute(
            "SELECT COUNT(*) FROM strategy_observations WHERE candidate_id=? AND evaluated_at IS NULL",
            (candidate_id,),
        ).fetchone()[0]
        expired = journal.db.execute(
            "SELECT COUNT(*) FROM strategy_observations WHERE candidate_id=? AND outcome='EXPIRED'",
            (candidate_id,),
        ).fetchone()[0]
        nets = [row["net_return"] for row in rows]
        equity, peak, drawdown = 1.0, 1.0, 0.0
        for value in nets:
            equity *= 1 + value
            peak = max(peak, equity)
            drawdown = max(drawdown, 1 - equity / peak)
        return {"completed": len(rows), "expired": expired, "pending": pending,
                "net_return": equity - 1 if rows else None,
                "mean_net_return": sum(nets) / len(nets) if nets else None,
                "win_rate": sum(value > 0 for value in nets) / len(nets) if nets else None,
                "mean_excess_return": (sum(row["excess_return"] for row in rows) / len(rows)
                                       if rows else None),
                "max_drawdown": drawdown if rows else None}

    def run_once(self, *, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        if not math.isfinite(now) or now <= 0:
            raise ValueError("strategy lab clock must be positive and finite")
        journal = Journal(ROOT / self.runtime.database)
        try:
            self._tables(journal)
            status = {"enabled": self.config.enabled, "as_of": now,
                      "execution": "observation_only", "live_enabled": False,
                      "market_open": self._market_open(journal, now), "candidates": []}
            if not self.config.enabled:
                status["reason"] = "strategy lab disabled"
            elif not status["market_open"]:
                status["reason"] = "waiting for a fresh FYERS NSE OPEN status"
            status["selector"] = (self._selector_status(journal, now)
                                  if self.config.enabled
                                  else {"state": "disabled", "selected_family": None})
            for candidate in self.config.candidates:
                quotes = self._quotes(journal, candidate.symbols, now)
                item = {"id": candidate.id, "kind": candidate.kind.value,
                        "metrics": self._metrics(journal, candidate.id)}
                if not self.config.enabled or not status["market_open"]:
                    item["state"] = "waiting_for_market"
                elif set(quotes) != set(candidate.symbols):
                    item["state"] = "waiting_for_fresh_two_sided_quotes"
                else:
                    bucket = now - now % candidate.interval_seconds
                    item["evaluated"] = self._evaluate(journal, candidate, quotes, now)
                    item["decision_created"] = self._decide(journal, candidate, quotes, bucket)
                    item["metrics"] = self._metrics(journal, candidate.id)
                    item["state"] = "collecting" if not item["decision_created"] else "decision_recorded"
                status["candidates"].append(item)
            with journal.db:
                journal.put("strategy_lab_status", status)
            return status
        finally:
            journal.close()

    async def schedule(self) -> None:
        while True:
            try:
                self.run_once()
            except (OSError, sqlite3.Error, ValueError) as exc:
                # Never crash monitoring or reveal exception payloads from external data.
                journal = Journal(ROOT / self.runtime.database)
                try:
                    with journal.db:
                        journal.put("strategy_lab_status", {"enabled": self.config.enabled,
                                    "as_of": time.time(), "execution": "observation_only",
                                    "live_enabled": False,
                                    "reason": f"strategy lab unavailable: {type(exc).__name__}"})
                finally:
                    journal.close()
            await asyncio.sleep(self.runtime.strategy_poll_seconds)
