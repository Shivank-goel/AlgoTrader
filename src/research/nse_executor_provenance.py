"""Deterministic provenance for the historical research executor.

Only explicitly material implementation/configuration files are included;
tests, UI, documentation and repository metadata are intentionally excluded.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.fyers.models import ROOT

SCHEMA_VERSION = "nse-historical-executor-v1"
MATERIAL_FILES = (
    "src/strategies/nse_regime_selector.py",
    "src/fyers/daily_data.py",
    "src/research/nse_engine.py",
    "src/research/nse_trials.py",
    "config/fyers_costs.yaml",
    "config/fyers_economics.yaml",
)


def provenance(root: Path = ROOT) -> dict:
    files = []
    for relative in MATERIAL_FILES:
        path = root / relative
        if not path.is_file():
            raise ValueError(f"material executor file is missing: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({"path": relative, "sha256": digest})
    manifest = {"schema_version": SCHEMA_VERSION, "files": files}
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return {"historical_executor_hash": hashlib.sha256(encoded).hexdigest(), **manifest}


def historical_executor_hash(root: Path = ROOT) -> str:
    return str(provenance(root)["historical_executor_hash"])
