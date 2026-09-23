"""Deterministic validation and provenance for completed FYERS daily-bar datasets."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from src.fyers.daily_data import CompletedBarArtifact, load_completed_bar
from src.fyers.models import ROOT, RuntimeConfig
from src.fyers.sessions import IST
from src.fyers.universe import ForwardUniverse

WARMUP_BARS = 253


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _load_artifacts(directory: Path) -> tuple[list[CompletedBarArtifact], list[str]]:
    artifacts: list[CompletedBarArtifact] = []
    failures: list[str] = []
    for path in sorted(directory.glob("????-??-??.json")):
        try:
            artifacts.append(load_completed_bar(path))
        except (OSError, ValueError):
            failures.append(path.name)
    return artifacts, failures


def daily_data_readiness(
    config: RuntimeConfig,
    *,
    directory: Path | None = None,
    universe: ForwardUniverse | None = None,
    now: float | None = None,
    warmup_bars: int = WARMUP_BARS,
) -> dict:
    """Return the immutable-data gate used before regime selection.

    This deliberately gates on completed daily bars, not on an intraday quote:
    after-close decisions must remain replayable when the stream is stopped.
    """
    if warmup_bars < 1:
        raise ValueError("warmup_bars must be positive")
    directory = directory or ROOT / config.daily_bars_directory
    universe = universe or ForwardUniverse.load(ROOT / "config/nse_forward_universe.yaml")
    symbols = [member.symbol for member in universe.members]
    now = time.time() if now is None else now
    artifacts, corrupt = _load_artifacts(directory)
    reasons: list[str] = []
    if corrupt:
        reasons.append("data_quality_failure")
    if not artifacts:
        reasons.append("missing_history")
    dates = [artifact.session_date.isoformat() for artifact in artifacts]
    if dates != sorted(set(dates)):
        reasons.append("timestamp_mismatch")
    expected_universe_hash = hashlib.sha256((ROOT / "config/nse_forward_universe.yaml").read_bytes()).hexdigest()
    mismatched_artifacts = [artifact.session_date.isoformat() for artifact in artifacts
                            if artifact.universe_sha256 != expected_universe_hash]
    if mismatched_artifacts:
        reasons.append("symbol_coverage_failure")
    malformed_symbol_dates = [artifact.session_date.isoformat() for artifact in artifacts
                              if (set(artifact.bars) - set(symbols)
                                  or set(artifact.missing_symbols) - set(symbols))]
    if malformed_symbol_dates:
        reasons.append("data_quality_failure")
    benchmark_mismatches = [artifact.session_date.isoformat() for artifact in artifacts
                            if artifact.benchmark_symbol != config.regime_symbol]
    benchmark_missing = [artifact.session_date.isoformat() for artifact in artifacts
                         if artifact.benchmark is None]
    if benchmark_mismatches or benchmark_missing:
        reasons.append("benchmark_missing")
    per_symbol: dict[str, dict] = {}
    for symbol in symbols:
        present = [artifact.session_date.isoformat() for artifact in artifacts if symbol in artifact.bars]
        missing = [artifact.session_date.isoformat() for artifact in artifacts if symbol not in artifact.bars]
        per_symbol[symbol] = {"completed_bars": len(present), "missing_dates": missing}
    missing_symbols = [symbol for symbol, value in per_symbol.items() if value["missing_dates"]]
    minimum_bars = min((value["completed_bars"] for value in per_symbol.values()), default=0)
    if missing_symbols:
        reasons.append("symbol_coverage_failure")
    if minimum_bars < warmup_bars or len(artifacts) < warmup_bars:
        reasons.append("insufficient_bars")
    # An artifact dated today before the configured close could only be an incomplete bar.
    local_now = datetime.fromtimestamp(now, IST)
    if artifacts and artifacts[-1].session_date >= local_now.date() and local_now.strftime("%H:%M") <= config.session_end:
        reasons.append("incomplete_session")
    manifest_rows = [(artifact.session_date.isoformat(), artifact.content_sha256) for artifact in artifacts]
    dataset_sha256 = _hash({"schema_version": 1, "universe_sha256": expected_universe_hash,
                            "benchmark": config.regime_symbol, "artifacts": manifest_rows})
    return {
        "data_ready": not reasons,
        "state": "DATA_READY" if not reasons else "DATA_NOT_READY",
        "reasons": list(dict.fromkeys(reasons)),
        "warmup_required": warmup_bars,
        "artifact_count": len(artifacts),
        "minimum_completed_bars": minimum_bars,
        "benchmark": {"symbol": config.regime_symbol, "completed_bars": len(artifacts) - len(benchmark_missing),
                      "missing_dates": benchmark_missing, "mismatched_dates": benchmark_mismatches},
        "symbols": per_symbol,
        "missing_symbols": missing_symbols,
        "corrupt_artifacts": corrupt,
        "malformed_symbol_dates": malformed_symbol_dates,
        "universe": {"id": universe.universe_id, "symbols": symbols,
                     "expected_sha256": expected_universe_hash,
                     "mismatched_artifact_dates": mismatched_artifacts},
        "dataset": {"schema_version": 1, "source": "FYERS history API v3",
                    "date_range": [dates[0], dates[-1]] if dates else None,
                    "artifact_sha256": dataset_sha256, "git_sha": _git_sha(),
                    "synced_at": max((artifact.captured_at.isoformat() for artifact in artifacts), default=None)},
    }
