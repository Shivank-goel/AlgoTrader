"""Central risk orchestrator for pre-trade checks and portfolio monitoring."""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.core.events import CircuitBreakerEvent
from src.core.models import PortfolioSnapshot, Position, Signal
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.position_sizer import PositionSizer

logger = logging.getLogger(__name__)


class RiskManager:
    """Orchestrates all risk checks before trade execution."""

    def __init__(
        self,
        config: dict[str, Any],
        position_sizer: PositionSizer,
        circuit_breaker: CircuitBreaker,
    ) -> None:
        self.config = config
        self.limits = config.get("limits", {})
        self.position_sizer = position_sizer
        self.circuit_breaker = circuit_breaker
        self._correlation_groups = config.get("correlation_groups", [])
        self._pending_cb_event: Optional[CircuitBreakerEvent] = None

    def take_pending_cb_event(self) -> Optional[CircuitBreakerEvent]:
        event = self._pending_cb_event
        self._pending_cb_event = None
        return event

    def can_trade(self, portfolio: PortfolioSnapshot) -> tuple[bool, str]:
        if self.circuit_breaker.is_halted:
            return False, "Circuit breaker is active"

        cb_event = self.circuit_breaker.check(portfolio)
        if cb_event and cb_event.action in ("halt", "close_all"):
            self._pending_cb_event = cb_event
            return False, f"Circuit breaker: {cb_event.reason}"

        max_positions = self.limits.get("max_open_positions", 3)
        if len(portfolio.open_positions) >= max_positions:
            return False, f"Max positions reached ({max_positions})"

        max_heat = self.limits.get("max_portfolio_heat_pct", 15.0)
        if portfolio.portfolio_heat_pct >= max_heat:
            return False, f"Portfolio heat too high ({portfolio.portfolio_heat_pct:.1f}%)"

        max_dd = self.limits.get("max_drawdown_pct", 15.0)
        if portfolio.drawdown_pct >= max_dd:
            return False, f"Max drawdown reached ({portfolio.drawdown_pct:.1f}%)"

        return True, "OK"

    def validate_signal(self, signal: Signal, portfolio: PortfolioSnapshot) -> tuple[bool, str]:
        can, reason = self.can_trade(portfolio)
        if not can:
            return False, reason

        if not signal.stop_loss:
            require_sl = self.config.get("stops", {}).get("require_stop_loss", True)
            if require_sl:
                return False, "Stop-loss is required"

        existing = [p for p in portfolio.open_positions if p.symbol == signal.symbol]
        if existing:
            return False, f"Already have position in {signal.symbol}"

        if self._is_correlated_exposure(signal.symbol, portfolio):
            return False, "Correlated exposure limit reached"

        return True, "OK"

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,
        atr: Optional[float] = None,
    ) -> float:
        sizing_equity = min(portfolio.equity, portfolio.available_balance)
        return self.position_sizer.calculate(
            signal=signal,
            equity=sizing_equity,
            atr=atr,
            size_multiplier=self.circuit_breaker.size_multiplier,
        )

    def calculate_portfolio_heat(
        self, positions: list[Position], equity: float
    ) -> float:
        if equity <= 0:
            return 0.0
        total_risk = 0.0
        for pos in positions:
            if pos.stop_loss and pos.entry_price:
                risk_per_unit = abs(pos.entry_price - pos.stop_loss)
                total_risk += risk_per_unit * pos.size
        return (total_risk / equity) * 100

    def _is_correlated_exposure(
        self, symbol: str, portfolio: PortfolioSnapshot
    ) -> bool:
        for group in self._correlation_groups:
            if symbol in group:
                open_in_group = [
                    p for p in portfolio.open_positions if p.symbol in group
                ]
                if open_in_group:
                    return True
        return False
