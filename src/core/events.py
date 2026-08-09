"""Event types and async event bus for the trading engine."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine, Optional, Union

from src.core.models import (
    MarketState,
    Order,
    Regime,
    Signal,
    TradeRecord,
)

logger = logging.getLogger(__name__)

EventHandler = Callable[..., Coroutine[Any, Any, None]]


@dataclass
class SignalEvent:
    signal: Signal
    market_state: MarketState
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class OrderEvent:
    order: Order
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class FillEvent:
    order: Order
    fill_price: float
    fill_size: float
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class RegimeChangeEvent:
    symbol: str
    old_regime: Regime
    new_regime: Regime
    confidence: float
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class TradeClosedEvent:
    trade: TradeRecord
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CircuitBreakerEvent:
    reason: str
    action: str  # halt | reduce_size | close_all
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


Event = Union[
    SignalEvent,
    OrderEvent,
    FillEvent,
    RegimeChangeEvent,
    TradeClosedEvent,
    CircuitBreakerEvent,
]


class EventBus:
    """Simple async pub/sub event bus."""

    def __init__(self, max_queue_size: int = 1000) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue_size)
        self._handlers: dict[type, list[EventHandler]] = defaultdict(list)
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def subscribe(self, event_type: type, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: type, handler: EventHandler) -> None:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    async def publish(self, event: Event) -> None:
        await self._queue.put(event)

    async def _dispatch(self, event: Event) -> None:
        event_type = type(event)
        for handler in self._handlers.get(event_type, []):
            try:
                await handler(event)
            except Exception:
                logger.exception("Error in event handler for %s", event_type.__name__)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._dispatch(event)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
