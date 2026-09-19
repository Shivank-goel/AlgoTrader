"""Delta Exchange WebSocket client for real-time market data."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable, Coroutine, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from src.core.models import Candle

logger = logging.getLogger(__name__)

WS_URL_PROD = "wss://socket.india.delta.exchange"
WS_URL_TESTNET = "wss://socket-ind.testnet.deltaex.org"

MessageHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class WebSocketClient:
    """Async WebSocket client with auto-reconnection."""

    def __init__(
        self,
        testnet: bool = True,
        on_candle: Optional[MessageHandler] = None,
        on_ticker: Optional[MessageHandler] = None,
        on_trade: Optional[MessageHandler] = None,
    ) -> None:
        self.ws_url = WS_URL_TESTNET if testnet else WS_URL_PROD
        self.on_candle = on_candle
        self.on_ticker = on_ticker
        self.on_trade = on_trade
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._subscriptions: list[dict] = []
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60
        self._last_message_time: Optional[datetime] = None

    @property
    def last_message_time(self) -> Optional[datetime]:
        return self._last_message_time

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.open

    async def connect(self) -> None:
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1
                    logger.info("WebSocket connected to %s", self.ws_url)

                    for sub in self._subscriptions:
                        await ws.send(json.dumps(sub))

                    async for message in ws:
                        self._last_message_time = datetime.utcnow()
                        await self._handle_message(message)

            except ConnectionClosed:
                logger.warning("WebSocket connection closed")
            except Exception:
                logger.exception("WebSocket error")

            if self._running:
                logger.info("Reconnecting in %ds...", self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type", "")

        if msg_type == "candlestick" and self.on_candle:
            await self.on_candle(data)
        elif msg_type == "v2/ticker" and self.on_ticker:
            await self.on_ticker(data)
        elif msg_type == "all_trades" and self.on_trade:
            await self.on_trade(data)
        elif msg_type == "ping":
            if self._ws:
                await self._ws.send(json.dumps({"type": "pong"}))

    def subscribe_candles(self, symbol: str, resolution: str) -> None:
        self._subscriptions.append({
            "type": "subscribe",
            "payload": {
                "channels": [
                    {"name": "candlestick", "symbols": [symbol], "resolution": resolution}
                ]
            },
        })

    def subscribe_ticker(self, symbol: str) -> None:
        self._subscriptions.append({
            "type": "subscribe",
            "payload": {
                "channels": [{"name": "v2/ticker", "symbols": [symbol]}]
            },
        })

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    @staticmethod
    def parse_candle(data: dict[str, Any], symbol: str, timeframe: str) -> Optional[Candle]:
        candle_data = data.get("candle", data)
        try:
            return Candle(
                timestamp=datetime.utcfromtimestamp(candle_data.get("time", 0)),
                open=float(candle_data["open"]),
                high=float(candle_data["high"]),
                low=float(candle_data["low"]),
                close=float(candle_data["close"]),
                volume=float(candle_data.get("volume", 0)),
                symbol=symbol,
                timeframe=timeframe,
            )
        except (KeyError, TypeError, ValueError):
            return None
