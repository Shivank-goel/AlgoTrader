"""Tests for startup reconciliation logic."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from src.core.models import Direction, Position
from src.core.reconciler import (
    PositionStateStore,
    ReconciliationResult,
    StartupReconciler,
)


@pytest.fixture
def tmp_state_file(tmp_path: Path) -> str:
    return str(tmp_path / "position_state.json")


@pytest.fixture
def state_store(tmp_state_file: str) -> PositionStateStore:
    return PositionStateStore(path=tmp_state_file)


def _make_position(
    symbol: str = "BTCUSD",
    side: Direction = Direction.LONG,
    size: float = 10.0,
    entry_price: float = 50000.0,
    stop_loss: Optional[float] = 49000.0,
    take_profit: Optional[float] = 55000.0,
    strategy_name: str = "ema_crossover",
) -> Position:
    return Position(
        symbol=symbol,
        side=side,
        size=size,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        strategy_name=strategy_name,
    )


class TestPositionStateStore:
    def test_save_and_load_roundtrip(self, state_store: PositionStateStore) -> None:
        pos = _make_position()
        state_store.save({"BTCUSD": pos})

        loaded = state_store.load()
        assert "BTCUSD" in loaded
        assert loaded["BTCUSD"]["side"] == "long"
        assert loaded["BTCUSD"]["size"] == 10.0
        assert loaded["BTCUSD"]["entry_price"] == 50000.0
        assert loaded["BTCUSD"]["stop_loss"] == 49000.0
        assert loaded["BTCUSD"]["strategy_name"] == "ema_crossover"

    def test_load_missing_file(self, tmp_path: Path) -> None:
        store = PositionStateStore(path=str(tmp_path / "nonexistent.json"))
        assert store.load() == {}

    def test_clear_removes_file(self, state_store: PositionStateStore) -> None:
        pos = _make_position()
        state_store.save({"BTCUSD": pos})
        state_store.clear()
        assert state_store.load() == {}

    def test_deserialize_position(self) -> None:
        data = {
            "symbol": "ETHUSD",
            "side": "short",
            "entry_price": 3000.0,
            "size": 5.0,
            "stop_loss": 3200.0,
            "take_profit": 2700.0,
            "strategy_name": "rsi_reversion",
            "opened_at": "2026-07-01T12:00:00",
            "unrealized_pnl": -50.0,
        }
        pos = PositionStateStore.deserialize_position(data)
        assert pos.symbol == "ETHUSD"
        assert pos.side == Direction.SHORT
        assert pos.size == 5.0
        assert pos.stop_loss == 3200.0


class TestStartupReconciler:
    def _mock_exchange(
        self,
        positions: list[Position],
        open_orders: list[dict[str, Any]] | None = None,
    ) -> AsyncMock:
        exchange = AsyncMock()
        exchange.get_all_positions = AsyncMock(return_value=positions)
        exchange.cancel_order = AsyncMock(return_value=True)

        async def mock_request(method, path, params=None, auth=False):
            if path == "/v2/orders" and params and params.get("state") == "open":
                return open_orders or []
            return []

        exchange._request = mock_request
        return exchange

    @pytest.mark.asyncio
    async def test_clean_start_no_state_no_exchange(
        self, state_store: PositionStateStore
    ) -> None:
        exchange = self._mock_exchange(positions=[])
        reconciler = StartupReconciler(exchange, state_store)

        result = await reconciler.reconcile()
        assert result.success
        assert not result.has_critical
        assert result.adopted_positions == {}

    @pytest.mark.asyncio
    async def test_orphan_on_exchange_no_local_state(
        self, state_store: PositionStateStore
    ) -> None:
        ex_pos = _make_position(symbol="BTCUSD", size=10.0)
        exchange = self._mock_exchange(positions=[ex_pos])
        reconciler = StartupReconciler(exchange, state_store)

        result = await reconciler.reconcile()
        assert result.success
        assert result.has_critical
        assert len(result.discrepancies) == 1
        assert result.discrepancies[0].kind == "orphan_on_exchange"
        assert "BTCUSD" in result.adopted_positions

    @pytest.mark.asyncio
    async def test_matching_state_no_discrepancy(
        self, state_store: PositionStateStore
    ) -> None:
        local_pos = _make_position(symbol="BTCUSD", size=10.0, stop_loss=49000.0)
        state_store.save({"BTCUSD": local_pos})

        ex_pos = _make_position(symbol="BTCUSD", size=10.0, stop_loss=None)
        exchange = self._mock_exchange(positions=[ex_pos])
        reconciler = StartupReconciler(exchange, state_store)

        result = await reconciler.reconcile()
        assert result.success
        assert not result.has_critical
        # Local metadata (SL/TP) should be preserved
        adopted = result.adopted_positions["BTCUSD"]
        assert adopted.stop_loss == 49000.0

    @pytest.mark.asyncio
    async def test_missing_from_exchange_triggers_critical(
        self, state_store: PositionStateStore
    ) -> None:
        local_pos = _make_position(symbol="BTCUSD", size=10.0)
        state_store.save({"BTCUSD": local_pos})

        exchange = self._mock_exchange(positions=[])
        reconciler = StartupReconciler(exchange, state_store)

        result = await reconciler.reconcile()
        assert result.success
        assert result.has_critical
        assert result.discrepancies[0].kind == "missing_from_exchange"

    @pytest.mark.asyncio
    async def test_size_mismatch_triggers_critical(
        self, state_store: PositionStateStore
    ) -> None:
        local_pos = _make_position(symbol="BTCUSD", size=10.0)
        state_store.save({"BTCUSD": local_pos})

        ex_pos = _make_position(symbol="BTCUSD", size=20.0)
        exchange = self._mock_exchange(positions=[ex_pos])
        reconciler = StartupReconciler(exchange, state_store)

        result = await reconciler.reconcile()
        assert result.has_critical
        assert result.discrepancies[0].kind == "size_mismatch"

    @pytest.mark.asyncio
    async def test_side_mismatch_triggers_critical(
        self, state_store: PositionStateStore
    ) -> None:
        local_pos = _make_position(symbol="BTCUSD", side=Direction.LONG)
        state_store.save({"BTCUSD": local_pos})

        ex_pos = _make_position(symbol="BTCUSD", side=Direction.SHORT)
        exchange = self._mock_exchange(positions=[ex_pos])
        reconciler = StartupReconciler(exchange, state_store)

        result = await reconciler.reconcile()
        assert result.has_critical
        assert result.discrepancies[0].kind == "side_mismatch"

    @pytest.mark.asyncio
    async def test_stale_orders_cancelled(
        self, state_store: PositionStateStore
    ) -> None:
        open_orders = [{"id": "12345"}, {"id": "67890"}]
        exchange = self._mock_exchange(positions=[], open_orders=open_orders)
        reconciler = StartupReconciler(exchange, state_store)

        result = await reconciler.reconcile()
        assert result.success
        assert len(result.cancelled_orders) == 2
        assert "12345" in result.cancelled_orders

    @pytest.mark.asyncio
    async def test_exchange_fetch_failure_halts(
        self, state_store: PositionStateStore
    ) -> None:
        exchange = AsyncMock()
        exchange.get_all_positions = AsyncMock(side_effect=Exception("connection timeout"))
        reconciler = StartupReconciler(exchange, state_store)

        result = await reconciler.reconcile()
        assert not result.success
        assert "Cannot fetch exchange positions" in result.message

    @pytest.mark.asyncio
    async def test_small_size_difference_within_tolerance(
        self, state_store: PositionStateStore
    ) -> None:
        """Size diff within 5% tolerance should not trigger critical."""
        local_pos = _make_position(symbol="BTCUSD", size=10.0)
        state_store.save({"BTCUSD": local_pos})

        ex_pos = _make_position(symbol="BTCUSD", size=10.4)  # 4% diff
        exchange = self._mock_exchange(positions=[ex_pos])
        reconciler = StartupReconciler(exchange, state_store)

        result = await reconciler.reconcile()
        assert not result.has_critical
