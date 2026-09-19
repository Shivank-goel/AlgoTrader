from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock

import pytest

from src.execution.fyers import FyersClient
from src.fyers.journal import Journal
from src.fyers.models import Instrument, Intent, Quote, Side
from src.fyers.orders import AmbiguousOrder, FyersOrderGateway, LiveOrdersDisabled


@pytest.fixture
def gateway(tmp_path):
    journal = Journal(tmp_path / "orders.db")
    client = FyersClient("app", "secret", base_url="https://api-t1.fyers.in/api/v3")
    gateway = FyersOrderGateway(client, journal)
    gateway._post = AsyncMock(return_value={"s": "ok", "id": "broker1"})
    yield gateway
    journal.close()


async def submit(gateway):
    request = Intent(intent_id="stable", strategy="test", symbol="NSE:SBIN-EQ", side=Side.BUY,
                     quantity=2, created_at=datetime.fromtimestamp(1000, timezone.utc))
    instrument = Instrument(symbol=request.symbol, isin="ISIN", lot_size=1, tick_size="0.1")
    quote = Quote(symbol=request.symbol, bid=99, ask=100, bid_size=10, ask_size=10,
                  received_time=1000, exchange_time=1000)
    return await gateway.place_limit(request, instrument, quote, limit_price=100,
                                    max_notional=1000, stale_seconds=30, now=1001)


@pytest.mark.asyncio
async def test_live_orders_disabled_by_default(gateway):
    with pytest.raises(LiveOrdersDisabled):
        await submit(gateway)
    gateway._post.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_submission_is_local_only(gateway):
    gateway.enabled = True
    first = await submit(gateway)
    assert await submit(gateway) == first
    gateway._post.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_persists_unknown_and_never_retries(gateway):
    gateway.enabled = True
    gateway._post.side_effect = AmbiguousOrder("timeout")
    with pytest.raises(AmbiguousOrder):
        await submit(gateway)
    assert gateway.journal.db.execute("SELECT status FROM live_orders").fetchone()[0] == "UNKNOWN"
    restarted = FyersOrderGateway(gateway.client, gateway.journal, enable=True)
    restarted._post = AsyncMock()
    with pytest.raises(AmbiguousOrder):
        await submit(restarted)
    restarted._post.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_partial_fill_then_final_without_double_counting(gateway):
    gateway.enabled = True
    await submit(gateway)
    row = {"id": "broker1", "symbol": "NSE:SBIN-EQ", "qty": 2, "side": 1, "filledQty": 1, "status": 6}
    gateway.client.get_orders = AsyncMock(return_value={"s": "ok", "orderBook": [row]})
    gateway.client.get_trades = AsyncMock(return_value={"s": "ok", "tradeBook": []})
    assert (await gateway.reconcile())[0]["filled"] == 1
    assert (await gateway.reconcile())[0]["filled"] == 1
    row.update(filledQty=2, status=2)
    result = (await gateway.reconcile())[0]
    assert result["status"] == "FILLED" and result["filled"] == 2


@pytest.mark.asyncio
async def test_missing_order_does_not_mean_safe_to_resubmit(gateway):
    gateway.enabled = True
    await submit(gateway)
    gateway.client.get_orders = AsyncMock(return_value={"s": "ok", "orderBook": []})
    gateway.client.get_trades = AsyncMock(return_value={"s": "ok", "tradeBook": []})
    with pytest.raises(AmbiguousOrder):
        await gateway.reconcile()
    assert gateway.journal.db.execute("SELECT status FROM live_orders").fetchone()[0] == "ACKNOWLEDGED"


@pytest.mark.asyncio
async def test_trade_ids_are_deduplicated_during_reconciliation(gateway):
    gateway.enabled = True
    await submit(gateway)
    row = {"id": "broker1", "symbol": "NSE:SBIN-EQ", "qty": 2, "side": 1,
           "filledQty": 1, "status": 6}
    trade = {"tradeId": "trade1", "orderNumber": "broker1", "tradedQty": 1, "tradePrice": 100}
    gateway.client.get_orders = AsyncMock(return_value={"s": "ok", "orderBook": [row]})
    gateway.client.get_trades = AsyncMock(return_value={"s": "ok", "tradeBook": [trade]})
    await gateway.reconcile()
    await gateway.reconcile()
    assert gateway.journal.db.execute("SELECT COUNT(*) FROM broker_trades").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_cancel_and_modify_are_disabled_then_audited(gateway):
    with pytest.raises(LiveOrdersDisabled):
        await gateway.cancel("stable")
    gateway.enabled = True
    await submit(gateway)
    gateway._mutate = AsyncMock(return_value={"s": "ok"})
    modified = await gateway.modify_limit("stable", quantity=2, limit_price=99.5)
    assert gateway._mutate.await_args.args[:2] == ("PATCH", "orders/sync")
    assert json.loads(modified["payload"])["limitPrice"] == 99.5
    assert gateway.journal.db.execute("SELECT COUNT(*) FROM order_revisions").fetchone()[0] == 1
    cancelled = await gateway.cancel("stable")
    assert gateway._mutate.await_args.args[:2] == ("DELETE", "orders/sync")
    assert cancelled["status"] == "CANCEL_REQUESTED"


@pytest.mark.asyncio
async def test_full_account_reconciliation_blocks_position_divergence(gateway):
    gateway.client.get_positions = AsyncMock(return_value={"netPositions": [
        {"symbol": "NSE:SBIN-EQ", "netQty": 1}]})
    gateway.client.get_holdings = AsyncMock(return_value={"holdings": [
        {"symbol": "NSE:INFY-EQ", "quantity": 2}]})
    gateway.client.get_funds = AsyncMock(return_value={"fund_limit": [
        {"id": 10, "equityAmount": 5000.0}]})
    result = await gateway.reconcile_account(intended={"NSE:SBIN-EQ": 1},
                                             local={"NSE:SBIN-EQ": 1})
    assert result["entry_blocked"]
    assert result["available_cash"] == 5000
    assert gateway.journal.get("halt_reason") == "broker position reconciliation mismatch"
