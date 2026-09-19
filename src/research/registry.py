"""Append-only SQLite experiment evidence with transactional trial budgets.

SQL triggers guard accidental edits, not a malicious filesystem owner. Back up
the database with SQLite backup, retaining independent artifact hashes.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from src.research.models import ExperimentResult, ExperimentSpec


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def evidence_hash(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def verify_artifacts(root: Path, spec: ExperimentSpec) -> None:
    root = root.resolve()
    for relative, expected in spec.artifacts.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or path == root:
            raise ValueError("Artifact must be inside the research workspace")
        if any(p.startswith(".env") or p in {".git", ".ssh"} for p in path.relative_to(root).parts):
            raise ValueError("Secret or internal files cannot be research artifacts")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError(f"Artifact changed: {relative}")


class ExperimentRegistry:
    def __init__(self, path: Path):
        self.path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            self.db.close()
            raise ValueError("Unsupported research database version")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY, budget INTEGER NOT NULL CHECK(budget>0));
            CREATE TABLE IF NOT EXISTS experiments (
                id TEXT PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(id),
                spec TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, experiment TEXT NOT NULL REFERENCES experiments(id),
                kind TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL,
                UNIQUE(experiment,kind));
            CREATE TABLE IF NOT EXISTS legacy (
                digest TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        for table in ("campaigns", "experiments", "events", "legacy"):
            for operation in ("UPDATE", "DELETE"):
                self.db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_{operation.lower()}
                    BEFORE {operation} ON {table} BEGIN
                    SELECT RAISE(ABORT, 'Research records are immutable'); END""")
        self.db.commit()
        # Additive migration: no historical spec, event or imported snapshot changes.
        self.db.execute("PRAGMA user_version=2")

    @contextmanager
    def execution_lock(self, experiment_id: str):
        """Prevent resolving an interruption while its cooperating worker is alive."""
        spec = self.spec(experiment_id)
        lock_path = self.path.with_name(self.path.name + f".{spec.experiment_id}.run.lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Experiment worker is active; cannot run or resolve it") from None
            yield

    def campaign(self, campaign_id: str, budget: int) -> None:
        if not campaign_id or type(budget) is not int or budget < 1:
            raise ValueError("A named positive trial budget is required")
        with self.db:
            self.db.execute("INSERT INTO campaigns VALUES(?,?)", (campaign_id, budget))

    def register(self, campaign_id: str, spec: ExperimentSpec) -> None:
        spec = ExperimentSpec.model_validate(spec.model_dump(mode="json"))
        payload = canonical(spec.model_dump(mode="json"))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            campaign = self.db.execute("SELECT budget FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            count = self.db.execute("SELECT COUNT(*) FROM experiments WHERE campaign=?", (campaign_id,)).fetchone()[0]
            if not campaign or count >= campaign[0]:
                raise ValueError("Missing campaign or experiment budget exhausted")
            if spec.parent_experiment_id and not self.db.execute("SELECT 1 FROM experiments WHERE id=?", (spec.parent_experiment_id,)).fetchone():
                raise ValueError("Unknown parent experiment")
            self.db.execute("INSERT INTO experiments VALUES(?,?,?,?)", (spec.experiment_id, campaign_id, payload, self.now()))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def spec(self, experiment_id: str) -> ExperimentSpec:
        row = self.db.execute("SELECT spec FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        if not row:
            raise ValueError("Unknown experiment")
        return ExperimentSpec.model_validate_json(row[0])

    def event(self, experiment_id: str, kind: str, payload: dict) -> None:
        if not isinstance(payload, dict):
            raise ValueError("Experiment event payload must be an object")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            spec = self.spec(experiment_id)
            rows = self.db.execute("SELECT kind,payload FROM events WHERE experiment=? ORDER BY seq", (experiment_id,)).fetchall()
            kinds = [r[0] for r in rows]
            expected = ["started", "result", "published"]
            if kinds != expected[:len(kinds)] or len(kinds) >= 3 or kind != expected[len(kinds)]:
                raise ValueError("Invalid experiment transition; inspect research status")
            if kind == "started":
                if payload != {}:
                    raise ValueError("Started event must have an empty payload")
                campaign = self.db.execute("SELECT campaign FROM experiments WHERE id=?", (experiment_id,)).fetchone()[0]
                problems = [r for r in self.status(campaign) if r["experiment_id"] != experiment_id
                            and (r["state"] in {"unfinished", "unpublished", "inconsistent"})]
                if problems:
                    raise ValueError("Campaign has unresolved experiments; recover or resolve them first")
            elif kind == "result":
                result = ExperimentResult.model_validate(payload)
                result.validate_spec(spec)
                payload = result.model_dump(mode="json", exclude_none=True)
            else:
                result_payload = json.loads(rows[1][1])
                ExperimentResult.model_validate(result_payload).validate_spec(spec)
                self._validate_publication(spec, result_payload, payload)
            self.db.execute("INSERT INTO events(experiment,kind,payload,created) VALUES(?,?,?,?)",
                            (experiment_id, kind, canonical(payload), self.now()))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @staticmethod
    def _validate_publication(spec: ExperimentSpec, result: dict, payload: dict) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("evidence"), dict):
            raise ValueError("Publication requires evidence bindings")
        binding = payload["evidence"]
        if binding.get("spec_sha256") != evidence_hash(spec.model_dump(mode="json")) or binding.get("result_sha256") != evidence_hash(result):
            raise ValueError("Publication does not match the frozen evidence")
        digest = binding.get("trials_sha256", "")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Publication requires a trial snapshot hash")
        when = binding.get("published_at")
        if not isinstance(when, str) or datetime.fromisoformat(when).utcoffset() is None:
            raise ValueError("Publication requires a timezone-aware date")
        if payload.get("qualification") is not False or type(payload.get("full_trial_count")) is not int or payload["full_trial_count"] < 1:
            raise ValueError("Publication requires a counted non-deployment report")

    def evidence(self, experiment_id: str, kind: str) -> dict | None:
        row = self.db.execute("SELECT payload FROM events WHERE experiment=? AND kind=?", (experiment_id, kind)).fetchone()
        return json.loads(row[0]) if row else None

    def import_legacy(self, path: Path) -> int:
        from src.backtest.statistics import TrialsRegistry
        # Validate using the existing compatibility registry. Preserve the raw
        # snapshot, including placeholder trials; do not invent missing metadata.
        raw = path.read_bytes()
        payload = TrialsRegistry.validate_snapshot(raw)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO legacy VALUES(?,?)", (hashlib.sha256(raw).hexdigest(), canonical(payload)))
        return len(payload["trials"])

    def status(self, campaign_id: str | None = None) -> list[dict]:
        """Audit existing evidence without rewriting or silently repairing it."""
        rows = self.db.execute("SELECT id,campaign,spec FROM experiments WHERE (? IS NULL OR campaign=?) ORDER BY id",
                               (campaign_id, campaign_id)).fetchall()
        results = []
        for experiment_id, campaign, encoded in rows:
            problems = []
            events = self.db.execute("SELECT kind,payload FROM events WHERE experiment=? ORDER BY seq", (experiment_id,)).fetchall()
            kinds = [e[0] for e in events]
            if kinds != ["started", "result", "published"][:len(kinds)] or len(kinds) > 3:
                problems.append("Invalid event sequence")
            try:
                spec = ExperimentSpec.model_validate_json(encoded)
                payloads = {k: json.loads(p) for k, p in events}
                if "started" in payloads and payloads["started"] != {}:
                    problems.append("Invalid started payload")
                if "result" in payloads:
                    ExperimentResult.model_validate(payloads["result"]).validate_spec(spec)
                publication = payloads.get("published", {})
                if "published" in payloads and (not isinstance(publication, dict)
                        or publication.get("qualification") is not False
                        or type(publication.get("full_trial_count")) is not int
                        or publication["full_trial_count"] < 1):
                    raise ValueError("Invalid historical publication")
                binding = publication.get("evidence")
                if binding is not None:
                    self._validate_publication(spec, payloads.get("result", {}), publication)
            except (ValueError, TypeError, AttributeError, KeyError):
                problems.append("Invalid stored evidence; review required")
                binding = None
            state = {0: "registered", 1: "unfinished", 2: "unpublished", 3: "published"}.get(len(kinds), "inconsistent")
            results.append({"experiment_id": experiment_id, "campaign": campaign,
                            "state": "inconsistent" if problems else state, "issues": problems,
                            "publication_bound": bool(binding) if "published" in kinds else False})
        return results

    def report_view(self, experiment_id: str, trials_path: Path) -> dict:
        """Annotate immutable reports with current freshness without recalculating them."""
        self.spec(experiment_id)
        publication = self.evidence(experiment_id, "published")
        binding = publication.get("evidence", {}) if isinstance(publication, dict) else {}
        if not isinstance(binding, dict):
            binding = {}
        current = None
        try:
            from src.backtest.statistics import TrialsRegistry
            raw = trials_path.read_bytes()
            TrialsRegistry.validate_snapshot(raw)
            current = hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError):
            pass
        status = "unpublished" if publication is None else "unverifiable"
        audit = next(r for r in self.status() if r["experiment_id"] == experiment_id)
        if binding.get("trials_sha256") and current and not audit["issues"]:
            status = "current" if binding["trials_sha256"] == current else "stale"
        return {"experiment_id": experiment_id, "freshness": status, "current_trials_sha256": current,
                "publication": publication, "issues": audit["issues"]}

    def close(self) -> None:
        self.db.close()
