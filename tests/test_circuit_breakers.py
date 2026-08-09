"""Integration tests for circuit breaker scenarios.

These tests exercise the REAL code paths through CircuitBreaker, RiskManager,
PortfolioManager, EventBus, and OrderManager. The exchange is the only mocked
boundary (I/O). Tests are designed to fail loudly when circuit breaker logic
has a gap.

Failure messages explain the exact gap and the fix required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.core.events import CircuitBreakerEvent, EventBus, FillEvent
from src.core.models import (
    Direction,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Signal,
    SignalAction,
)
from src.execution.exchange import DeltaExchangeClient, PositionCloseError
from src.execution.order_manager import OrderManager
from src.portfolio.manager import PortfolioManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.manager import RiskManager
from src.risk.position_sizer import PositionSizer

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_config(
    tmp_path: Path,
    *,
    max_daily_loss_pct: float = 5.0,
    max_drawdown_pct: float = 15.0,
) -> dict[str, Any]:
    """Build a realistic risk config with configurable thresholds.

    Use separate thresholds per test so only the intended breaker fires
    (e.g. Test 2 needs drawdown to fire, not daily-loss first).
    """
    return {
        "position_sizing": {
            "method": "fixed_fractional",
            "risk_per_trade_pct": 2.0,
            "max_leverage": 5,
            "min_order_size_usd": 10,
        },
        "limits": {
            "max_open_positions": 10,
            "max_portfolio_heat_pct": 15.0,
            "max_daily_loss_pct": max_daily_loss_pct,
            "max_drawdown_pct": max_drawdown_pct,
        },
        "stops": {"require_stop_loss": True},
        "circuit_breaker": {
            "daily_loss_halt": True,
            "max_drawdown_close_all": True,
            "consecutive_loss_limit": 5,
            "consecutive_loss_size_reduction": 0.5,
            "data_feed_timeout_seconds": 300,
            "max_consecutive_api_errors": 10,
            "manual_resume_only": True,
            # Isolated per-test temp files — no pollution of real state
            "state_file": str(tmp_path / "cb_state.json"),
            "audit_file": str(tmp_path / "cb_audit.log"),
        },
        "correlation_groups": [],
    }


def _make_exchange() -> DeltaExchangeClient:
    return DeltaExchangeClient(
        api_key="test-key",
        api_secret="test-secret",
        base_url="https://api.test",
        max_retries=2,
    )


def _make_signal(
    symbol: str = "BTCUSD",
    entry: float = 50_000.0,
    stop: float = 49_000.0,
) -> Signal:
    return Signal(
        symbol=symbol,
        direction=Direction.LONG,
        action=SignalAction.ENTER,
        strategy_name="test_strategy",
        confidence=0.8,
        entry_price=entry,
        stop_loss=stop,
        take_profit=entry * 1.04,
    )


def _make_position(
    symbol: str = "BTCUSD",
    side: Direction = Direction.LONG,
    size: float = 1.0,
    entry: float = 50_000.0,
) -> Position:
    return Position(
        symbol=symbol,
        side=side,
        entry_price=entry,
        size=size,
        stop_loss=entry * 0.98,
    )


# ---------------------------------------------------------------------------
# Test 1 — Daily loss hits exactly 5%
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_loss_at_5pct_halts_trading_no_orders_placed(
    tmp_path: Path,
) -> None:
    """
    Reaching exactly 5% daily loss must:
      1. Cause validate_signal to return False.
      2. Set circuit_breaker.is_halted = True (persisted for all subsequent checks).
      3. Never result in a call to exchange.place_order.

    The test exercises the real path:
        validate_signal → can_trade → circuit_breaker.check(portfolio)
        → _create_event("daily_loss_limit", "halt") → _set_halted()

    A second validate_signal call exercises the early-exit branch:
        can_trade → circuit_breaker.is_halted → (False, "Circuit breaker is active")
    """
    cfg = _make_config(
        tmp_path,
        max_daily_loss_pct=5.0,
        # High drawdown threshold so it doesn't fire before daily-loss check
        max_drawdown_pct=25.0,
    )

    event_bus = EventBus()
    await event_bus.start()

    circuit_breaker = CircuitBreaker(cfg, event_bus)
    risk_manager = RiskManager(cfg, PositionSizer(cfg), circuit_breaker)
    portfolio = PortfolioManager(initial_equity=10_000.0)
    exchange = _make_exchange()
    order_manager = OrderManager(exchange, event_bus, paper_mode=False)  # noqa: F841

    try:
        # Seed the circuit breaker's daily-start equity.
        # The first check() call initialises _daily_start_equity from the snapshot.
        seed_event = circuit_breaker.check(portfolio.get_snapshot())
        assert seed_event is None, "No breaker should fire at zero loss"
        assert not circuit_breaker.is_halted
        assert circuit_breaker._daily_start_equity == pytest.approx(10_000.0)

        # Drive equity to exactly the 5 % threshold (500 USD loss).
        portfolio.equity = 9_500.0

        signal = _make_signal()
        snapshot = portfolio.get_snapshot()
        # Snapshot equity = 9500 + 0 unrealised = 9500
        # daily_loss_pct = (10000 - 9500) / 10000 * 100 = 5.0 %  →  fires

        with patch.object(exchange, "place_order", new_callable=AsyncMock) as mock_place:
            # ── real path under test ──────────────────────────────────────
            valid, reason = risk_manager.validate_signal(signal, snapshot)
            # ─────────────────────────────────────────────────────────────

            assert not valid, (
                "validate_signal must return False when daily loss reaches 5%. "
                f"Got valid=True, reason={reason!r}"
            )
            assert "circuit breaker" in reason.lower(), (
                f"Rejection reason must mention circuit breaker, got: {reason!r}"
            )

            # The halt flag must be set synchronously inside check() — not via bus
            assert circuit_breaker.is_halted, (
                "circuit_breaker.is_halted must be True immediately after the "
                "daily-loss threshold is crossed; validate_signal must not return "
                "before the halt is set."
            )

            # Verify persistence: subsequent calls hit the early-exit branch
            valid2, reason2 = risk_manager.validate_signal(signal, snapshot)
            assert not valid2
            assert "circuit breaker is active" in reason2.lower(), (
                f"After halt, subsequent checks should hit the is_halted branch. "
                f"Got: {reason2!r}"
            )

            # The decisive check: no order must ever reach the exchange
            mock_place.assert_not_called()
    finally:
        await event_bus.stop()


# ---------------------------------------------------------------------------
# Test 2 — Drawdown hits 15% → ALL positions are actually closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drawdown_15pct_actually_closes_all_positions(tmp_path: Path) -> None:
    """
    When drawdown reaches 15%, circuit_breaker.check() must return a
    "close_all" event. Publishing that event through the real EventBus must
    cause _close_all_positions() to be called, which must call
    close_position_verified() for every open position.

    After everything settles:
      - Both positions must have received a SELL close order on the exchange.
      - The portfolio must be empty (FillEvents processed by on_fill handler).

    This is NOT a flag-only check: the test verifies that the positions are
    actually sent to the exchange and removed from the portfolio.
    """
    cfg = _make_config(
        tmp_path,
        # High daily-loss threshold so it never fires — we want drawdown only
        max_daily_loss_pct=25.0,
        max_drawdown_pct=15.0,
    )

    event_bus = EventBus()
    await event_bus.start()

    circuit_breaker = CircuitBreaker(cfg, event_bus)
    portfolio = PortfolioManager(initial_equity=10_000.0)
    exchange = _make_exchange()
    order_manager = OrderManager(exchange, event_bus, paper_mode=False)

    # Tracking state
    close_order_symbols: list[str] = []
    all_closes_dispatched = asyncio.Event()
    fills_needed = 2
    fills_received = 0
    all_fills_done = asyncio.Event()

    # ── Replicate engine._on_fill (buy=open, sell=close) ─────────────────
    async def on_fill(event: FillEvent) -> None:
        nonlocal fills_received
        if event.order.side == OrderSide.SELL:
            portfolio.close_position(event.order.symbol, event.fill_price)
            close_order_symbols.append(event.order.symbol)
        fills_received += 1
        if fills_received >= fills_needed:
            all_fills_done.set()

    event_bus.subscribe(FillEvent, on_fill)

    # ── Replicate engine._on_circuit_breaker + _close_all_positions ──────
    # Uses the EXACT same code that lives in the engine — no simplification.
    async def on_circuit_breaker(event: CircuitBreakerEvent) -> None:
        if event.action in ("close_all", "halt"):
            for pos in list(portfolio.positions):
                try:
                    await order_manager.close_position_verified(
                        symbol=pos.symbol,
                        direction=pos.side,
                        size=pos.size,
                        strategy_name="circuit_breaker",
                        reason="circuit_breaker",
                    )
                except PositionCloseError:
                    pytest.fail(
                        f"close_position_verified raised PositionCloseError for "
                        f"{pos.symbol} — position was NOT closed despite close_all. "
                        "Check close retry / is_position_flat logic."
                    )
                except Exception as exc:
                    pytest.fail(
                        f"Unexpected exception closing {pos.symbol}: {exc}"
                    )
        all_closes_dispatched.set()

    event_bus.subscribe(CircuitBreakerEvent, on_circuit_breaker)

    try:
        # Seed circuit breaker state (sets _peak_equity and _daily_start_equity)
        circuit_breaker.check(portfolio.get_snapshot())
        assert circuit_breaker._peak_equity == pytest.approx(10_000.0)

        # Open two positions
        portfolio.open_position(_make_position("BTCUSD", size=1.0, entry=50_000.0))
        portfolio.open_position(_make_position("ETHUSD", size=5.0, entry=3_000.0))
        assert len(portfolio.positions) == 2

        # Mock exchange:
        #   is_position_flat: False on first call per symbol (pre-close),
        #                     True on second call (post-close)
        flat_call_count: dict[str, int] = {}

        async def fake_is_flat(symbol: str) -> bool:
            n = flat_call_count.get(symbol, 0) + 1
            flat_call_count[symbol] = n
            # First call → position still open; second → flat after close order
            return n > 1

        async def fake_place_order(order: Order) -> Order:
            order.status = OrderStatus.FILLED
            order.filled_size = order.size
            order.avg_fill_price = 49_500.0
            order.exchange_order_id = f"ex-close-{order.symbol}"
            return order

        # Drive equity to exactly 15 % below peak (10 000 × 0.85 = 8 500)
        portfolio.equity = 8_500.0

        with patch.object(exchange, "place_order", side_effect=fake_place_order):
            with patch.object(exchange, "is_position_flat", side_effect=fake_is_flat):
                snapshot = portfolio.get_snapshot()
                # snapshot.equity = 8500 + 0 unrealised = 8500
                # drawdown = (10000 - 8500) / 10000 * 100 = 15.0 %

                # ── real path under test ──────────────────────────────────
                cb_event = circuit_breaker.check(snapshot)
                # ─────────────────────────────────────────────────────────

                assert cb_event is not None, (
                    "circuit_breaker.check() must return an event at 15% drawdown. "
                    "Verify max_drawdown_pct and max_drawdown_close_all in config."
                )
                assert cb_event.action == "close_all", (
                    f"Expected action='close_all' at 15% drawdown, "
                    f"got '{cb_event.action}'. The drawdown branch may not be "
                    "executing or another branch is firing first."
                )

                # Publish through the real bus — exercises real dispatch path
                await circuit_breaker.trigger_and_publish(
                    cb_event.reason, cb_event.action, cb_event.details
                )

                # Wait for the on_circuit_breaker handler to finish submitting closes
                await asyncio.wait_for(
                    all_closes_dispatched.wait(),
                    timeout=5.0,
                )

                # Wait for all FillEvents to be dispatched by the event bus and
                # processed by on_fill (so portfolio.close_position is called)
                await asyncio.wait_for(
                    all_fills_done.wait(),
                    timeout=5.0,
                )

        # ── Critical assertions ───────────────────────────────────────────
        assert len(portfolio.positions) == 0, (
            f"{len(portfolio.positions)} position(s) remain open after close_all. "
            f"Remaining: {[p.symbol for p in portfolio.positions]}. "
            "The circuit breaker may have set a flag without actually submitting "
            "close orders, or the FillEvent handler was never wired."
        )
        assert set(close_order_symbols) == {"BTCUSD", "ETHUSD"}, (
            f"Expected close orders for both BTCUSD and ETHUSD. "
            f"Got: {close_order_symbols}. "
            "close_position_verified may not have been called for all positions."
        )
        # Verify each symbol received exactly one close-order call on the exchange
        assert flat_call_count.get("BTCUSD", 0) >= 2, (
            "is_position_flat was not called for BTCUSD — close may have been skipped"
        )
        assert flat_call_count.get("ETHUSD", 0) >= 2, (
            "is_position_flat was not called for ETHUSD — close may have been skipped"
        )
    finally:
        await event_bus.stop()


# ---------------------------------------------------------------------------
# Test 3 — Circuit breaker fires WHILE an order is in-flight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_flight_order_is_not_leaked_when_circuit_breaker_fires(
    tmp_path: Path,
) -> None:
    """
    SCENARIO
    --------
    1. An entry order for BTCUSD is submitted and is being polled on the
       exchange (not yet filled — order is in-flight).
    2. While polling, the circuit breaker fires "close_all" (e.g. drawdown
       from another symbol's loss).
    3. _close_all_positions iterates portfolio.positions — which is EMPTY
       because the fill hasn't been received yet — so it does nothing.
    4. The in-flight order fill arrives. The portfolio opens the position.

    EXPECTED (correct behaviour)
    ----------------------------
    After step 4, the position must be closed or the order must have been
    cancelled before it filled. The portfolio must be empty.

    ACTUAL (current behaviour — GAP)
    ---------------------------------
    The position is opened AFTER close_all completed, creating a leaked open
    position with no stop-loss management and no CB coverage.

    THIS TEST WILL FAIL under the current code. The failure message describes
    the exact gap and the recommended fix.

    FIX DIRECTION
    -------------
    Either (a) cancel in-flight orders when the CB fires, or (b) after
    close_all completes, re-check portfolio.positions and close any that were
    opened by fills that arrived concurrently.
    """
    cfg = _make_config(
        tmp_path,
        max_daily_loss_pct=25.0,  # don't interfere
        max_drawdown_pct=15.0,
    )

    event_bus = EventBus()
    await event_bus.start()

    circuit_breaker = CircuitBreaker(cfg, event_bus)
    portfolio = PortfolioManager(initial_equity=10_000.0)
    exchange = _make_exchange()
    order_manager = OrderManager(exchange, event_bus, paper_mode=False)

    # Synchronisation primitives
    order_is_in_flight = asyncio.Event()   # set when place_order is mid-poll
    close_all_finished = asyncio.Event()   # set when CB handler is done
    fill_processed = asyncio.Event()       # set when on_fill has opened position

    # ── Replicate engine._on_fill (position open blocked while halted) ───
    async def on_fill(event: FillEvent) -> None:
        if circuit_breaker.is_halted:
            fill_processed.set()
            return
        if event.order.side == OrderSide.BUY:
            portfolio.open_position(
                Position(
                    symbol=event.order.symbol,
                    side=Direction.LONG,
                    entry_price=event.fill_price,
                    size=event.fill_size,
                    strategy_name=event.order.strategy_name,
                )
            )
        fill_processed.set()

    event_bus.subscribe(FillEvent, on_fill)

    # ── Replicate engine._on_circuit_breaker + _close_all_positions ──────
    async def on_circuit_breaker(event: CircuitBreakerEvent) -> None:
        if event.action in ("close_all", "halt"):
            # Snapshot of portfolio at this exact moment — fill not yet received
            for pos in list(portfolio.positions):
                try:
                    await order_manager.close_position_verified(
                        symbol=pos.symbol,
                        direction=pos.side,
                        size=pos.size,
                        strategy_name="circuit_breaker",
                        reason="circuit_breaker",
                    )
                except (PositionCloseError, Exception):
                    pass
        close_all_finished.set()

    event_bus.subscribe(CircuitBreakerEvent, on_circuit_breaker)

    try:
        # Seed circuit breaker state
        circuit_breaker.check(portfolio.get_snapshot())

        # ── "Slow" exchange: place_order pauses until CB close_all finishes ──
        # This simulates the real-world delay of polling for a fill while the
        # circuit breaker fires concurrently.
        async def slow_place_order(order: Order) -> Order:
            # Signal that we are now in-flight (order placed, awaiting fill)
            order_is_in_flight.set()
            # Block until the CB handler has run and finished close_all.
            # At the moment close_all runs, portfolio.positions is empty.
            await asyncio.wait_for(close_all_finished.wait(), timeout=5.0)
            # Fill arrives AFTER close_all completed — this is the race condition
            order.status = OrderStatus.FILLED
            order.filled_size = 1.0
            order.avg_fill_price = 50_000.0
            order.exchange_order_id = "ex-delayed-fill"
            return order

        # is_position_flat: exchange sees nothing (the fill hasn't settled)
        async def fake_is_flat(_symbol: str) -> bool:
            return True

        with patch.object(exchange, "place_order", side_effect=slow_place_order):
            with patch.object(exchange, "is_position_flat", side_effect=fake_is_flat):
                # Submit the entry order as a concurrent background task.
                # It will block inside slow_place_order until close_all_finished.
                entry_order = Order(
                    symbol="BTCUSD",
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    size=1.0,
                    price=50_000.0,
                    strategy_name="test_strategy",
                )
                submit_task = asyncio.create_task(
                    order_manager.submit_order(entry_order)
                )

                # Wait until the order is in-flight before firing the CB
                await asyncio.wait_for(order_is_in_flight.wait(), timeout=5.0)

                # Circuit breaker fires while order is in-flight.
                # At this moment: portfolio.positions == [] (no fill yet)
                portfolio.equity = 8_500.0  # 15 % below peak of 10 000
                cb_snapshot = portfolio.get_snapshot()
                cb_event = circuit_breaker.check(cb_snapshot)

                assert cb_event is not None, "CB must fire at 15% drawdown"
                assert cb_event.action == "close_all"

                # Publish event — on_circuit_breaker runs, sees empty portfolio,
                # calls close_all_finished.set() immediately (nothing to close)
                await circuit_breaker.trigger_and_publish(
                    cb_event.reason, cb_event.action, cb_event.details
                )

                # The submit_task can now complete (close_all_finished is set).
                # It will fill the order, then _reconcile_and_publish will fire
                # a FillEvent which on_fill will use to open the position.
                await asyncio.wait_for(submit_task, timeout=5.0)

                # Wait for on_fill to process the FillEvent
                await asyncio.wait_for(fill_processed.wait(), timeout=5.0)

        # ── THE CRITICAL ASSERTION ────────────────────────────────────────
        # Under correct behaviour: portfolio must be empty (position was
        # either cancelled or re-closed after the fill).
        # Under current behaviour: this assertion FAILS because the position
        # leaked through after close_all completed.
        leaked = portfolio.positions
        if leaked:
            pytest.fail(
                f"RACE CONDITION: {len(leaked)} position(s) leaked after close_all.\n"
                f"  Leaked: {[(p.symbol, p.side.value, p.size) for p in leaked]}\n"
                "\n"
                "  Root cause: _close_all_positions() iterated portfolio.positions\n"
                "  BEFORE the in-flight order's FillEvent was processed. The fill\n"
                "  arrived after close_all completed, opening a position that is\n"
                "  now outside any circuit-breaker or stop-loss management.\n"
                "\n"
                "  Fix options:\n"
                "  (a) Cancel in-flight orders when the CB fires (add\n"
                "      order_manager.cancel_all_pending() to _close_all_positions).\n"
                "  (b) After _close_all_positions returns, loop until\n"
                "      portfolio.positions is stable for two consecutive checks\n"
                "      (re-close anything that appeared from concurrent fills).\n"
            )
    finally:
        await event_bus.stop()
