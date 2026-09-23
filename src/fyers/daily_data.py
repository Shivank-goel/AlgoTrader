"""Immutable completed-bar artifacts used by forward research and replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.execution.fyers import FyersClient, FyersGatewayError
from src.fyers.models import ROOT, RuntimeConfig
from src.fyers.sessions import IST


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


class DailyBar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)

    @model_validator(mode="after")
    def valid_range(self) -> DailyBar:
        if self.high < max(self.open, self.low, self.close) or self.low > min(
            self.open, self.high, self.close
        ):
            raise ValueError("daily OHLC range is invalid")
        return self


class CompletedBarArtifact(BaseModel):
    """One completed NSE session. Missing members stay explicit."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    session_date: date
    source: str = "FYERS history API v3"
    captured_at: datetime
    universe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bars: dict[str, DailyBar]
    benchmark_symbol: str
    benchmark: DailyBar | None
    missing_symbols: list[str]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_content(self) -> CompletedBarArtifact:
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("capture timestamp must be timezone-aware")
        if set(self.bars) & set(self.missing_symbols):
            raise ValueError("a symbol cannot be both present and missing")
        raw = self.model_dump(mode="json", exclude={"content_sha256"})
        if _hash(raw) != self.content_sha256:
            raise ValueError("completed-bar artifact hash mismatch")
        return self


def load_completed_bar(path: Path) -> CompletedBarArtifact:
    return CompletedBarArtifact.model_validate_json(path.read_bytes())


def _artifact(payload: dict) -> CompletedBarArtifact:
    # Hash the canonical, type-normalized representation. Raw callers may use
    # ISO strings while validation materializes date/datetime/model objects.
    captured_at = payload["captured_at"]
    if isinstance(captured_at, str):
        captured_at = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    session_date = payload["session_date"]
    if isinstance(session_date, str):
        session_date = date.fromisoformat(session_date)
    normalized = {
        **payload,
        "session_date": session_date,
        "captured_at": captured_at,
        "bars": {symbol: DailyBar.model_validate(bar)
                 for symbol, bar in payload["bars"].items()},
        "benchmark": (DailyBar.model_validate(payload["benchmark"])
                      if payload.get("benchmark") is not None else None),
    }
    provisional = CompletedBarArtifact.model_construct(
        **normalized, content_sha256="0" * 64,
    ).model_dump(mode="json", exclude={"content_sha256"})
    return CompletedBarArtifact.model_validate({**provisional, "content_sha256": _hash(provisional)})


def _publish(path: Path, artifact: CompletedBarArtifact) -> CompletedBarArtifact:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(artifact.model_dump(mode="json"), indent=2, allow_nan=False)
    if path.exists():
        old = load_completed_bar(path)
        excluded = {"captured_at", "content_sha256"}
        if old.model_dump(mode="json", exclude=excluded) != artifact.model_dump(
            mode="json", exclude=excluded,
        ):
            raise ValueError(f"completed-bar artifact already exists with different data: {path.name}")
        return old
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return artifact


def _candles(body: dict) -> dict[date, DailyBar]:
    rows: dict[date, DailyBar] = {}
    for epoch, opening, high, low, close, volume in body.get("candles", []):
        day = datetime.fromtimestamp(epoch, IST).date()
        bar = DailyBar(open=opening, high=high, low=low, close=close, volume=volume)
        if day in rows and rows[day] != bar:
            raise ValueError("FYERS returned conflicting daily candles")
        rows[day] = bar
    return rows


async def sync_daily_history(
    client: FyersClient,
    config: RuntimeConfig,
    *,
    start: date,
    end: date,
    captured_at: datetime | None = None,
) -> list[CompletedBarArtifact]:
    """Fetch completed daily bars in bounded FYERS requests and publish per-day artifacts."""
    if end < start:
        raise ValueError("daily history end precedes start")
    captured_at = captured_at or datetime.now(UTC)
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("capture timestamp must be timezone-aware")
    local_now = captured_at.astimezone(IST)
    if end > local_now.date() or (end == local_now.date() and local_now.strftime("%H:%M") <= config.session_end):
        raise ValueError("daily history may contain an incomplete current-session candle")
    symbols = [*config.symbols, config.regime_symbol]
    by_symbol: dict[str, dict[date, DailyBar]] = {symbol: {} for symbol in symbols}
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=365))
        for symbol in symbols:
            body = await _history_with_rate_limit(client, symbol, cursor, chunk_end, config)
            by_symbol[symbol].update(_candles(body))
            await asyncio.sleep(config.history_request_interval_seconds)
        cursor = chunk_end + timedelta(days=1)
    lab = yaml.safe_load((ROOT / config.strategy_lab_file).read_text())
    universe_path = ROOT / lab["regime_selector"]["universe_file"]
    universe_sha256 = hashlib.sha256(universe_path.read_bytes()).hexdigest()
    output = []
    # Returned exchange sessions, rather than the currently configured calendar
    # year, define historical dates. This permits bounded multi-year backfills
    # while the runtime calendar remains fail-closed for today's session.
    session_days = sorted({day for rows in by_symbol.values() for day in rows if start <= day <= end})
    for day in session_days:
        bars = {symbol: rows[day] for symbol, rows in by_symbol.items()
                if symbol != config.regime_symbol and day in rows}
        missing = sorted(set(config.symbols) - set(bars))
        artifact = _artifact({
            "schema_version": 1,
            "session_date": day.isoformat(),
            "source": "FYERS history API v3",
            "captured_at": captured_at.isoformat(),
            "universe_sha256": universe_sha256,
            "bars": {symbol: bar.model_dump(mode="json") for symbol, bar in bars.items()},
            "benchmark_symbol": config.regime_symbol,
            "benchmark": (by_symbol[config.regime_symbol].get(day).model_dump(mode="json")
                          if day in by_symbol[config.regime_symbol] else None),
            "missing_symbols": missing,
        })
        published = _publish(ROOT / config.daily_bars_directory / f"{day.isoformat()}.json", artifact)
        output.append(published)
    return output


async def _history_with_rate_limit(
    client: FyersClient, symbol: str, start: date, end: date, config: RuntimeConfig,
) -> dict:
    """Retry only FYERS HTTP 429 responses with bounded exponential backoff."""
    for attempt in range(config.history_rate_limit_retries + 1):
        try:
            return await client.get_history(symbol, start, end, "D")
        except FyersGatewayError as exc:
            if "HTTP 429" not in str(exc) or attempt == config.history_rate_limit_retries:
                raise
            await asyncio.sleep(config.history_rate_limit_backoff_seconds * (2 ** attempt))
    raise AssertionError("unreachable")


def completed_bar_panel(
    directory: Path,
    symbols: list[str],
    *,
    through: date | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, list[CompletedBarArtifact]]:
    """Load immutable completed sessions for identical replay/forward decisions."""
    artifacts = []
    for path in sorted(directory.glob("????-??-??.json")):
        artifact = load_completed_bar(path)
        if through is None or artifact.session_date <= through:
            artifacts.append(artifact)
    if not artifacts:
        return pd.DataFrame(columns=symbols), pd.DataFrame(columns=symbols), pd.Series(dtype=float), []
    if any(a.session_date >= b.session_date for a, b in zip(artifacts, artifacts[1:], strict=False)):
        raise ValueError("completed-bar artifacts are duplicate or unordered")
    index = pd.DatetimeIndex([pd.Timestamp(a.session_date) for a in artifacts])
    closes = pd.DataFrame(
        [{symbol: artifact.bars[symbol].close if symbol in artifact.bars else float("nan")
          for symbol in symbols} for artifact in artifacts], index=index,
    )
    opens = pd.DataFrame(
        [{symbol: artifact.bars[symbol].open if symbol in artifact.bars else float("nan")
          for symbol in symbols} for artifact in artifacts], index=index,
    )
    benchmark = pd.Series(
        [artifact.benchmark.close if artifact.benchmark else float("nan") for artifact in artifacts],
        index=index, name=artifacts[-1].benchmark_symbol,
    )
    return closes, opens, benchmark, artifacts
