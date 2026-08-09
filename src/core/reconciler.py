"""Startup reconciliation — ensures local state matches exchange before trading resumes.

On every engine start, this module:
1. Loads persisted local position state from disk (if available)
2. Fetches live positions and open orders from Delta Exchange
3. Compares local vs exchange state
4. Flags discrepancies and halts if unexplained differences are found
5. Adopts verified exchange state as the source of truth
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.core.models import Direction, Position

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = "data/position_state.json"


@dataclass
class Discrepancy:
    """A single mismatch between local persisted state and live exchange state."""

    symbol: str
    kind: str  # "orphan_on_exchange", "missing_from_exchange", "size_mismatch", "side_mismatch"
    details: str
    severity: str = "critical"  # "critical" halts; "warning" logs but continues


@dataclass
class ReconciliationResult:
    """Outcome of the startup reconciliation check."""

    success: bool
    discrepancies: list[Discrepancy] = field(default_factory=list)
    adopted_positions: dict[str, Position] = field(default_factory=dict)
    cancelled_orders: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def has_critical(self) -> bool:
        return any(d.severity == "critical" for d in self.discrepancies)


class PositionStateStore:
    """Persists open position state to a JSON file on every material change."""

    def __init__(self, path: str = DEFAULT_STATE_FILE) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, positions: dict[str, Position]) -> None:
        """Atomically persist current position state."""
        data = {
            "saved_at": datetime.utcnow().isoformat(),
            "positions": {
                symbol: self._serialize_position(pos)
                for symbol, pos in positions.items()
            },
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._path)
        logger.debug("Position state persisted: %d position(s)", len(positions))

    def load(self) -> dict[str, dict[str, Any]]:
        """Load persisted position state. Returns empty dict if no file."""
        if not self._path.exists():
            logger.info("No persisted position state found at %s", self._path)
            return {}
        try:
            data = json.loads(self._path.read_text())
            positions = data.get("positions", {})
            saved_at = data.get("saved_at", "unknown")
            logger.info(
                "Loaded persisted position state: %d position(s), saved at %s",
                len(positions),
                saved_at,
            )
            return positions
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load position state from %s: %s", self._path, exc)
            return {}

    def clear(self) -> None:
        """Remove persisted state file (e.g., after clean shutdown with no positions)."""
        if self._path.exists():
            self._path.unlink()
            logger.debug("Position state file cleared")

    @staticmethod
    def _serialize_position(pos: Position) -> dict[str, Any]:
        return {
            "symbol": pos.symbol,
            "side": pos.side.value,
            "entry_price": pos.entry_price,
            "size": pos.size,
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "strategy_name": pos.strategy_name,
            "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
            "unrealized_pnl": pos.unrealized_pnl,
        }

    @staticmethod
    def deserialize_position(data: dict[str, Any]) -> Position:
        opened_at = data.get("opened_at")
        if opened_at and isinstance(opened_at, str):
            opened_at = datetime.fromisoformat(opened_at)
        else:
            opened_at = datetime.utcnow()

        return Position(
            symbol=data["symbol"],
            side=Direction(data["side"]),
            entry_price=float(data["entry_price"]),
            size=float(data["size"]),
            stop_loss=data.get("stop_loss"),
            take_profit=data.get("take_profit"),
            strategy_name=data.get("strategy_name"),
            opened_at=opened_at,
            unrealized_pnl=float(data.get("unrealized_pnl", 0.0)),
        )


class StartupReconciler:
    """Reconciles local persisted state against live exchange state on startup.

    Fail-safe behaviour:
    - If exchange has positions we don't know about → CRITICAL discrepancy → halt
    - If local state has positions exchange says are flat → CRITICAL discrepancy → halt
    - If sizes or sides disagree → CRITICAL discrepancy → halt
    - Clean state (no local persistence, no exchange positions) → pass
    - Fresh start (no local persistence, exchange has positions) → adopt with WARNING
    """

    SIZE_TOLERANCE_PCT = 5.0  # Allow 5% size difference due to contract rounding

    def __init__(
        self,
        exchange_client: Any,
        state_store: PositionStateStore,
        alert_callback: Optional[Any] = None,
    ) -> None:
        self._exchange = exchange_client
        self._state_store = state_store
        self._alert_callback = alert_callback

    async def reconcile(self) -> ReconciliationResult:
        """Run full reconciliation. Call BEFORE the engine processes any signals."""
        logger.info("Starting startup reconciliation...")

        local_raw = self._state_store.load()
        local_positions: dict[str, Position] = {}
        for symbol, data in local_raw.items():
            try:
                local_positions[symbol] = PositionStateStore.deserialize_position(data)
            except (KeyError, ValueError) as exc:
                logger.error("Failed to deserialize position for %s: %s", symbol, exc)

        try:
            exchange_positions_list = await self._exchange.get_all_positions()
        except Exception as exc:
            message = f"RECONCILIATION FAILED: Cannot fetch exchange positions: {exc}"
            logger.critical(message)
            if self._alert_callback:
                await self._alert_callback(message)
            return ReconciliationResult(
                success=False,
                message=message,
            )

        exchange_positions: dict[str, Position] = {
            pos.symbol: pos for pos in exchange_positions_list
        }

        try:
            open_orders = await self._fetch_open_orders()
        except Exception as exc:
            logger.warning("Could not fetch open orders during reconciliation: %s", exc)
            open_orders = []

        discrepancies: list[Discrepancy] = []
        adopted: dict[str, Position] = {}

        # Case 1: No local state persisted (fresh start or state file lost)
        if not local_positions:
            if exchange_positions:
                for symbol, ex_pos in exchange_positions.items():
                    discrepancies.append(Discrepancy(
                        symbol=symbol,
                        kind="orphan_on_exchange",
                        details=(
                            f"Exchange has {ex_pos.side.value} {ex_pos.size} @ {ex_pos.entry_price} "
                            f"but no local state exists. Possibly from a prior session that crashed "
                            f"without persisting state."
                        ),
                        severity="critical",
                    ))
                    adopted[symbol] = ex_pos
            # No local, no exchange → clean start
        else:
            # Case 2: Compare local vs exchange
            all_symbols = set(local_positions.keys()) | set(exchange_positions.keys())
            for symbol in all_symbols:
                local = local_positions.get(symbol)
                exchange = exchange_positions.get(symbol)

                if local and not exchange:
                    discrepancies.append(Discrepancy(
                        symbol=symbol,
                        kind="missing_from_exchange",
                        details=(
                            f"Local state has {local.side.value} {local.size} @ {local.entry_price} "
                            f"but exchange reports no position. Order may have been filled and closed "
                            f"while bot was down, or position was liquidated."
                        ),
                        severity="critical",
                    ))
                elif exchange and not local:
                    discrepancies.append(Discrepancy(
                        symbol=symbol,
                        kind="orphan_on_exchange",
                        details=(
                            f"Exchange has {exchange.side.value} {exchange.size} @ {exchange.entry_price} "
                            f"but local state has no record. Order may have filled while bot was down."
                        ),
                        severity="critical",
                    ))
                    adopted[symbol] = exchange
                elif local and exchange:
                    if local.side != exchange.side:
                        discrepancies.append(Discrepancy(
                            symbol=symbol,
                            kind="side_mismatch",
                            details=(
                                f"Local={local.side.value}, Exchange={exchange.side.value}. "
                                f"Position may have been flipped manually or via exchange UI."
                            ),
                            severity="critical",
                        ))
                    else:
                        size_diff_pct = abs(local.size - exchange.size) / max(local.size, 1e-9) * 100
                        if size_diff_pct > self.SIZE_TOLERANCE_PCT:
                            discrepancies.append(Discrepancy(
                                symbol=symbol,
                                kind="size_mismatch",
                                details=(
                                    f"Local size={local.size}, Exchange size={exchange.size} "
                                    f"(diff={size_diff_pct:.1f}%). Partial fill/close may have "
                                    f"occurred while bot was down."
                                ),
                                severity="critical",
                            ))

                    # Adopt exchange truth but preserve local metadata
                    merged = Position(
                        symbol=exchange.symbol,
                        side=exchange.side,
                        entry_price=exchange.entry_price,
                        size=exchange.size,
                        unrealized_pnl=exchange.unrealized_pnl,
                        stop_loss=local.stop_loss,
                        take_profit=local.take_profit,
                        strategy_name=local.strategy_name,
                        opened_at=local.opened_at,
                    )
                    adopted[symbol] = merged

        cancelled_orders: list[str] = []
        for order_data in open_orders:
            order_id = str(order_data.get("id", ""))
            if order_id:
                try:
                    await self._exchange.cancel_order(order_id)
                    cancelled_orders.append(order_id)
                    logger.info("Cancelled stale open order %s during reconciliation", order_id)
                except Exception as exc:
                    logger.warning("Failed to cancel stale order %s: %s", order_id, exc)

        has_critical = any(d.severity == "critical" for d in discrepancies)
        if has_critical:
            message = self._format_alert(discrepancies)
            logger.critical("RECONCILIATION DISCREPANCIES FOUND:\n%s", message)
            if self._alert_callback:
                await self._alert_callback(message)

        result = ReconciliationResult(
            success=True,
            discrepancies=discrepancies,
            adopted_positions=adopted,
            cancelled_orders=cancelled_orders,
            message=(
                f"Reconciliation complete: {len(adopted)} position(s) adopted, "
                f"{len(discrepancies)} discrepancy(ies), "
                f"{len(cancelled_orders)} stale order(s) cancelled"
            ),
        )
        logger.info(result.message)
        return result

    async def _fetch_open_orders(self) -> list[dict[str, Any]]:
        """Attempt to fetch open orders from exchange. Best-effort."""
        try:
            result = await self._exchange._request(
                "GET", "/v2/orders", params={"state": "open"}, auth=True
            )
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return result.get("orders", result.get("result", []))
            return []
        except Exception as exc:
            logger.debug("Open orders fetch not supported or failed: %s", exc)
            return []

    @staticmethod
    def _format_alert(discrepancies: list[Discrepancy]) -> str:
        lines = ["STARTUP RECONCILIATION ALERT - TRADING HALTED"]
        lines.append("=" * 50)
        for d in discrepancies:
            lines.append(f"[{d.severity.upper()}] {d.symbol}: {d.kind}")
            lines.append(f"  {d.details}")
            lines.append("")
        lines.append("Manual review required before resuming trading.")
        lines.append("Run `python main.py reconcile --accept` to acknowledge and resume.")
        return "\n".join(lines)
