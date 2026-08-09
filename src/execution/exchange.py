"""Delta Exchange API wrapper with REST authentication and retry logic."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp
from aiohttp import TCPConnector
from aiohttp.resolver import ThreadedResolver

from src.core.models import Candle, Order, OrderSide, OrderStatus, OrderType, Position

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Parse numeric API fields without raising on bad data."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_position(raw: dict[str, Any]) -> Optional[Position]:
    """Validate and parse a single position record from the exchange."""
    from src.core.models import Direction

    symbol = raw.get("product_symbol") or raw.get("symbol") or ""
    if not symbol:
        logger.warning("Skipping position with missing symbol: %s", raw)
        return None

    size = _safe_float(raw.get("size", 0))
    if size == 0:
        return None

    entry_price = _safe_float(raw.get("entry_price", 0))
    if entry_price <= 0:
        logger.warning("Skipping %s position with invalid entry_price: %s", symbol, raw.get("entry_price"))
        return None

    side = Direction.LONG if size > 0 else Direction.SHORT
    return Position(
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        size=abs(size),
        unrealized_pnl=_safe_float(raw.get("unrealized_pnl", 0)),
    )


def _extract_ticker_price(ticker: dict[str, Any]) -> Optional[float]:
    """Extract a usable mark/spot price from a ticker payload."""
    for key in ("mark_price", "close", "spot_price", "last_price"):
        price = _safe_float(ticker.get(key), default=0.0)
        if price > 0:
            return price
    return None


TIMEFRAME_MAP = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "1d": "1d",
    "1w": "1w",
}

# Seconds per candle for each resolution (used for start-time calculation)
RESOLUTION_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "1d": 86400,
    "1w": 604800,
}


class ExchangeError(Exception):
    pass


class AmbiguousOrderError(ExchangeError):
    """Transport/timeout failure where the exchange may have accepted the order."""


class OrderResolutionError(ExchangeError):
    """Order could not be resolved to a terminal state after ambiguous submit."""

    def __init__(self, client_order_id: str, symbol: str, polls: int) -> None:
        self.client_order_id = client_order_id
        self.symbol = symbol
        self.polls = polls
        super().__init__(
            f"Order {client_order_id} for {symbol} unresolved after {polls} status polls"
        )


class PositionCloseError(ExchangeError):
    """Position remained open on the exchange after all close attempts."""

    def __init__(self, symbol: str, reason: str, attempts: int) -> None:
        self.symbol = symbol
        self.reason = reason
        self.attempts = attempts
        super().__init__(
            f"Failed to close {symbol} ({reason}) after {attempts} verified attempts"
        )


TERMINAL_EXCHANGE_ORDER_STATES = frozenset({"closed", "filled", "cancelled", "rejected"})
DEFAULT_ORDER_POLL_MAX = 8
DEFAULT_ORDER_POLL_BASE_INTERVAL = 0.5


def _exchange_order_state(raw: dict[str, Any]) -> str:
    return (raw.get("state") or raw.get("status") or "").lower()


def is_terminal_exchange_order(raw: dict[str, Any]) -> bool:
    return _exchange_order_state(raw) in TERMINAL_EXCHANGE_ORDER_STATES


def _is_ambiguous_transport_error(exc: BaseException) -> bool:
    """True when a request may have reached the server without a usable response."""
    if isinstance(exc, AmbiguousOrderError):
        return True
    if isinstance(exc, aiohttp.ClientError):
        return True
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return True
    if isinstance(exc, ExchangeError):
        message = str(exc)
        if any(code in message for code in ("500", "502", "503", "504", "429")):
            return True
        if "Request failed after retries" in message:
            return True
    return False


class DeltaExchangeClient:
    """Async wrapper for Delta Exchange REST API (Global demo/testnet)."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        testnet: bool = True,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.testnet = testnet
        self.max_retries = max_retries
        self._session: Optional[aiohttp.ClientSession] = None
        self._consecutive_errors = 0
        self._product_cache: dict[str, dict[str, Any]] = {}

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            connector = TCPConnector(
                resolver=ThreadedResolver(),
                ttl_dns_cache=300,
                limit=20,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "crypto-trader/1.0",
                },
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign(self, method: str, path: str, query_string: str = "", payload: str = "") -> dict[str, str]:
        """Generate HMAC-SHA256 signature per Delta Exchange docs.

        signature_data = method + timestamp + path + query_string + payload
        """
        timestamp = str(int(time.time()))
        signature_data = method + timestamp + path + query_string + payload
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_data.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
            "User-Agent": "crypto-trader/1.0",
        }

    def _request_urllib(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        auth: bool = False,
    ) -> Any:
        """Synchronous urllib fallback — reliable DNS on macOS when aiohttp fails.

        Retries on transient errors (429, 5xx, timeouts) with exponential backoff.
        Does NOT retry on 4xx client errors (except 429).
        """
        query_string = ""
        if params:
            query_string = "?" + urlencode(params)

        payload = ""
        if data:
            payload = json.dumps(data, separators=(",", ":"))

        url = f"{self.base_url}{path}{query_string}"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "crypto-trader/1.0",
        }
        if auth:
            headers.update(self._sign(method.upper(), path, query_string, payload))

        body_bytes = payload.encode("utf-8") if payload and method.upper() != "GET" else None
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method.upper())
            ctx = ssl.create_default_context()
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=ctx),
            )

            try:
                with opener.open(req, timeout=30) as resp:
                    raw = resp.read().decode("utf-8")
                    body = json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {"error": raw}
                error_msg = body.get("error", body.get("message", raw))

                if exc.code == 429 or exc.code >= 500:
                    last_error = ExchangeError(f"API error {exc.code}: {error_msg}")
                    if attempt < self.max_retries - 1:
                        wait = 2 ** (attempt + 1)
                        logger.warning(
                            "Transient error %d on %s, retrying in %ds (%d/%d)",
                            exc.code, path, wait, attempt + 1, self.max_retries,
                        )
                        time.sleep(wait)
                        continue
                    raise last_error from exc

                raise ExchangeError(f"API error {exc.code}: {error_msg}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Transport error on %s: %s, retrying in %ds (%d/%d)",
                        path, exc, wait, attempt + 1, self.max_retries,
                    )
                    time.sleep(wait)
                    continue
                raise ExchangeError(f"Request failed after retries: {exc}") from exc

            if isinstance(body, dict) and body.get("success") is False:
                raise ExchangeError(f"API error: {body.get('error', body)}")

            self._consecutive_errors = 0
            return body.get("result", body) if isinstance(body, dict) else body

        raise ExchangeError(f"Request failed after retries: {last_error}") from last_error

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        auth: bool = False,
    ) -> Any:
        loop = asyncio.get_event_loop()
        # urllib only — aiohttp DNS is unreliable in some environments (e.g. IDE sandboxes)
        return await loop.run_in_executor(
            None,
            lambda: self._request_urllib(method, path, params, data, auth),
        )

    async def _request_once(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        auth: bool = False,
    ) -> Any:
        """Perform a single HTTP round trip (no retries, no transport fallback)."""
        await self.connect()
        assert self._session is not None

        url = f"{self.base_url}{path}"
        query_string = ""
        if params:
            query_string = "?" + urlencode(params)

        payload = ""
        if data:
            payload = json.dumps(data, separators=(",", ":"))

        headers: dict[str, str] = {}
        if auth:
            headers.update(self._sign(method.upper(), path, query_string, payload))

        kwargs: dict[str, Any] = {"headers": headers}
        if params:
            kwargs["params"] = params
        if data:
            kwargs["data"] = payload

        try:
            async with self._session.request(method.upper(), url, **kwargs) as resp:
                if resp.status == 429:
                    raise AmbiguousOrderError(f"Rate limited: {resp.status}")

                body = await resp.json()

                if resp.status >= 400:
                    self._consecutive_errors += 1
                    error_msg = body.get("error", body.get("message", body))
                    raise ExchangeError(f"API error {resp.status}: {error_msg}")

                self._consecutive_errors = 0
                return body.get("result", body)
        except ExchangeError:
            raise
        except aiohttp.ClientError as exc:
            self._consecutive_errors += 1
            raise AmbiguousOrderError(f"HTTP client error: {exc}") from exc

    async def lookup_order_by_client_id(
        self, client_order_id: str
    ) -> Optional[dict[str, Any]]:
        """Query Delta for an order by client_order_id before retrying a submit."""
        try:
            result = await self._request_once(
                "GET",
                f"/v2/orders/client_order_id/{client_order_id}",
                auth=True,
            )
            if isinstance(result, dict) and result.get("id") is not None:
                return result
            return None
        except ExchangeError as exc:
            message = str(exc).lower()
            if "404" in message or "not_found" in message or "not found" in message:
                return None
            raise

    async def _submit_order_with_idempotency(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """POST /v2/orders with client_order_id; lookup before any retry.

        Returns (exchange_order_payload, outcome_was_ambiguous).
        """
        client_order_id = payload["client_order_id"]
        last_error: Optional[Exception] = None
        outcome_ambiguous = False

        for attempt in range(self.max_retries):
            try:
                result = await self._request_once(
                    "POST", "/v2/orders", data=payload, auth=True
                )
                return result, outcome_ambiguous
            except ExchangeError as exc:
                if not _is_ambiguous_transport_error(exc):
                    existing = await self.lookup_order_by_client_id(client_order_id)
                    if existing is not None:
                        logger.info(
                            "Adopted existing order for client_order_id=%s after API error",
                            client_order_id,
                        )
                        return existing, False
                    raise
                outcome_ambiguous = True
                last_error = exc
            except AmbiguousOrderError as exc:
                outcome_ambiguous = True
                last_error = exc

            existing = await self.lookup_order_by_client_id(client_order_id)
            if existing is not None:
                logger.info(
                    "Adopted existing order for client_order_id=%s after ambiguous submit",
                    client_order_id,
                )
                return existing, True

            if attempt < self.max_retries - 1:
                wait = 2 ** attempt
                logger.warning(
                    "Order submit ambiguous for client_order_id=%s; "
                    "not on exchange yet, retrying in %ds (%d/%d)",
                    client_order_id,
                    wait,
                    attempt + 1,
                    self.max_retries,
                )
                await asyncio.sleep(wait)
                continue

        raise ExchangeError(
            f"Order submit failed after {self.max_retries} attempts "
            f"for client_order_id={client_order_id}: {last_error}"
        ) from last_error

    async def poll_order_until_terminal(
        self,
        client_order_id: str,
        *,
        symbol: str,
        max_polls: int = DEFAULT_ORDER_POLL_MAX,
        base_interval: float = DEFAULT_ORDER_POLL_BASE_INTERVAL,
    ) -> dict[str, Any]:
        """Poll by client_order_id until filled/cancelled/rejected or exhaustion."""
        last_seen: Optional[dict[str, Any]] = None

        for poll_idx in range(max_polls):
            try:
                raw = await self.lookup_order_by_client_id(client_order_id)
            except ExchangeError as exc:
                if _is_ambiguous_transport_error(exc):
                    logger.warning(
                        "Ambiguous poll error for client_order_id=%s (poll %d/%d): %s",
                        client_order_id,
                        poll_idx + 1,
                        max_polls,
                        exc,
                    )
                    raw = None
                else:
                    raise

            if raw is not None:
                last_seen = raw
                if is_terminal_exchange_order(raw):
                    logger.info(
                        "Order client_order_id=%s reached terminal state %s after %d poll(s)",
                        client_order_id,
                        _exchange_order_state(raw),
                        poll_idx + 1,
                    )
                    return raw

            if poll_idx < max_polls - 1:
                wait = base_interval * (2 ** poll_idx)
                logger.debug(
                    "Order client_order_id=%s still non-terminal; polling again in %.2fs",
                    client_order_id,
                    wait,
                )
                await asyncio.sleep(wait)

        raise OrderResolutionError(client_order_id, symbol, max_polls)

    def _apply_exchange_order_to_order(
        self,
        order: Order,
        raw: dict[str, Any],
        contracts: float,
    ) -> Order:
        """Map a terminal exchange order payload onto the local Order model.

        Handles full fills, partial fills, and cancellations distinctly.
        """
        state = _exchange_order_state(raw)
        order.exchange_order_id = str(raw.get("id", order.exchange_order_id or ""))
        order.size = float(contracts)
        order.updated_at = datetime.utcnow()

        avg = raw.get("average_fill_price") or raw.get("avg_fill_price")

        # Use the explicit filled_size from the exchange response
        actual_filled = _safe_float(raw.get("filled_size"), default=0.0)
        order_size = _safe_float(raw.get("size"), default=contracts)

        if state in ("closed", "filled"):
            order.status = OrderStatus.FILLED
            order.avg_fill_price = float(avg) if avg is not None else order.price
            order.filled_size = actual_filled if actual_filled > 0 else order_size
        elif state == "cancelled":
            # Cancelled orders may have partial fills
            if actual_filled > 0 and avg is not None:
                order.status = OrderStatus.PARTIALLY_FILLED
                order.avg_fill_price = float(avg)
                order.filled_size = actual_filled
                logger.warning(
                    "Order %s cancelled with partial fill: %.0f/%.0f contracts @ %.2f",
                    order.exchange_order_id, actual_filled, order_size, float(avg),
                )
            else:
                order.status = OrderStatus.CANCELLED
                order.filled_size = 0.0
        elif state == "rejected":
            order.status = OrderStatus.REJECTED
            order.filled_size = 0.0
        else:
            order.status = OrderStatus.OPEN

        return order

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    async def get_products(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/v2/products")
        products = result if isinstance(result, list) else result.get("products", [])
        for p in products:
            symbol = p.get("symbol", "")
            self._product_cache[symbol] = p
        return products

    async def get_product(self, symbol: str) -> dict[str, Any]:
        if symbol in self._product_cache:
            return self._product_cache[symbol]
        products = await self.get_products()
        for p in products:
            if p.get("symbol") == symbol:
                return p
        raise ExchangeError(f"Product not found: {symbol}")

    async def get_candles(
        self,
        symbol: str,
        resolution: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 500,
    ) -> list[Candle]:
        import time as _time

        mapped = TIMEFRAME_MAP.get(resolution, resolution)
        bar_seconds = RESOLUTION_SECONDS.get(mapped, 900)
        now = int(_time.time())
        params: dict[str, Any] = {
            "symbol": symbol,
            "resolution": mapped,
            "start": int(start.timestamp()) if start else now - (limit * bar_seconds),
            "end": int(end.timestamp()) if end else now,
        }

        result = await self._request("GET", "/v2/history/candles", params=params)
        candles = []
        raw = result if isinstance(result, list) else result.get("candles", result)
        if not raw:
            return candles

        for c in raw:
            try:
                if isinstance(c, dict):
                    ts = c.get("time", c.get("timestamp"))
                    if ts is None:
                        logger.debug("Skipping candle missing timestamp for %s: %s", symbol, c)
                        continue
                    o, h, l, cl = c.get("open"), c.get("high"), c.get("low"), c.get("close")
                    if any(v is None for v in (o, h, l, cl)):
                        logger.debug("Skipping candle missing OHLC for %s: %s", symbol, c)
                        continue
                    candles.append(
                        Candle(
                            timestamp=datetime.utcfromtimestamp(int(ts)),
                            open=float(o),
                            high=float(h),
                            low=float(l),
                            close=float(cl),
                            volume=_safe_float(c.get("volume", 0)),
                            symbol=symbol,
                            timeframe=resolution,
                        )
                    )
                elif isinstance(c, (list, tuple)) and len(c) >= 6:
                    candles.append(
                        Candle(
                            timestamp=datetime.utcfromtimestamp(int(c[0])),
                            open=float(c[1]),
                            high=float(c[2]),
                            low=float(c[3]),
                            close=float(c[4]),
                            volume=_safe_float(c[5]),
                            symbol=symbol,
                            timeframe=resolution,
                        )
                    )
            except (TypeError, ValueError) as exc:
                logger.warning("Invalid candle data for %s: %s (%s)", symbol, c, exc)
        return sorted(candles, key=lambda x: x.timestamp)

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        return await self._request("GET", f"/v2/tickers/{symbol}")

    async def get_balance(self) -> dict[str, Any]:
        result = await self._request("GET", "/v2/wallet/balances", auth=True)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            balances = result.get("balances", result.get("result", []))
            if isinstance(balances, list):
                return balances
            logger.warning("Unexpected balance response shape: %s", type(result).__name__)
        else:
            logger.warning("Unexpected balance response type: %s", type(result).__name__)
        return []

    async def get_positions(self, underlying: str = "BTC") -> list[Position]:
        result = await self._request(
            "GET",
            "/v2/positions",
            params={"underlying_asset_symbol": underlying},
            auth=True,
        )
        positions: list[Position] = []
        raw = result if isinstance(result, list) else result.get("positions", [])
        if not isinstance(raw, list):
            logger.warning("Unexpected positions response for %s: %s", underlying, type(raw).__name__)
            return positions
        for p in raw:
            if not isinstance(p, dict):
                continue
            parsed = _parse_position(p)
            if parsed is not None:
                positions.append(parsed)
        return positions

    async def get_all_positions(self) -> list[Position]:
        """Fetch all open margined positions."""
        try:
            result = await self._request("GET", "/v2/positions/margined", auth=True)
            raw = result if isinstance(result, list) else result.get("positions", [])
            if not isinstance(raw, list):
                logger.warning("Unexpected margined positions response: %s", type(raw).__name__)
                raw = []
            positions: list[Position] = []
            for p in raw:
                if not isinstance(p, dict):
                    continue
                parsed = _parse_position(p)
                if parsed is not None:
                    positions.append(parsed)
            logger.info("Synced %d open position(s) from margined endpoint", len(positions))
            return positions
        except ExchangeError as exc:
            logger.warning("Margined positions endpoint failed (%s), falling back to per-underlying", exc)

        all_positions: list[Position] = []
        underlyings = set()
        try:
            products = await self.get_products()
            for p in products:
                if p.get("contract_type") == "perpetual_futures":
                    u = (p.get("underlying_asset") or {}).get("symbol", "")
                    if u:
                        underlyings.add(u)
        except ExchangeError:
            underlyings = {"BTC", "ETH"}

        for underlying in sorted(underlyings):
            try:
                pos = await self.get_positions(underlying)
                all_positions.extend(pos)
            except ExchangeError as exc:
                logger.debug("Positions fetch failed for %s: %s", underlying, exc)
                continue
        logger.info("Synced %d open position(s) via per-underlying fallback", len(all_positions))
        return all_positions

    async def get_open_position_for_symbol(self, symbol: str) -> Optional[Position]:
        """Return the exchange-reported open position for a symbol, if any."""
        target = symbol.upper()
        for position in await self.get_all_positions():
            if position.symbol.upper() == target:
                return position
        return None

    async def is_position_flat(self, symbol: str) -> bool:
        """True when the exchange reports no open position for the symbol."""
        return await self.get_open_position_for_symbol(symbol) is None

    async def contracts_for_notional(self, symbol: str, notional_usd: float) -> int:
        """Convert USD notional to integer contract count for Delta perpetuals."""
        product = await self.get_product(symbol)
        min_size = max(1, int(float(product.get("min_size", 1) or 1)))
        contract_value = float(product.get("contract_value", 1) or 1)
        if contract_value <= 0:
            contract_value = 1.0

        # contract_value is in the underlying asset (e.g. 0.001 BTC).
        # Multiply by mark price to get USD value per contract.
        try:
            ticker = await self.get_ticker(symbol)
            mark = float(
                ticker.get("mark_price")
                or ticker.get("close")
                or ticker.get("spot_price")
                or 0
            )
        except ExchangeError:
            mark = 0.0

        usd_per_contract = contract_value * mark if mark > 0 else contract_value
        contracts = int(math.floor(notional_usd / usd_per_contract))

        max_order_size = int(float(product.get("position_size_limit", 0) or 0))
        if max_order_size > 0:
            contracts = min(contracts, max_order_size)
        return max(min_size, contracts)

    async def place_order(self, order: Order) -> Order:
        product = await self.get_product(order.symbol)
        min_size = max(1, int(float(product.get("min_size", 1) or 1)))

        # Delta expects integer contract count, not fractional coin size.
        if order.order_type == OrderType.MARKET and order.price:
            notional = order.size * order.price
        else:
            try:
                ticker = await self.get_ticker(order.symbol)
                mark = float(
                    ticker.get("mark_price")
                    or ticker.get("close")
                    or ticker.get("spot_price")
                    or 0
                )
            except ExchangeError:
                mark = order.price or 0.0
            notional = order.size * mark if mark else order.size

        min_order_usd = 10.0
        if notional < min_order_usd:
            notional = min_order_usd
        contracts = await self.contracts_for_notional(order.symbol, notional)
        contracts = max(min_size, contracts)
        max_order_size = int(float(product.get("position_size_limit", 0) or 0))
        if max_order_size > 0:
            contracts = min(contracts, max_order_size)

        if not order.client_order_id:
            from src.core.models import new_client_order_id

            order.client_order_id = new_client_order_id()

        payload = {
            "product_id": product["id"],
            "size": contracts,
            "side": order.side.value,
            "order_type": self._map_order_type(order.order_type),
            "client_order_id": order.client_order_id,
        }
        if order.price is not None and order.order_type != OrderType.MARKET:
            payload["limit_price"] = str(order.price)

        result, outcome_ambiguous = await self._submit_order_with_idempotency(payload)

        if outcome_ambiguous or not is_terminal_exchange_order(result):
            result = await self.poll_order_until_terminal(
                order.client_order_id,
                symbol=order.symbol,
            )

        return self._apply_exchange_order_to_order(order, result, float(contracts))

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel and verify the order is actually cancelled on the exchange."""
        await self._request("DELETE", f"/v2/orders/{order_id}", auth=True)
        for attempt in range(3):
            try:
                detail = await self.get_order(order_id)
                state = _exchange_order_state(detail)
                if state in ("cancelled", "rejected"):
                    return True
                if state in ("closed", "filled"):
                    logger.warning(
                        "Order %s filled before cancel took effect (state=%s)",
                        order_id,
                        state,
                    )
                    return False
            except ExchangeError:
                pass
            await asyncio.sleep(0.5 * (2 ** attempt))
        logger.warning(
            "Cancel verification inconclusive for order %s after 3 checks", order_id
        )
        return False

    async def place_bracket_order(
        self,
        symbol: str,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        trigger_method: str = "last_traded_price",
    ) -> Optional[dict[str, Any]]:
        """Attach SL/TP bracket to an existing position via POST /v2/orders/bracket."""
        if stop_loss_price is None and take_profit_price is None:
            return None

        product = await self.get_product(symbol)
        payload: dict[str, Any] = {
            "product_id": product["id"],
            "bracket_stop_trigger_method": trigger_method,
        }

        if stop_loss_price is not None:
            payload["stop_loss_order"] = {
                "order_type": "market_order",
                "stop_price": str(stop_loss_price),
            }
        if take_profit_price is not None:
            payload["take_profit_order"] = {
                "order_type": "market_order",
                "stop_price": str(take_profit_price),
            }

        try:
            result = await self._request("POST", "/v2/orders/bracket", data=payload, auth=True)
            logger.info(
                "Bracket order placed for %s: SL=%s TP=%s",
                symbol, stop_loss_price, take_profit_price,
            )
            return result
        except ExchangeError as exc:
            exc_str = str(exc)
            if "bracket_order_exists" in exc_str:
                logger.debug("Bracket already exists for %s — no action needed", symbol)
                return {"already_exists": True}
            if "immediate_execution" in exc_str or "immediate_liquidation" in exc_str:
                logger.warning(
                    "Bracket order invalid for %s (price already past SL/TP): %s",
                    symbol,
                    exc,
                )
                return {"bracket_invalid": True}
            logger.warning("Bracket order failed for %s: %s", symbol, exc)
            return None

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v2/orders/{order_id}", auth=True)

    def _map_order_type(self, order_type: OrderType) -> str:
        mapping = {
            OrderType.MARKET: "market_order",
            OrderType.LIMIT: "limit_order",
            OrderType.STOP_MARKET: "stop_market_order",
            OrderType.STOP_LIMIT: "stop_limit_order",
        }
        return mapping.get(order_type, "market_order")
