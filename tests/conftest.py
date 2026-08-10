"""Shared pytest fixtures.

The repo had no conftest.py and no pytest config; each test file rebuilt its
own scaffolding. Fixtures here are the ones needed by more than one module.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from src.core.events import EventBus

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture
async def event_bus() -> AsyncIterator[EventBus]:
    """A started EventBus, stopped on teardown."""
    bus = EventBus()
    await bus.start()
    try:
        yield bus
    finally:
        await bus.stop()


@pytest.fixture
def synthetic_ohlcv():
    """Factory for deterministic OHLCV frames.

    Deterministic by seed so backtest assertions can pin exact trade counts.
    """

    def _make(
        n: int = 500,
        *,
        start_price: float = 100.0,
        trend: float = 0.0,
        vol: float = 0.01,
        seed: int = 0,
        timeframe_minutes: int = 15,
        start: datetime | None = None,
    ) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        steps = rng.normal(loc=trend, scale=vol, size=n)
        close = start_price * np.exp(np.cumsum(steps))

        # Build OHLC around the close path with a plausible intrabar range.
        spread = np.abs(rng.normal(loc=vol / 2, scale=vol / 4, size=n)) * close
        open_ = np.concatenate([[start_price], close[:-1]])
        high = np.maximum(open_, close) + spread
        low = np.minimum(open_, close) - spread
        volume = rng.uniform(500.0, 1500.0, size=n)

        begin = start or (datetime(2026, 1, 1) - timedelta(minutes=timeframe_minutes * n))
        index = pd.date_range(begin, periods=n, freq=f"{timeframe_minutes}min")

        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=index,
        )

    return _make


@pytest.fixture
def tmp_risk_config(tmp_path) -> Iterator[dict]:
    """A risk config dict written to a temp file, with venue-correct symbols."""
    config = {
        "position_sizing": {
            "method": "fixed_fractional",
            "risk_per_trade_pct": 2.0,
            "max_leverage": 5,
            "min_order_size_usd": 10,
        },
        "limits": {
            "max_open_positions": 3,
            "max_portfolio_heat_pct": 15.0,
            "max_daily_loss_pct": 5.0,
            "max_drawdown_pct": 15.0,
            "max_correlated_exposure_multiplier": 1.5,
        },
        "stops": {"require_stop_loss": True, "default_atr_multiplier": 2.0},
        "circuit_breaker": {
            "state_file": str(tmp_path / "circuit_breaker_state.json"),
            "audit_file": str(tmp_path / "circuit_breaker_audit.log"),
        },
        "correlation_groups": [["BTCUSD", "ETHUSD"]],
    }
    path = tmp_path / "risk.yaml"
    path.write_text(yaml.safe_dump(config))
    yield config


@pytest.fixture
def isolated_engine(tmp_path, monkeypatch):
    """A TradingEngine with every persistent path redirected into tmp_path.

    Uses the real config files so tests exercise shipped settings, but no test
    may touch data/trades.db, data/position_state.json or the live circuit
    breaker state.
    """
    from src.core import engine as engine_module
    from src.core.reconciler import PositionStateStore
    from src.portfolio.journal import TradeJournal

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("settings.yaml", "risk.yaml", "strategies.yaml"):
        shutil.copy(CONFIG_DIR / name, config_dir / name)

    risk = yaml.safe_load((config_dir / "risk.yaml").read_text())
    risk["circuit_breaker"]["state_file"] = str(tmp_path / "cb_state.json")
    risk["circuit_breaker"]["audit_file"] = str(tmp_path / "cb_audit.log")
    (config_dir / "risk.yaml").write_text(yaml.safe_dump(risk))

    monkeypatch.setattr(
        engine_module, "TradeJournal", lambda: TradeJournal(str(tmp_path / "trades.db"))
    )
    monkeypatch.setattr(
        engine_module,
        "PositionStateStore",
        lambda: PositionStateStore(str(tmp_path / "position_state.json")),
    )

    eng = engine_module.TradingEngine(config_dir=str(config_dir))
    eng.portfolio.equity = 10_000.0
    eng.portfolio.available_balance = 10_000.0
    return eng
