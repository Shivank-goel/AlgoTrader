"""Auditable signal and exit records; no broker interaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from src.fyers.models import Side


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    timestamp: float
    symbol: str
    market_regime: str
    stock_state: str
    strategy_family: str | None
    action: str
    reason: str
    long_score: float | None
    short_score: float | None
    relative_strength_rank: int | None
    relative_weakness_rank: int | None
    risk_multiplier: float
    reference_price: float | None

    def payload(self) -> dict:
        return asdict(self)

    @staticmethod
    def identity(timestamp: float, symbol: str, route: dict, stock: dict) -> str:
        raw = json.dumps({"timestamp": timestamp, "symbol": symbol, "route": route, "stock": stock},
                         sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


def target_deltas(targets: dict[str, int], current: dict[str, int]) -> dict[str, tuple[Side, int]]:
    """Convert target holdings into deterministic entry/exit quantities."""
    result = {}
    for symbol in sorted(set(targets) | set(current)):
        delta = targets.get(symbol, 0) - current.get(symbol, 0)
        if delta:
            result[symbol] = (Side.BUY if delta > 0 else Side.SELL, abs(delta))
    return result
