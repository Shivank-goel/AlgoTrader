"""Order lifecycle management — signals to orders with bracket support."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Optional

from src.backtest.costs import CostModel
from src.core.events import EventBus, FillEvent, OrderEvent
from src.core.models import (
    Direction,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Signal,
    SignalAction,
)
from src.execution.exchange import (
    DeltaExchangeClient,
    OrderResolutionError,
    PositionCloseError,
)

logger = logging.getLogger(__name__)
slippage_logger = logging.getLogger("execution.slippage")

AlertCallback = Callable[[str], Awaitable[None]]

DEFAULT_CLOSE_MAX_RETRIES = 5
DEFAULT_CLOSE_BASE_INTERVAL = 1.0


class OrderManager:
    """Manages order creation, tracking, and fill handling."""

    def __init__(
        self,
        exchange: DeltaExchangeClient,
        event_bus: EventBus,
        paper_mode: bool = True,
        alert_callback: Optional[AlertCallback] = None,
        cost_model: Optional[CostModel] = None,
    ) -> None:
        self.exchange = exchange
        self.event_bus = event_bus
        self.paper_mode = paper_mode
        self._alert_callback = alert_callback
        # Paper fills are priced by the same model the backtester uses, so a
        # paper result can be compared against a backtest at all.
        self.cost_model = cost_model or CostModel()
        self._orders: dict[str, Order] = {}
        self._halted_symbols: set[str] = set()

    def set_alert_callback(self, callback: Optional[AlertCallback]) -> None:
        self._alert_callback = callback

    def is_symbol_halted(self, symbol: str) -> bool:
        return symbol.upper() in self._halted_symbols

    def get_halted_symbols(self) -> set[str]:
        return set(self._halted_symbols)

    def _halt_symbol(self, symbol: str) -> None:
        self._halted_symbols.add(symbol.upper())

    def signal_to_order(self, signal: Signal, size: float) -> Order:
        side = OrderSide.BUY if signal.direction == Direction.LONG else OrderSide.SELL
        order = Order(
            symbol=signal.symbol,
            side=side,
            order_type=OrderType.MARKET,
            size=size,
            price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            signal_id=signal.id,
            strategy_name=signal.strategy_name,
        )
        return order

    async def execute_signal(self, signal: Signal, size: float) -> Optional[Order]:
        if signal.action == SignalAction.HOLD:
            return None

        if self.is_symbol_halted(signal.symbol):
            logger.warning("Signal blocked for halted symbol %s", signal.symbol)
            return None

        if signal.action == SignalAction.EXIT:
            return await self.close_position_verified(
                symbol=signal.symbol,
                direction=signal.direction,
                size=size,
                mark_price=signal.entry_price,
                strategy_name=signal.strategy_name,
                reason="signal_exit",
                signal_id=signal.id,
            )

        order = self.signal_to_order(signal, size)
        return await self.submit_order(order)

    async def submit_order(self, order: Order) -> Order:
        if self.is_symbol_halted(order.symbol):
            raise ValueError(f"Trading halted for unresolved order on {order.symbol}")

        self._orders[order.id] = order
        await self.event_bus.publish(OrderEvent(order=order))

        if self.paper_mode:
            mark_price = order.price or 0.0
            if mark_price <= 0:
                # Without a mark there is no honest fill price. Filling at 0.0
                # is what made paper P&L meaningless; refuse instead.
                logger.error(
                    "PAPER order for %s has no usable price — rejecting rather "
                    "than filling at zero",
                    order.symbol,
                )
                order.status = OrderStatus.REJECTED
                order.updated_at = datetime.utcnow()
                return order

            order.status = OrderStatus.FILLED
            order.filled_size = order.size
            order.avg_fill_price = self.cost_model.fill_price_for_side(
                mark_price, order.side
            )
            order.updated_at = datetime.utcnow()

            fee = self.cost_model.fee_usd(order.avg_fill_price * order.filled_size)
            logger.info(
                "PAPER order filled: %s %s %.4f @ %.4f (mark %.4f, fee $%.4f)",
                order.side.value,
                order.symbol,
                order.size,
                order.avg_fill_price,
                mark_price,
                fee,
            )
            await self.event_bus.publish(
                FillEvent(
                    order=order,
                    fill_price=order.avg_fill_price,
                    fill_size=order.filled_size,
                    fee_usd=fee,
                )
            )
            return order

        try:
            resolved = await self.exchange.place_order(order)
            self._orders[resolved.id] = resolved
            return await self._reconcile_and_publish(resolved)
        except OrderResolutionError as exc:
            self._halt_symbol(exc.symbol)
            message = (
                f"UNRESOLVED ORDER: {exc.symbol} "
                f"client_order_id={exc.client_order_id} "
                f"after {exc.polls} polls — halting trading for symbol"
            )
            logger.critical(message)
            if self._alert_callback is not None:
                await self._alert_callback(message)
            raise
        except Exception:
            logger.exception("Order rejected: %s", order.id)
            raise

    async def _reconcile_and_publish(self, order: Order) -> Order:
        """Publish fill events using actual filled quantity/price from the exchange.

        Handles full fills, partial fills (uses actual filled size), and terminal
        non-fills. Logs slippage on every fill for execution quality tracking.
        """
        order.updated_at = datetime.utcnow()

        if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            fill_price = order.avg_fill_price or order.price or 0.0
            fill_size = order.filled_size  # Always use actual filled quantity

            if fill_size <= 0:
                logger.error(
                    "Order %s marked %s but filled_size=%.4f — treating as cancelled",
                    order.id, order.status.value, fill_size,
                )
                order.status = OrderStatus.CANCELLED
                return order

            self._log_slippage(order, fill_price, fill_size)

            # Estimated, not authoritative: Delta does not return a per-order fee
            # on this endpoint. Good enough to keep P&L honest, and it uses the
            # same schedule as the backtest.
            await self.event_bus.publish(
                FillEvent(
                    order=order,
                    fill_price=fill_price,
                    fill_size=fill_size,
                    fee_usd=self.cost_model.fee_usd(fill_price * fill_size),
                )
            )
            logger.info(
                "LIVE order %s: %s %s %.0f contracts @ %.2f (requested %.0f)",
                order.status.value,
                order.side.value,
                order.symbol,
                fill_size,
                fill_price,
                order.size,
            )
            return order

        if order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            logger.warning(
                "Order terminal without fill: %s %s status=%s",
                order.id,
                order.symbol,
                order.status.value,
            )
            return order

        logger.error(
            "Order %s for %s returned non-terminal status %s after resolution",
            order.id,
            order.symbol,
            order.status.value,
        )
        raise OrderResolutionError(order.client_order_id, order.symbol, polls=0)

    def _log_slippage(self, order: Order, fill_price: float, fill_size: float) -> None:
        """Log requested vs actual fill price for execution quality measurement."""
        requested_price = order.price
        if requested_price is None or requested_price <= 0 or fill_price <= 0:
            slippage_logger.info(
                "symbol=%s side=%s strategy=%s fill_price=%.4f fill_size=%.4f "
                "requested_price=N/A slippage_bps=N/A order_id=%s",
                order.symbol, order.side.value, order.strategy_name,
                fill_price, fill_size, order.id,
            )
            return

        if order.side == OrderSide.BUY:
            slippage_bps = (fill_price - requested_price) / requested_price * 10000
        else:
            slippage_bps = (requested_price - fill_price) / requested_price * 10000

        slippage_logger.info(
            "symbol=%s side=%s strategy=%s fill_price=%.4f fill_size=%.4f "
            "requested_price=%.4f slippage_bps=%.2f order_id=%s",
            order.symbol, order.side.value, order.strategy_name,
            fill_price, fill_size, requested_price, slippage_bps, order.id,
        )

        if abs(slippage_bps) > 50:
            logger.warning(
                "High slippage on %s %s: %.2f bps (requested=%.4f, filled=%.4f)",
                order.side.value, order.symbol, slippage_bps,
                requested_price, fill_price,
            )

    async def _publish_reconciled_close(
        self,
        *,
        symbol: str,
        direction: Direction,
        size: float,
        mark_price: Optional[float],
        strategy_name: str,
        reason: str,
        signal_id: Optional[str],
    ) -> None:
        """Book a close that happened on the exchange without our order.

        The fill price is the current mark, which is an approximation — the
        exchange bracket actually filled at its trigger. An approximate trade
        record is worth far more than a silently discarded one, so this is
        tagged `<reason>_reconciled` to keep it distinguishable in analysis.
        """
        if not mark_price or mark_price <= 0:
            logger.error(
                "Cannot book reconciled close for %s — no mark price. The "
                "position will be dropped without a trade record.",
                symbol,
            )
            return

        order = Order(
            symbol=symbol,
            side=OrderSide.SELL if direction == Direction.LONG else OrderSide.BUY,
            order_type=OrderType.MARKET,
            size=size,
            price=mark_price,
            status=OrderStatus.FILLED,
            filled_size=size,
            avg_fill_price=mark_price,
            signal_id=signal_id,
            strategy_name=strategy_name,
            is_exit=True,
        )
        self._orders[order.id] = order

        logger.info(
            "Booking reconciled close for %s at mark %.4f (%s) — position was "
            "already flat on the exchange",
            symbol, mark_price, reason,
        )
        await self.event_bus.publish(
            FillEvent(
                order=order,
                fill_price=mark_price,
                fill_size=size,
                fee_usd=self.cost_model.fee_usd(mark_price * size),
            )
        )

    async def close_position_verified(
        self,
        symbol: str,
        direction: Direction,
        size: float,
        *,
        mark_price: Optional[float] = None,
        strategy_name: str = "manual",
        reason: str = "exit",
        signal_id: Optional[str] = None,
        max_retries: int = DEFAULT_CLOSE_MAX_RETRIES,
        base_interval: float = DEFAULT_CLOSE_BASE_INTERVAL,
    ) -> Optional[Order]:
        """Close a position with retries; verify flat on the exchange before returning.

        `mark_price` is required in paper mode — it is the reference the
        simulated fill is priced from. Live mode ignores it; the exchange
        reports the real fill.
        """
        symbol = symbol.upper()
        last_order: Optional[Order] = None
        last_error: Optional[Exception] = None

        if self.paper_mode:
            order = Order(
                symbol=symbol,
                side=OrderSide.SELL if direction == Direction.LONG else OrderSide.BUY,
                order_type=OrderType.MARKET,
                size=size,
                price=mark_price,
                signal_id=signal_id,
                strategy_name=strategy_name,
                is_exit=True,
            )
            self._orders[order.id] = order
            await self.event_bus.publish(OrderEvent(order=order))

            if not mark_price or mark_price <= 0:
                # This is the bug that made every paper close book a loss of
                # 100% of notional: the Order was constructed without a price,
                # so `order.price or 0.0` filled at zero and the engine closed
                # the position at 0.0.
                logger.error(
                    "PAPER close for %s has no mark price — refusing to fill at "
                    "zero (position left open)",
                    symbol,
                )
                order.status = OrderStatus.REJECTED
                order.updated_at = datetime.utcnow()
                raise PositionCloseError(symbol, "no mark price supplied", 0)

            order.status = OrderStatus.FILLED
            order.filled_size = size
            order.avg_fill_price = self.cost_model.exit_fill_price(mark_price, direction)
            order.updated_at = datetime.utcnow()

            fee = self.cost_model.fee_usd(order.avg_fill_price * size)
            logger.info(
                "PAPER close filled: %s %s %.4f @ %.4f (mark %.4f, fee $%.4f, %s)",
                order.side.value, symbol, size, order.avg_fill_price,
                mark_price, fee, reason,
            )
            await self.event_bus.publish(
                FillEvent(
                    order=order,
                    fill_price=order.avg_fill_price,
                    fill_size=order.filled_size,
                    fee_usd=fee,
                )
            )
            return order

        published_fill = False

        for attempt in range(max_retries):
            try:
                if await self.exchange.is_position_flat(symbol):
                    logger.info(
                        "Exchange confirms %s is flat (%s, attempt %d/%d)",
                        symbol,
                        reason,
                        attempt + 1,
                        max_retries,
                    )
                    if not published_fill:
                        # The position closed without us placing the order that
                        # closed it — almost always an exchange-side bracket
                        # fill. Returning here silently was one of two reasons
                        # the trades table stayed empty: no FillEvent means the
                        # engine never books the close, so the local position is
                        # dropped with no TradeRecord, no P&L and no input to
                        # the performance scorer.
                        await self._publish_reconciled_close(
                            symbol=symbol,
                            direction=direction,
                            size=size,
                            mark_price=mark_price,
                            strategy_name=strategy_name,
                            reason=reason,
                            signal_id=signal_id,
                        )
                    return last_order

                exit_order = Order(
                    symbol=symbol,
                    side=OrderSide.SELL if direction == Direction.LONG else OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    size=size,
                    signal_id=signal_id,
                    strategy_name=strategy_name,
                    is_exit=True,
                )
                self._orders[exit_order.id] = exit_order
                await self.event_bus.publish(OrderEvent(order=exit_order))

                resolved = await self.exchange.place_order(exit_order)
                self._orders[resolved.id] = resolved
                last_order = resolved

                if resolved.status == OrderStatus.FILLED:
                    last_order = await self._reconcile_and_publish(resolved)
                    published_fill = True
                elif resolved.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                    logger.warning(
                        "Close order %s for %s ended as %s; verifying exchange position",
                        resolved.id,
                        symbol,
                        resolved.status.value,
                    )

                if await self.exchange.is_position_flat(symbol):
                    logger.info(
                        "Position %s verified flat on exchange after close (%s)",
                        symbol,
                        reason,
                    )
                    return last_order

                logger.warning(
                    "Position %s still open on exchange after close attempt %d/%d",
                    symbol,
                    attempt + 1,
                    max_retries,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Close attempt %d/%d failed for %s (%s): %s",
                    attempt + 1,
                    max_retries,
                    symbol,
                    reason,
                    exc,
                    exc_info=True,
                )
                try:
                    if await self.exchange.is_position_flat(symbol):
                        logger.info(
                            "Position %s flat on exchange despite close error (%s)",
                            symbol,
                            reason,
                        )
                        return last_order
                except Exception:
                    logger.debug(
                        "Position verification failed for %s after close error",
                        symbol,
                        exc_info=True,
                    )

            if attempt < max_retries - 1:
                wait = base_interval * (2 ** attempt)
                await asyncio.sleep(wait)

        try:
            if await self.exchange.is_position_flat(symbol):
                return last_order
        except Exception:
            logger.debug("Final flat check failed for %s", symbol, exc_info=True)

        message = (
            f"CRITICAL: POSITION CLOSE FAILED for {symbol} ({reason}) "
            f"after {max_retries} attempts — HUMAN INTERVENTION REQUIRED. "
            f"Position may still be open on the exchange."
        )
        if last_error is not None:
            message = f"{message} Last error: {last_error}"

        logger.critical(message)
        self._halt_symbol(symbol)
        if self._alert_callback is not None:
            await self._alert_callback(message)

        raise PositionCloseError(symbol, reason, max_retries)

    async def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if not order:
            return False
        if self.paper_mode:
            order.status = OrderStatus.CANCELLED
            return True
        if order.exchange_order_id:
            actually_cancelled = await self.exchange.cancel_order(order.exchange_order_id)
            if actually_cancelled:
                order.status = OrderStatus.CANCELLED
            else:
                detail = await self.exchange.get_order(order.exchange_order_id)
                self.exchange._apply_exchange_order_to_order(
                    order, detail, order.size
                )
                if order.status == OrderStatus.FILLED:
                    await self._reconcile_and_publish(order)
            return actually_cancelled
        order.status = OrderStatus.CANCELLED
        return True

    def get_open_orders(self) -> list[Order]:
        return [
            o
            for o in self._orders.values()
            if o.status in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
        ]

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)
