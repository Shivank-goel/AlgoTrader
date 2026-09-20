"""Immutable, bounded FYERS session exports for deterministic replay."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.data.quality import quote_quality
from src.fyers.models import Instrument

_IST = ZoneInfo("Asia/Kolkata")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


class SessionManifest(BaseModel):
    """Validated replay contract embedded in every exported session."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: int = Field(1, ge=1, le=1)
    start: float
    end: float
    timezone: str = "Asia/Kolkata"
    event_order: str = "journal_id"
    freshness_seconds: float = Field(gt=0)
    corporate_action_policy: str = "intraday_session_no_adjustment"
    master_event_id: int = Field(gt=0)
    master_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    events_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_window(self) -> SessionManifest:
        if self.end <= self.start:
            raise ValueError("Session end must be after start")
        if datetime.fromtimestamp(self.start, _IST).date() != datetime.fromtimestamp(self.end, _IST).date():
            raise ValueError("A session export must stay within one Asia/Kolkata date")
        return self


class SessionExport(BaseModel):
    """Portable event snapshot with independently verifiable provenance."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    manifest: SessionManifest
    instruments: dict[str, Instrument]
    events: list[dict]
    quality: dict

    @model_validator(mode="after")
    def evidence_matches(self) -> SessionExport:
        manifest = self.manifest
        if not self.instruments:
            raise ValueError("Session export requires an instrument master")
        instrument_rows = {symbol: row.model_dump(mode="json") for symbol, row in self.instruments.items()}
        if _digest(instrument_rows) != manifest.master_sha256:
            raise ValueError("Instrument-master hash mismatch")
        if _digest(self.events) != manifest.events_sha256:
            raise ValueError("Event hash mismatch")
        if _digest(self.quality) != manifest.quality_sha256:
            raise ValueError("Quality-report hash mismatch")
        ids = [event.get("id") for event in self.events]
        if any(not isinstance(value, int) for value in ids) or ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("Events must retain unique journal order")
        if any(not manifest.start <= float(event.get("received", -1)) <= manifest.end for event in self.events):
            raise ValueError("Event outside exported window")
        expected = quote_quality(
            self.events, list(self.instruments), start=manifest.start, end=manifest.end,
            stale_seconds=manifest.freshness_seconds,
        )
        if expected != self.quality:
            raise ValueError("Quality report does not reproduce from exported events")
        return self

    @property
    def session_sha256(self) -> str:
        return _digest(self.model_dump(mode="json"))


def export_session(source: Path, output: Path, *, start: float, end: float,
                   symbols: list[str], stale_seconds: float) -> SessionExport:
    """Capture one SQLite read snapshot and atomically publish its session artifact."""
    if output.exists():
        raise ValueError("Session export already exists")
    # Validate the window before touching the source database.
    base = {"schema_version": 1, "start": start, "end": end,
            "freshness_seconds": stale_seconds, "master_event_id": 1,
            "master_sha256": "0" * 64, "events_sha256": "0" * 64,
            "quality_sha256": "0" * 64}
    SessionManifest.model_validate(base)
    db = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("BEGIN")
        master_row = db.execute(
            "SELECT id,payload FROM events WHERE kind='instruments' AND received<=? ORDER BY id DESC LIMIT 1",
            (end,),
        ).fetchone()
        if master_row is None:
            raise ValueError("No instrument master exists in or before the session window")
        rows = db.execute(
            "SELECT id,received,kind,payload FROM events WHERE received>=? AND received<=? ORDER BY id",
            (start, end),
        ).fetchall()
    finally:
        db.close()
    master_raw = json.loads(master_row[1])
    instruments = {symbol: Instrument.model_validate(master_raw[symbol]) for symbol in symbols}
    events = [{"id": row[0], "received": row[1], "kind": row[2], "data": json.loads(row[3])} for row in rows]
    instrument_rows = {symbol: row.model_dump(mode="json") for symbol, row in instruments.items()}
    quality = quote_quality(events, symbols, start=start, end=end, stale_seconds=stale_seconds)
    manifest = SessionManifest(start=start, end=end, freshness_seconds=stale_seconds,
                               master_event_id=master_row[0], master_sha256=_digest(instrument_rows),
                               events_sha256=_digest(events), quality_sha256=_digest(quality))
    artifact = SessionExport(manifest=manifest, instruments=instruments, events=events, quality=quality)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(artifact.model_dump(mode="json"), indent=2, allow_nan=False))
            handle.flush()
            import os
            os.fsync(handle.fileno())
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return artifact


def load_session(path: Path) -> SessionExport:
    """Load and fully revalidate an exported session."""
    return SessionExport.model_validate_json(path.read_bytes())
