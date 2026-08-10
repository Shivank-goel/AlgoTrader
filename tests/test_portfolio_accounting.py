"""End-to-end accounting: a paper round trip must produce an honest trade record.

Before this phase, paper trading produced:
  - closes filled at price 0.0, booking -100% of notional every time
  - zero rows in the trades table, because several close paths never emitted a
    FillEvent and _on_fill is the only caller of journal.record_trade
  - TradeRecord.fees always 0, despite the column existing and being persisted

Which in turn meant PerformanceScorer never received a single input and the
`confidence*0.7 + perf_score*0.3` blend in the engine was a constant.
"""

from __future__ import annotations

import asyncio

import pytest

from src.backtest.costs import CostModel
from src.core.events import FillEvent
from src.core.models import Direction, Order, OrderSide, OrderStatus, OrderType, Position
from src.portfolio.manager import PortfolioManager

# ---------------------------------------------------------------------------
# PortfolioManager arithmetic
# ---------------------------------------------------------------------------


@pytest.fixture
def portfolio() -> PortfolioManager:
    return PortfolioManager(initial_equity=10_000.0)


def test_round_trip_nets_to_gross_minus_fees_and_funding(portfolio):
    portfolio.open_position(
        Position(
            symbol="BTCUSD",
            side=Direction.LONG,
            entry_price=50_000.0,
            size=0.1,
            strategy_name="test",
        ),
        entry_fee=3.0,
    )

    trade = portfolio.close_position("BTCUSD", 51_000.0, exit_fee=3.06, funding=1.5)

    gross = (51_000.0 - 50_000.0) * 0.1  # 100.0
    assert trade.pnl == pytest.approx(gross - 3.0 - 3.06 - 1.5)
    assert trade.fees == pytest.approx(3.0 + 3.06 + 1.5)


def test_short_round_trip_nets_correctly(portfolio):
    portfolio.open_position(
        Position(
            symbol="ETHUSD",
            side=Direction.SHORT,
            entry_price=3_000.0,
            size=1.0,
            strategy_name="test",
        ),
        entry_fee=1.77,
    )

    trade = portfolio.close_position("ETHUSD", 2_900.0, exit_fee=1.71)
    assert trade.pnl == pytest.approx(100.0 - 1.77 - 1.71)


def test_fees_can_turn_a_marginal_winner_into_a_loser(portfolio):
    """The whole reason costs matter: a 5bps move does not survive 12bps of fees."""
    portfolio.open_position(
        Position(
            symbol="BTCUSD", side=Direction.LONG, entry_price=50_000.0, size=0.1
        ),
        entry_fee=2.95,
    )
    # +5 bps gross move.
    trade = portfolio.close_position("BTCUSD", 50_025.0, exit_fee=2.95)

    assert trade.pnl < 0, "a sub-fee move was booked as a win"


def test_equity_moves_by_net_not_gross(portfolio):
    before = portfolio.equity
    portfolio.open_position(
        Position(symbol="BTCUSD", side=Direction.LONG, entry_price=50_000.0, size=0.1),
        entry_fee=3.0,
    )
    trade = portfolio.close_position("BTCUSD", 51_000.0, exit_fee=3.0)

    assert portfolio.equity == pytest.approx(before + trade.pnl)


def test_entry_fee_does_not_leak_between_positions(portfolio):
    """A stale entry fee would be charged to whatever position came next."""
    portfolio.open_position(
        Position(symbol="BTCUSD", side=Direction.LONG, entry_price=50_000.0, size=0.1),
        entry_fee=100.0,
    )
    portfolio.close_position("BTCUSD", 50_000.0)

    portfolio.open_position(
        Position(symbol="BTCUSD", side=Direction.LONG, entry_price=50_000.0, size=0.1),
        entry_fee=0.0,
    )
    second = portfolio.close_position("BTCUSD", 50_000.0)

    assert second.pnl == pytest.approx(0.0)


def test_closing_an_unknown_symbol_is_a_no_op(portfolio):
    assert portfolio.close_position("NOSUCH", 100.0) is None


# ---------------------------------------------------------------------------
# Contract vs coin units
# ---------------------------------------------------------------------------


def test_pnl_uses_the_underlying_amount_not_the_contract_count(portfolio):
    """A Delta BTC contract is 0.001 BTC, so 100 contracts is 0.1 BTC.

    Treating `size` as coins overstated live P&L by 1/contract_value — here
    that is a factor of 1000.
    """
    portfolio.open_position(
        Position(
            symbol="BTCUSD",
            side=Direction.LONG,
            entry_price=50_000.0,
            size=100,          # contracts
            contract_value=0.001,
            strategy_name="test",
        )
    )

    trade = portfolio.close_position("BTCUSD", 51_000.0)

    # 0.1 BTC x $1000 move = $100, not $100,000.
    assert trade.pnl == pytest.approx(100.0)


def test_paper_positions_are_unaffected_by_the_contract_value_default(portfolio):
    """Paper mode sizes in coins, so contract_value must default to 1.0."""
    portfolio.open_position(
        Position(symbol="BTCUSD", side=Direction.LONG, entry_price=50_000.0, size=0.1)
    )
    trade = portfolio.close_position("BTCUSD", 51_000.0)
    assert trade.pnl == pytest.approx(100.0)


def test_unrealized_pnl_uses_the_underlying_amount(portfolio):
    portfolio.open_position(
        Position(
            symbol="BTCUSD",
            side=Direction.LONG,
            entry_price=50_000.0,
            size=100,
            contract_value=0.001,
        )
    )
    portfolio.update_prices({"BTCUSD": 51_000.0})

    assert portfolio._positions["BTCUSD"].unrealized_pnl == pytest.approx(100.0)


def test_portfolio_heat_uses_the_underlying_amount(tmp_risk_config):
    """Heat drives the risk gate; contract counts would inflate it 1000x."""
    from src.core.events import EventBus
    from src.core.models import PortfolioSnapshot
    from src.risk.circuit_breaker import CircuitBreaker
    from src.risk.manager import RiskManager
    from src.risk.position_sizer import PositionSizer

    cb = CircuitBreaker(tmp_risk_config, EventBus())
    rm = RiskManager(tmp_risk_config, PositionSizer(tmp_risk_config), cb)

    snapshot = PortfolioSnapshot(
        equity=10_000.0,
        available_balance=10_000.0,
        unrealized_pnl=0.0,
        realized_pnl_today=0.0,
        drawdown_pct=0.0,
        portfolio_heat_pct=0.0,
        open_positions=[
            Position(
                symbol="BTCUSD",
                side=Direction.LONG,
                entry_price=50_000.0,
                size=100,
                contract_value=0.001,
                stop_loss=49_000.0,
            )
        ],
    )

    # 0.1 BTC risking $1000/coin = $100 = 1% of a $10k account.
    assert rm.calculate_portfolio_heat(
        snapshot.open_positions, snapshot.equity
    ) == pytest.approx(1.0)


def test_contract_value_survives_a_restart():
    """State persistence must round-trip the unit, or restarts corrupt P&L."""
    from src.core.reconciler import PositionStateStore

    original = Position(
        symbol="BTCUSD",
        side=Direction.LONG,
        entry_price=50_000.0,
        size=100,
        contract_value=0.001,
    )
    restored = PositionStateStore.deserialize_position(
        PositionStateStore._serialize_position(original)
    )

    assert restored.contract_value == 0.001
    assert restored.underlying_size == pytest.approx(0.1)


def test_legacy_state_without_contract_value_defaults_to_one():
    from src.core.reconciler import PositionStateStore

    restored = PositionStateStore.deserialize_position(
        {"symbol": "ADAUSD", "side": "short", "entry_price": 0.1712, "size": 6.0}
    )
    assert restored.contract_value == 1.0


# ---------------------------------------------------------------------------
# Paper execution through the engine
# ---------------------------------------------------------------------------


async def _drain(bus, timeout: float = 1.0) -> None:
    """Let the event bus dispatch everything currently queued."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not bus._queue.empty() and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)


@pytest.fixture
async def paper_engine(isolated_engine):
    """Isolated engine forced into paper mode with its event bus running."""
    engine = isolated_engine
    engine.order_manager.paper_mode = True
    await engine.event_bus.start()
    try:
        yield engine
    finally:
        await engine.event_bus.stop()


async def test_paper_round_trip_produces_a_journalled_trade(paper_engine):
    """The headline regression: paper closes used to fill at 0.0."""
    engine = paper_engine
    entry_mark = 50_000.0

    entry = Order(
        symbol="BTCUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        size=0.1,
        price=entry_mark,
        stop_loss=49_000.0,
        take_profit=52_000.0,
        strategy_name="test",
    )
    await engine.order_manager.submit_order(entry)
    await _drain(engine.event_bus)

    position = engine.portfolio._positions.get("BTCUSD")
    assert position is not None
    assert position.entry_price > entry_mark, "entry slippage was not applied"

    await engine.order_manager.close_position_verified(
        symbol="BTCUSD",
        direction=Direction.LONG,
        size=position.size,
        mark_price=51_000.0,
        strategy_name="test",
        reason="take_profit",
    )
    await _drain(engine.event_bus)

    assert engine.portfolio._positions == {}

    trades = engine.journal.get_trades()
    assert len(trades) == 1, "the close was not journalled"

    trade = trades[0]
    assert trade.exit_price > 0, "close filled at zero"
    assert trade.exit_price < 51_000.0, "exit slippage was not applied to a sell"
    assert trade.fees > 0, "fees were not recorded"
    assert 0 < trade.pnl < 100.0, "P&L should be positive but below the gross move"


async def test_paper_close_without_a_mark_price_refuses_rather_than_filling_at_zero(
    paper_engine,
):
    engine = paper_engine
    engine.portfolio.open_position(
        Position(
            symbol="BTCUSD", side=Direction.LONG, entry_price=50_000.0, size=0.1
        )
    )

    from src.execution.exchange import PositionCloseError

    with pytest.raises(PositionCloseError):
        await engine.order_manager.close_position_verified(
            symbol="BTCUSD",
            direction=Direction.LONG,
            size=0.1,
            mark_price=None,
            strategy_name="test",
            reason="exit",
        )
    await _drain(engine.event_bus)

    assert "BTCUSD" in engine.portfolio._positions, "position was closed at a bogus price"
    assert engine.journal.get_trades() == []


async def test_a_losing_paper_trade_is_recorded_as_a_loss(paper_engine):
    engine = paper_engine

    entry = Order(
        symbol="BTCUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        size=0.1,
        price=50_000.0,
        strategy_name="test",
    )
    await engine.order_manager.submit_order(entry)
    await _drain(engine.event_bus)

    await engine.order_manager.close_position_verified(
        symbol="BTCUSD",
        direction=Direction.LONG,
        size=0.1,
        mark_price=49_000.0,
        strategy_name="test",
        reason="stop_loss",
    )
    await _drain(engine.event_bus)

    trade = engine.journal.get_trades()[0]
    assert trade.pnl < 0
    # A -2% move on 5000 notional is about -100, plus costs — not -100% of notional.
    assert trade.pnl > -200.0, f"loss of {trade.pnl} looks like a zero-price fill"


async def test_performance_scorer_receives_input_once_trades_are_recorded(paper_engine):
    """The downstream consequence: the adaptive loop had never had any data."""
    engine = paper_engine

    for exit_mark in (51_000.0, 49_500.0, 52_000.0):
        await engine.order_manager.submit_order(
            Order(
                symbol="BTCUSD",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                size=0.1,
                price=50_000.0,
                strategy_name="ema_crossover",
            )
        )
        await _drain(engine.event_bus)
        await engine.order_manager.close_position_verified(
            symbol="BTCUSD",
            direction=Direction.LONG,
            size=0.1,
            mark_price=exit_mark,
            strategy_name="ema_crossover",
            reason="exit",
        )
        await _drain(engine.event_bus)

    by_strategy = engine.journal.get_trades_by_strategy()
    assert "ema_crossover" in by_strategy
    assert len(by_strategy["ema_crossover"]) == 3


# ---------------------------------------------------------------------------
# Reconciled closes
# ---------------------------------------------------------------------------


async def test_exchange_side_close_is_still_booked(isolated_engine):
    """A bracket that fills on the exchange must not vanish from the journal."""
    engine = isolated_engine
    engine.order_manager.paper_mode = False
    await engine.event_bus.start()

    try:
        engine.portfolio.open_position(
            Position(
                symbol="BTCUSD",
                side=Direction.LONG,
                entry_price=50_000.0,
                size=0.1,
                strategy_name="test",
            ),
            entry_fee=3.0,
        )

        async def already_flat(_symbol: str) -> bool:
            return True

        engine.order_manager.exchange.is_position_flat = already_flat

        await engine.order_manager.close_position_verified(
            symbol="BTCUSD",
            direction=Direction.LONG,
            size=0.1,
            mark_price=51_500.0,
            strategy_name="test",
            reason="take_profit",
        )
        await _drain(engine.event_bus)

        assert engine.portfolio._positions == {}
        trades = engine.journal.get_trades()
        assert len(trades) == 1, (
            "a position closed by an exchange bracket produced no trade record"
        )
        assert trades[0].pnl > 0
    finally:
        await engine.event_bus.stop()


def test_fill_event_carries_a_fee_by_default():
    order = Order(
        symbol="BTCUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        size=1.0,
        status=OrderStatus.FILLED,
    )
    assert FillEvent(order=order, fill_price=100.0, fill_size=1.0).fee_usd == 0.0


def test_engine_and_backtester_share_one_cost_model(isolated_engine):
    """A paper result must be comparable to a backtest, so costs must match."""
    engine = isolated_engine
    assert isinstance(engine.cost_model, CostModel)
    assert engine.order_manager.cost_model is engine.cost_model
    assert engine.cost_model.taker_fee_bps == pytest.approx(5.9)
