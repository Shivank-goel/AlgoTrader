"""Validated FYERS order/trade stream boundary; deployment remains disabled."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.fyers.journal import Journal


class BrokerTradeUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, allow_inf_nan=False)
    trade_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    symbol: str = Field(pattern=r"^NSE:.+-EQ$")
    quantity: int = Field(gt=0, strict=True)
    price: float = Field(gt=0)
    side: int
    timestamp: datetime

    @model_validator(mode="after")
    def valid(self) -> BrokerTradeUpdate:
        if self.side not in {-1, 1} or self.timestamp.tzinfo is None:
            raise ValueError("invalid FYERS trade direction or timestamp")
        return self


class DisabledOrderStream:
    """Deduplicates validated fixtures without opening a broker connection."""

    def __init__(self, journal: Journal, *, enabled: bool = False) -> None:
        if enabled:
            raise ValueError("order WebSocket deployment is not authorized")
        self.journal = journal
        with journal.db:
            journal.db.execute("""CREATE TABLE IF NOT EXISTS broker_trade_updates (
                trade_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, payload TEXT NOT NULL,
                received REAL NOT NULL)""")

    def accept_trade(self, payload: dict, *, received: float) -> bool:
        if not math.isfinite(received) or received <= 0:
            raise ValueError("received timestamp must be positive and finite")
        normalized = dict(payload)
        stamp = normalized.get("timestamp")
        if isinstance(stamp, int | float):
            normalized["timestamp"] = datetime.fromtimestamp(stamp, UTC)
        update = BrokerTradeUpdate.model_validate(normalized)
        encoded = update.model_dump_json()
        old = self.journal.db.execute(
            "SELECT payload FROM broker_trade_updates WHERE trade_id=?", (update.trade_id,),
        ).fetchone()
        if old:
            if old["payload"] != encoded:
                raise ValueError("broker trade ID conflicts with durable history")
            return False
        with self.journal.db:
            self.journal.db.execute(
                "INSERT INTO broker_trade_updates VALUES(?,?,?,?)",
                (update.trade_id, update.order_id, encoded, received),
            )
        return True
