"""Core data models for the trading system."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def new_client_order_id() -> str:
    """32-char idempotency key accepted by Delta Exchange."""
    return uuid4().hex


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class Regime(str, Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"
    QUIET = "quiet"
    UNKNOWN = "unknown"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class SignalAction(str, Enum):
    ENTER = "enter"
    EXIT = "exit"
    HOLD = "hold"


class Candle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str
    timeframe: str


class Signal(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    symbol: str
    direction: Direction
    action: SignalAction = SignalAction.ENTER
    strategy_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    regime: Optional[Regime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Order(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    client_order_id: str = Field(default_factory=new_client_order_id)
    exchange_order_id: Optional[str] = None
    symbol: str
    side: OrderSide
    order_type: OrderType
    size: float
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    avg_fill_price: Optional[float] = None
    signal_id: Optional[str] = None
    strategy_name: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Position(BaseModel):
    symbol: str
    side: Direction
    entry_price: float
    size: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    leverage: float = 1.0
    strategy_name: Optional[str] = None
    opened_at: datetime = Field(default_factory=datetime.utcnow)


class TradeRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    symbol: str
    side: Direction
    strategy_name: str
    regime: Regime
    entry_price: float
    exit_price: Optional[float] = None
    size: float
    pnl: float = 0.0
    pnl_pct: float = 0.0
    fees: float = 0.0
    entry_time: datetime
    exit_time: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    indicators_at_entry: dict[str, Any] = Field(default_factory=dict)
    exit_reason: Optional[str] = None


class MarketState(BaseModel):
    symbol: str
    timestamp: datetime
    price: float
    regime: Regime
    regime_confidence: float = 0.0
    indicators: dict[str, Any] = Field(default_factory=dict)
    multi_timeframe: dict[str, dict[str, Any]] = Field(default_factory=dict)
    support_levels: list[float] = Field(default_factory=list)
    resistance_levels: list[float] = Field(default_factory=list)
    candles: list[Candle] = Field(default_factory=list)


class PortfolioSnapshot(BaseModel):
    equity: float
    available_balance: float
    unrealized_pnl: float
    realized_pnl_today: float
    drawdown_pct: float
    portfolio_heat_pct: float
    open_positions: list[Position] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
