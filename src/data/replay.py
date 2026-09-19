"""Read-only, deterministic FYERS event replay in persisted arrival order."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3


def read_events(path: Path) -> tuple[list[dict], str]:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("BEGIN")
        rows = db.execute("SELECT id, received, kind, payload FROM events ORDER BY id").fetchall()
        events = [{"id": i, "received": received, "kind": kind, "data": json.loads(payload)}
                  for i, received, kind, payload in rows]
        encoded = json.dumps(events, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return events, hashlib.sha256(encoded.encode()).hexdigest()
    finally:
        db.close()
