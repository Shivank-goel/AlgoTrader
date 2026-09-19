"""Legacy optional Dhan gateway. FYERS is the selected NSE integration target."""

from __future__ import annotations

import asyncio
import logging
import re
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

import aiohttp
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

DHAN_API_BASE_URL = "https://api.dhan.co/v2"
CORRELATION_ID_RE = re.compile(r"^[a-zA-Z0-9 _-]{1,30}$")


class DhanGatewayError(Exception):
    """Base error raised by the Dhan gateway."""


class DhanLiveTradingDisabled(DhanGatewayError):
    """Order submission was not explicitly enabled."""


class DhanAmbiguousOrderError(DhanGatewayError):
    """A request may have reached Dhan without a usable response."""


class DhanOrderStatus(str, Enum):
    TRANSIT = "TRANSIT"
    PENDING = "PENDING"
    PART_TRADED = "PART_TRADED"
    TRADED = "TRADED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


def new_dhan_correlation_id() -> str:
    """Return a Dhan-valid idempotency key (at most 30 characters)."""
    return f"dh_{uuid4().hex[:27]}"


class DhanOrderRequest(BaseModel):
    """Minimum Dhan v2 order request with local validation."""

    security_id: str
    quantity: int = Field(gt=0)
    transaction_type: str
    exchange_segment: str
    product_type: str
    order_type: str
    correlation_id: str = Field(default_factory=new_dhan_correlation_id)
    validity: str = "DAY"
    price: Optional[float] = None
    trigger_price: Optional[float] = None

    @field_validator("correlation_id")
    @classmethod
    def correlation_id_is_dhan_compatible(cls, value: str) -> str:
        if not CORRELATION_ID_RE.fullmatch(value):
            raise ValueError("correlation_id must be 1-30 letters, digits, spaces, _ or -")
        return value

    @field_validator("transaction_type")
    @classmethod
    def transaction_type_is_valid(cls, value: str) -> str:
        value = value.upper()
        if value not in {"BUY", "SELL"}:
            raise ValueError("transaction_type must be BUY or SELL")
        return value

    @field_validator("order_type")
    @classmethod
    def order_type_is_valid(cls, value: str) -> str:
        value = value.upper()
        if value not in {"MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_MARKET"}:
            raise ValueError("unsupported Dhan order_type")
        return value

    @model_validator(mode="after")
    def price_requirements_are_valid(self) -> "DhanOrderRequest":
        if self.order_type in {"LIMIT", "STOP_LOSS"} and (self.price is None or self.price <= 0):
            raise ValueError("LIMIT and STOP_LOSS orders require a positive price")
        if self.order_type in {"STOP_LOSS", "STOP_LOSS_MARKET"} and (
            self.trigger_price is None or self.trigger_price <= 0
        ):
            raise ValueError("stop orders require a positive trigger_price")
        return self

    def to_payload(self, dhan_client_id: str) -> dict[str, Any]:
        """Encode this object using Dhan's v2 field names."""
        return {
            "dhanClientId": dhan_client_id,
            "correlationId": self.correlation_id,
            "transactionType": self.transaction_type,
            "exchangeSegment": self.exchange_segment,
            "productType": self.product_type,
            "orderType": self.order_type,
            "validity": self.validity,
            "securityId": self.security_id,
            "quantity": self.quantity,
            "price": self.price or "",
            "triggerPrice": self.trigger_price or "",
            "afterMarketOrder": False,
        }


class DhanOrderResponse(BaseModel):
    """Stable fields used from a Dhan order-book response."""

    order_id: str = Field(alias="orderId")
    order_status: DhanOrderStatus = Field(alias="orderStatus")
    correlation_id: Optional[str] = Field(default=None, alias="correlationId")


class DhanClient:
    """Async Dhan v2 client with explicit live-order enablement."""

    def __init__(
        self,
        dhan_client_id: str,
        access_token: str,
        *,
        allow_order_submission: bool = False,
        base_url: str = DHAN_API_BASE_URL,
        timeout_seconds: float = 20.0,
    ) -> None:
        if not dhan_client_id:
            raise ValueError("dhan_client_id is required")
        if not access_token:
            raise ValueError("access_token is required")
        self.dhan_client_id = dhan_client_id
        self.access_token = access_token
        self.allow_order_submission = allow_order_submission
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._session: Optional[aiohttp.ClientSession] = None

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Content-Type": "application/json", "access-token": self.access_token},
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _request(
        self, method: str, path: str, *, payload: Optional[dict[str, Any]] = None
    ) -> Any:
        await self.connect()
        assert self._session is not None
        try:
            async with self._session.request(method, f"{self.base_url}{path}", json=payload) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise DhanGatewayError(f"Dhan {method} {path} failed with HTTP {response.status}")
                return body
        except asyncio.TimeoutError as exc:
            raise DhanAmbiguousOrderError(f"Dhan {method} {path} timed out") from exc
        except aiohttp.ClientError as exc:
            raise DhanAmbiguousOrderError(f"Dhan {method} {path} transport failure") from exc

    async def get_order_by_correlation_id(self, correlation_id: str) -> Optional[DhanOrderResponse]:
        """Look up a possibly accepted order without placing another one."""
        raw = await self._request("GET", f"/orders/external/{correlation_id}")
        return DhanOrderResponse.model_validate(raw) if raw else None

    async def place_order(self, request: DhanOrderRequest) -> DhanOrderResponse:
        """Submit once; on an unknown result, adopt the order by correlation ID."""
        if not self.allow_order_submission:
            raise DhanLiveTradingDisabled(
                "Dhan order submission is disabled; complete deployment gates first"
            )
        try:
            raw = await self._request("POST", "/orders", payload=request.to_payload(self.dhan_client_id))
            return DhanOrderResponse.model_validate(raw)
        except DhanAmbiguousOrderError:
            logger.warning("Dhan submit ambiguous; looking up correlation_id=%s", request.correlation_id)
            existing = await self.get_order_by_correlation_id(request.correlation_id)
            if existing is not None:
                return existing
            raise
