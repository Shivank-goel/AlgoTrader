"""Validated NSE cash-equity data and configuration boundaries."""

from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[2]


def environment_path() -> Path:
    """VM deployments may keep credentials outside the Git checkout."""
    configured = os.environ.get("FYERS_ENV_FILE")
    return Path(configured).expanduser() if configured else ROOT / ".env"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbols: list[str] = Field(min_length=1, max_length=50)
    master_url: str
    database: str
    sdk_logs: str
    stale_seconds: float = Field(gt=0, le=300)
    queue_size: int = Field(gt=0, le=100000)
    reconnect_attempts: int = Field(ge=0, le=50)
    reconcile_seconds: float = Field(ge=10)
    capital_inr: float = Field(gt=0)
    max_order_inr: float = Field(gt=0)
    max_daily_loss_inr: float = Field(gt=0)
    slippage_bps: float = Field(ge=0, le=100)
    qualification_file: str
    trials_file: str
    backup_directory: str = "data/fyers/backups"
    backup_max_age_seconds: float = Field(default=172800, gt=0)
    alert_after_seconds: float = Field(default=300, gt=0)
    auto_record: bool = False
    session_start: str = Field(default="09:10", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    session_end: str = Field(default="15:35", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    holidays_file: str = "config/nse_holidays.yaml"
    backup_retention_count: int = Field(default=14, ge=1, le=365)
    recorder_poll_seconds: float = Field(default=30, ge=5, le=300)
    recorder_max_restarts: int = Field(default=3, ge=0, le=20)
    recorder_restart_window_seconds: float = Field(default=3600, ge=60, le=86400)
    recorder_backoff_seconds: float = Field(default=30, ge=1, le=3600)
    strategy_lab_file: str = "config/fyers_strategy_lab.yaml"
    strategy_poll_seconds: float = Field(default=30, ge=5, le=300)
    daily_bars_directory: str = "data/fyers/daily-bars"
    history_request_interval_seconds: float = Field(default=1.1, ge=0.1, le=30)
    history_rate_limit_retries: int = Field(default=5, ge=0, le=10)
    history_rate_limit_backoff_seconds: float = Field(default=5, ge=1, le=300)
    session_export_directory: str = "data/fyers/sessions"
    regime_symbol: str = Field(default="NSE:NIFTY50-INDEX", pattern=r"^NSE:.+-INDEX$")
    expected_outbound_ip: str | None = None
    minimum_free_disk_bytes: int = Field(default=1_073_741_824, ge=100_000_000)
    monthly_infrastructure_inr: float = Field(default=0, ge=0)
    finalization_delay_minutes: int = Field(default=10, ge=0, le=180)
    finalization_max_retries: int = Field(default=3, ge=1, le=20)
    finalization_backoff_seconds: float = Field(default=300, ge=30, le=3600)

    @field_validator("symbols")
    @classmethod
    def cash_equity_symbols(cls, symbols: list[str]) -> list[str]:
        if len(set(symbols)) != len(symbols) or any(not s.startswith("NSE:") or not s.endswith("-EQ") for s in symbols):
            raise ValueError("Use unique NSE cash equity symbols ending in -EQ")
        return symbols

    @model_validator(mode="after")
    def valid_session_window(self) -> RuntimeConfig:
        if self.session_start >= self.session_end:
            raise ValueError("session_start must be earlier than session_end")
        return self

    @classmethod
    def load(cls, path: Path = ROOT / "config/fyers_runtime.yaml") -> RuntimeConfig:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class Instrument(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    symbol: str
    isin: str = Field(min_length=1)
    lot_size: int = Field(gt=0)
    tick_size: Decimal = Field(gt=0)
    trading_session: str = ""
    master_updated: str = ""


class Quote(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    symbol: str
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    bid_size: int = Field(gt=0)
    ask_size: int = Field(gt=0)
    exchange_time: float = Field(gt=0)
    received_time: float = Field(gt=0)

    def usable(self, now: float, stale_seconds: float) -> bool:
        return (self.ask >= self.bid
                and 0 <= now - self.received_time <= stale_seconds
                and 0 <= now - self.exchange_time <= stale_seconds)


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    intent_id: str = Field(min_length=1, max_length=100)
    strategy: str = Field(min_length=1)
    symbol: str
    side: Side
    quantity: int = Field(gt=0, strict=True)
    created_at: datetime
