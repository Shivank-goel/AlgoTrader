"""Portfolio state management and equity tracking."""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Optional

from src.core.models import Direction, PortfolioSnapshot, Position, Regime, TradeRecord

logger = logging.getLogger(__name__)

# get_snapshot() appends on every scan *and* every dashboard poll, so an
# unbounded list grew for the life of the process. ~1 week at a 30s cadence.
EQUITY_HISTORY_MAXLEN = 20_000


class PortfolioManager:
    """Tracks positions, equity, and P&L."""

    def __init__(self, initial_equity: float = 10000.0) -> None:
        self.initial_equity = initial_equity
        self.equity = initial_equity
        self.available_balance = initial_equity
        self._positions: dict[str, Position] = {}
        self._peak_equity = initial_equity
        self._peak_equity_initialized = False
        self._realized_pnl_today = 0.0
        self._equity_history: deque[tuple[datetime, float]] = deque(
            maxlen=EQUITY_HISTORY_MAXLEN
        )
        # Entry fee per open symbol, carried until the position closes so the
        # round trip can be booked net.
        self._entry_fees: dict[str, float] = {}

    def seed_equity_from_exchange(self, balance: float) -> None:
        """Sync equity from exchange balance. On first call, also sets peak."""
        if balance <= 0:
            return
        if not self._peak_equity_initialized:
            self.initial_equity = balance
            self._peak_equity = balance
            self._peak_equity_initialized = True
            logger.info(
                "Portfolio seeded from exchange: equity=%.2f, peak=%.2f",
                balance, balance,
            )
        self.equity = balance
        if balance > self._peak_equity:
            self._peak_equity = balance

    @property
    def positions(self) -> list[Position]:
        return list(self._positions.values())

    def open_position(self, position: Position, entry_fee: float = 0.0) -> None:
        self._positions[position.symbol] = position
        self._entry_fees[position.symbol] = entry_fee
        logger.info(
            "Opened %s %s: %.4f @ %.2f (entry fee $%.4f)",
            position.side.value,
            position.symbol,
            position.size,
            position.entry_price,
            entry_fee,
        )

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        *,
        exit_fee: float = 0.0,
        funding: float = 0.0,
        exit_reason: str = "signal",
    ) -> Optional[TradeRecord]:
        """Close a position and book net P&L.

        Fees and funding are subtracted here rather than being reported
        alongside a gross number, because every downstream consumer — the
        circuit breaker's loss counter, the performance scorer, the equity
        curve — should be reasoning about money actually kept.
        """
        pos = self._positions.pop(symbol, None)
        if not pos:
            return None

        entry_fee = self._entry_fees.pop(symbol, 0.0)
        total_fees = entry_fee + exit_fee

        # underlying_size, not size: in live mode `size` is a contract count.
        qty = pos.underlying_size
        if pos.side == Direction.LONG:
            gross_pnl = (exit_price - pos.entry_price) * qty
        else:
            gross_pnl = (pos.entry_price - exit_price) * qty

        pnl = gross_pnl - total_fees - funding

        notional = pos.entry_price * qty
        pnl_pct = (pnl / notional) * 100 if notional else 0.0
        self.equity += pnl
        self._realized_pnl_today += pnl
        self.available_balance = self.equity

        trade = TradeRecord(
            symbol=symbol,
            side=pos.side,
            strategy_name=pos.strategy_name or "unknown",
            regime=Regime.UNKNOWN,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size=pos.size,
            pnl=pnl,
            pnl_pct=pnl_pct,
            fees=total_fees + funding,
            entry_time=pos.opened_at,
            exit_time=datetime.utcnow(),
            exit_reason=exit_reason,
        )
        if trade.entry_time and trade.exit_time:
            trade.duration_seconds = int((trade.exit_time - trade.entry_time).total_seconds())

        logger.info("Closed %s: PnL=%.2f (%.2f%%)", symbol, pnl, pnl_pct)
        return trade

    def update_prices(self, prices: dict[str, float], from_exchange_sync: bool = False) -> None:
        """Update unrealized PnL from current prices.
        
        If from_exchange_sync is True, skip recalculation — the exchange already
        provided correct unrealized_pnl on each position (which accounts for
        contract_value, fees, etc.).
        """
        if from_exchange_sync:
            return
        for symbol, pos in self._positions.items():
            if symbol in prices:
                price = prices[symbol]
                qty = pos.underlying_size
                if pos.side == Direction.LONG:
                    pos.unrealized_pnl = (price - pos.entry_price) * qty
                else:
                    pos.unrealized_pnl = (pos.entry_price - price) * qty

    def get_snapshot(self) -> PortfolioSnapshot:
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        total_equity = self.equity + unrealized

        if total_equity > self._peak_equity:
            self._peak_equity = total_equity

        drawdown = (
            (self._peak_equity - total_equity) / self._peak_equity * 100
            if self._peak_equity > 0
            else 0
        )

        self._equity_history.append((datetime.utcnow(), total_equity))

        return PortfolioSnapshot(
            equity=total_equity,
            available_balance=self.available_balance,
            unrealized_pnl=unrealized,
            realized_pnl_today=self._realized_pnl_today,
            drawdown_pct=drawdown,
            portfolio_heat_pct=0.0,
            open_positions=self.positions,
        )

    def reset_daily_pnl(self) -> None:
        self._realized_pnl_today = 0.0

    def get_equity_history(self) -> list[tuple[datetime, float]]:
        return list(self._equity_history)
