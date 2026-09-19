"""Validated NSE cash-equity data and configuration boundaries."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator
import yaml

ROOT = Path(__file__).resolve().parents[2]


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

    @field_validator("symbols")
    @classmethod
    def cash_equity_symbols(cls, symbols: list[str]) -> list[str]:
        if len(set(symbols)) != len(symbols) or any(not s.startswith("NSE:") or not s.endswith("-EQ") for s in symbols):
            raise ValueError("Use unique NSE cash equity symbols ending in -EQ")
        return symbols

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
