"""Read-only FYERS v3 account access; not yet connected to the trading engine."""

from __future__ import annotations

import asyncio
import math
import os
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
import yaml


class FyersGatewayError(Exception):
    """FYERS returned an error or an unusable response."""


class FyersClient:
    """Read-only account and market data; no order mutation APIs."""

    def __init__(
        self, app_id: str, access_token: str, *, base_url: str,
        timeout_seconds: float = 20,
    ) -> None:
        if not app_id.strip() or not access_token.strip():
            raise ValueError("FYERS_APP_ID and FYERS_ACCESS_TOKEN are required")
        url = urlparse(base_url)
        if url.scheme != "https" or url.hostname != "api-t1.fyers.in" or url.path.rstrip("/") != "/api/v3" or url.query or url.fragment or url.username or url.password or url.port:
            raise ValueError("Expected the official FYERS v3 HTTPS endpoint")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 0 and 120")
        self._authorization = f"{app_id.strip()}:{access_token.strip()}"
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def from_env(cls, config_path: str | Path | None = None) -> FyersClient:
        """Read exported credentials; caller may load the untracked .env first."""
        path = Path(config_path) if config_path else Path(__file__).resolve().parents[2] / "config/fyers.yaml"
        with path.open() as stream:
            config = yaml.safe_load(stream)
        return cls(os.environ.get("FYERS_APP_ID", ""), os.environ.get("FYERS_ACCESS_TOKEN", ""), **config)

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": self._authorization},
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def _get(self, path: str, *, params: dict[str, str] | None = None,
                   market_data: bool = False) -> dict[str, Any]:
        await self.connect()
        assert self._session is not None
        try:
            base = self.base_url.removesuffix("/api/v3") + "/data" if market_data else self.base_url
            options = {"params": params} if params is not None else {}
            async with self._session.get(f"{base}/{path}", allow_redirects=False, **options) as response:
                if response.status != 200:
                    raise FyersGatewayError(f"FYERS {path}: HTTP {response.status}")
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            # Do not include response bodies, headers or tokens in errors.
            raise FyersGatewayError(f"FYERS {path}: transport or invalid JSON response") from None
        if not isinstance(body, dict) or body.get("s") != "ok":
            raise FyersGatewayError(f"FYERS {path}: API error or invalid response envelope")
        return body

    async def get_profile(self) -> dict[str, Any]:
        return await self._get("profile")

    async def get_funds(self) -> dict[str, Any]:
        return await self._get("funds")

    async def get_holdings(self) -> dict[str, Any]:
        return await self._get("holdings")

    async def get_positions(self) -> dict[str, Any]:
        return await self._get("positions")

    async def get_orders(self) -> dict[str, Any]:
        return await self._get("orders")

    async def get_trades(self) -> dict[str, Any]:
        return await self._get("tradebook")

    async def get_market_status(self) -> dict[str, Any]:
        return await self._get("marketStatus", market_data=True)

    @staticmethod
    def _validate_symbol(symbol: str) -> None:
        if not symbol or ":" not in symbol or any(c.isspace() or c == "," for c in symbol):
            raise ValueError("Use a FYERS symbol such as NSE:SBIN-EQ")

    async def get_quotes(self, symbols: list[str]) -> dict[str, Any]:
        """Fetch a snapshot, not a streaming feed; reject partial symbol failures."""
        if not 1 <= len(symbols) <= 50:
            raise ValueError("Provide 1 to 50 symbols")
        for symbol in symbols:
            self._validate_symbol(symbol)
        body = await self._get("quotes", params={"symbols": ",".join(symbols)}, market_data=True)
        rows = body.get("d")
        if (not isinstance(rows, list) or not rows
                or any(not isinstance(row, dict) or row.get("s") != "ok"
                       or not isinstance(row.get("n"), str)
                       or not isinstance(row.get("v"), dict) for row in rows)
                or {row.get("n") for row in rows} != set(symbols)):
            raise FyersGatewayError("FYERS quotes: missing or rejected symbols")
        return body

    async def get_history(self, symbol: str, start: date, end: date,
                          resolution: str = "D") -> dict[str, Any]:
        """One bounded request; the newest candle may still be incomplete."""
        self._validate_symbol(symbol)
        if resolution not in {"1", "2", "3", "5", "10", "15", "20", "30", "60", "120", "240", "D"}:
            raise ValueError("Unsupported candle resolution")
        limit = 366 if resolution == "D" else 100
        if not 0 <= (end - start).days < limit:
            raise ValueError(f"Request must cover 1 to {limit} calendar days")
        body = await self._get("history", params={
            "symbol": symbol, "resolution": resolution, "date_format": "1",
            "range_from": start.isoformat(), "range_to": end.isoformat(), "cont_flag": "0",
        }, market_data=True)
        candles = body.get("candles")
        if not isinstance(candles, list) or any(
            not isinstance(row, list) or len(row) != 6
            or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in row)
            or row[0] <= 0 or min(row[1:5]) <= 0 or row[5] < 0
            or row[2] < max(row[1], row[3], row[4])
            or row[3] > min(row[1], row[2], row[4])
            for row in candles
        ):
            raise FyersGatewayError("FYERS history: invalid candle response")
        if any(b[0] <= a[0] for a, b in zip(candles, candles[1:])):
            raise FyersGatewayError("FYERS history: duplicate or unordered candles")
        return body
