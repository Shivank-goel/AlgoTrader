"""Fail-closed limit-order transport for future deployment, not enabled by CLI.

This boundary preserves ambiguous intents across restarts. FYERS order tags are
correlation labels, not a documented broker-side deduplication guarantee.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from decimal import Decimal
import time

import aiohttp

from src.execution.fyers import FyersClient, FyersGatewayError
from src.execution.reconciliation import PositionViews
from src.execution.state_machine import validate_transition
from src.fyers.journal import Journal
from src.fyers.models import Instrument, Intent, Quote, Side


class LiveOrdersDisabled(FyersGatewayError):
    pass


class AmbiguousOrder(FyersGatewayError):
    pass


class FyersOrderGateway:
    """One outstanding intent at a time; no automatic resubmission after timeout.

    enable=False is deliberate. Caller must implement and verify production risk,
    portfolio reconciliation and deployment approval before enabling this adapter.
    No production caller currently enables it.
    """

    def __init__(self, client: FyersClient, journal: Journal, *, enable: bool = False,
                 requests_per_second: float = 5) -> None:
        if type(enable) is not bool:
            raise ValueError("Order transport enable must be an explicit boolean")
        if not math.isfinite(requests_per_second) or not 0 < requests_per_second <= 20:
            raise ValueError("Invalid order transport rate")
        self.client, self.journal, self.enabled = client, journal, enable
        self._request_interval = 1 / requests_per_second
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0
        journal.db.execute("""CREATE TABLE IF NOT EXISTS live_orders (
            intent_id TEXT PRIMARY KEY, tag TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
            broker_id TEXT, status TEXT NOT NULL, filled INTEGER NOT NULL DEFAULT 0)""")
        journal.db.execute("""CREATE TABLE IF NOT EXISTS order_transitions (
            id INTEGER PRIMARY KEY, intent_id TEXT NOT NULL, previous TEXT NOT NULL,
            current TEXT NOT NULL, filled INTEGER NOT NULL, observed REAL NOT NULL)""")
        journal.db.execute("""CREATE TABLE IF NOT EXISTS broker_trades (
            trade_id TEXT PRIMARY KEY, broker_order_id TEXT NOT NULL,
            quantity INTEGER NOT NULL, price REAL NOT NULL, observed REAL NOT NULL)""")
        journal.db.execute("""CREATE TABLE IF NOT EXISTS order_revisions (
            id INTEGER PRIMARY KEY, intent_id TEXT NOT NULL, previous_payload TEXT NOT NULL,
            current_payload TEXT NOT NULL, observed REAL NOT NULL)""")
        journal.db.execute("UPDATE live_orders SET status=CASE WHEN filled>0 THEN 'PARTIALLY_FILLED' ELSE 'ACKNOWLEDGED' END WHERE status='PENDING'")
        journal.db.commit()

    def _transition(self, intent_id: str, status: str, filled: int, broker_id: str | None = None) -> None:
        db = self.journal.db
        row = db.execute("SELECT * FROM live_orders WHERE intent_id=?", (intent_id,)).fetchone()
        quantity = json.loads(row["payload"])["qty"]
        validate_transition(row["status"], status, quantity=quantity, previous_filled=row["filled"], filled=filled)
        if (row["status"], row["filled"]) != (status, filled):
            db.execute("INSERT INTO order_transitions(intent_id,previous,current,filled,observed) VALUES(?,?,?,?,?)",
                       (intent_id, row["status"], status, filled, time.time()))
        db.execute("UPDATE live_orders SET status=?,filled=?,broker_id=COALESCE(?,broker_id) WHERE intent_id=?",
                   (status, filled, broker_id, intent_id))

    async def place_limit(self, intent: Intent, instrument: Instrument, quote: Quote,
                          *, limit_price: float, max_notional: float,
                          stale_seconds: float, now: float | None = None) -> dict:
        if not self.enabled:
            raise LiveOrdersDisabled("FYERS live orders are disabled")
        if self.journal.get("halt_reason"):
            raise LiveOrdersDisabled("Trading is halted")
        now = time.time() if now is None else now
        intent_age = now - intent.created_at.timestamp()
        if intent.created_at.tzinfo is None or not -0.001 <= intent_age <= stale_seconds:
            raise ValueError("Intent is stale, future or missing timezone")
        if instrument.symbol != intent.symbol or quote.symbol != intent.symbol or intent.quantity % instrument.lot_size:
            raise ValueError("Instrument or lot mismatch")
        if not quote.usable(now, stale_seconds):
            raise ValueError("Quote is not executable")
        if not all(math.isfinite(x) and x > 0 for x in (limit_price, max_notional)):
            raise ValueError("Invalid price or notional limit")
        if limit_price * intent.quantity > max_notional or Decimal(str(limit_price)) % instrument.tick_size:
            raise ValueError("Price tick or notional limit violated")
        tag = hashlib.sha256(intent.intent_id.encode()).hexdigest()[:20]
        payload = {"symbol": intent.symbol, "qty": intent.quantity, "type": 1,
                   "side": 1 if intent.side == Side.BUY else -1, "productType": "CNC",
                   "limitPrice": limit_price, "stopPrice": 0, "validity": "DAY",
                   "disclosedQty": 0, "offlineOrder": False, "stopLoss": 0,
                   "takeProfit": 0, "orderTag": tag}
        encoded = json.dumps(payload, sort_keys=True)
        db = self.journal.db
        db.execute("BEGIN IMMEDIATE")
        try:
            old = db.execute("SELECT * FROM live_orders WHERE intent_id=?", (intent.intent_id,)).fetchone()
            if old:
                if old["payload"] != encoded:
                    raise ValueError("Intent ID reused with changed order")
                db.rollback()
                if old["status"] in {"SUBMITTING", "UNKNOWN"}:
                    raise AmbiguousOrder("Reconcile existing intent; never resubmit it")
                return dict(old)
            if db.execute("SELECT 1 FROM live_orders WHERE status NOT IN ('FILLED','CANCELLED','REJECTED','EXPIRED')").fetchone():
                raise AmbiguousOrder("An outstanding order requires reconciliation")
            db.execute("INSERT INTO live_orders(intent_id,tag,payload,status) VALUES(?,?,?,?)",
                       (intent.intent_id, tag, encoded, "SUBMITTING"))
            db.commit()  # durable before touching the network
        except BaseException:
            db.rollback()
            raise
        try:
            response = await self._post(payload)
            broker_id = response.get("id")
            if response.get("s") != "ok" or not isinstance(broker_id, str) or not broker_id:
                raise AmbiguousOrder("Order acceptance not established")
        except BaseException:
            with db:
                self._transition(intent.intent_id, "UNKNOWN", 0)
            # Includes cancellation/crash recovery. No POST retry is allowed.
            raise
        with db:
            self._transition(intent.intent_id, "ACKNOWLEDGED", 0, broker_id)
        return dict(db.execute("SELECT * FROM live_orders WHERE intent_id=?", (intent.intent_id,)).fetchone())

    async def _post(self, payload: dict) -> dict:
        return await self._mutate("POST", "orders/sync", payload)

    async def _mutate(self, method: str, path: str, payload: dict) -> dict:
        await self.client.connect()
        async with self._request_lock:
            delay = self._request_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()
            try:
                async with self.client._session.request(method, f"{self.client.base_url}/{path}",
                                                        json=payload, allow_redirects=False) as response:
                    if response.status != 200:
                        raise AmbiguousOrder("HTTP error; acceptance must be reconciled")
                    body = await response.json(content_type=None)
                    if not isinstance(body, dict):
                        raise AmbiguousOrder("Invalid order response")
                    return body
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                raise AmbiguousOrder("Order transport failed; reconcile before continuing") from None

    def _active(self, intent_id: str):
        row = self.journal.db.execute("SELECT * FROM live_orders WHERE intent_id=?", (intent_id,)).fetchone()
        if row is None or row["status"] not in {"ACKNOWLEDGED", "PARTIALLY_FILLED"} or not row["broker_id"]:
            raise AmbiguousOrder("Order is not in a safely mutable state")
        return row

    async def cancel(self, intent_id: str) -> dict:
        """Request cancellation once; any ambiguous response requires reconciliation."""
        if not self.enabled:
            raise LiveOrdersDisabled("FYERS live orders are disabled")
        row = self._active(intent_id)
        with self.journal.db:
            self._transition(intent_id, "CANCEL_REQUESTED", row["filled"])
        try:
            body = await self._mutate("DELETE", "orders/sync", {"id": row["broker_id"]})
            if body.get("s") != "ok":
                raise AmbiguousOrder("Cancellation acceptance not established")
        except BaseException:
            with self.journal.db:
                self._transition(intent_id, "UNKNOWN", row["filled"])
            raise
        return dict(self.journal.db.execute("SELECT * FROM live_orders WHERE intent_id=?", (intent_id,)).fetchone())

    async def modify_limit(self, intent_id: str, *, quantity: int, limit_price: float) -> dict:
        """Modify a known order without changing its stable intent identity."""
        if not self.enabled:
            raise LiveOrdersDisabled("FYERS live orders are disabled")
        row = self._active(intent_id)
        old = json.loads(row["payload"])
        if type(quantity) is not int or quantity < max(1, row["filled"]) or not math.isfinite(limit_price) or limit_price <= 0:
            raise ValueError("Invalid modification")
        current = {**old, "qty": quantity, "limitPrice": limit_price, "id": row["broker_id"]}
        try:
            body = await self._mutate("PATCH", "orders/sync", {"id": row["broker_id"], "type": 1,
                                                               "qty": quantity, "limitPrice": limit_price})
            if body.get("s") != "ok":
                raise AmbiguousOrder("Modification acceptance not established")
        except BaseException:
            with self.journal.db:
                self._transition(intent_id, "UNKNOWN", row["filled"])
            raise
        current.pop("id")
        encoded = json.dumps(current, sort_keys=True)
        with self.journal.db:
            self.journal.db.execute(
                "INSERT INTO order_revisions(intent_id,previous_payload,current_payload,observed) VALUES(?,?,?,?)",
                (intent_id, row["payload"], encoded, time.time()))
            self.journal.db.execute("UPDATE live_orders SET payload=? WHERE intent_id=?", (encoded, intent_id))
        return dict(self.journal.db.execute("SELECT * FROM live_orders WHERE intent_id=?", (intent_id,)).fetchone())

    async def reconcile(self) -> list[dict]:
        """Adopt by broker ID/tag, keeping missing/ambiguous orders unresolved."""
        book, trades = await asyncio.gather(self.client.get_orders(), self.client.get_trades())
        if not isinstance(book.get("orderBook"), list):
            raise FyersGatewayError("Invalid order book")
        if not isinstance(trades.get("tradeBook"), list):
            raise FyersGatewayError("Invalid trade book")
        statuses = {1: "CANCELLED", 2: "FILLED", 4: "ACKNOWLEDGED", 5: "REJECTED", 6: "ACKNOWLEDGED", 7: "EXPIRED"}
        db = self.journal.db
        rows = db.execute("SELECT * FROM live_orders WHERE status NOT IN ('FILLED','CANCELLED','REJECTED','EXPIRED')").fetchall()
        with db:
            for trade in trades["tradeBook"]:
                trade_id = trade.get("tradeId") or trade.get("id")
                order_id = trade.get("orderNumber") or trade.get("orderId")
                quantity, price = trade.get("tradedQty"), trade.get("tradePrice")
                if (not isinstance(trade_id, str) or not trade_id or not isinstance(order_id, str)
                        or not order_id or type(quantity) is not int or quantity <= 0
                        or isinstance(price, bool) or not isinstance(price, (int, float))
                        or not math.isfinite(price) or price <= 0):
                    raise AmbiguousOrder("Invalid broker trade record")
                db.execute("INSERT OR IGNORE INTO broker_trades VALUES(?,?,?,?,?)",
                           (trade_id, order_id, quantity, float(price), time.time()))
            for local in rows:
                matches = [r for r in book["orderBook"] if
                           (local["broker_id"] and r.get("id") == local["broker_id"])
                           or (not local["broker_id"] and r.get("orderTag") in {local["tag"], f"1:{local['tag']}"})]
                if len(matches) != 1:
                    raise AmbiguousOrder("Missing or duplicate broker order; manual reconciliation required")
                order = matches[0]
                payload = json.loads(local["payload"])
                filled = order.get("filledQty")
                if (order.get("symbol") != payload["symbol"] or order.get("qty") != payload["qty"]
                        or order.get("side") != payload["side"]
                        or type(filled) is not int or not local["filled"] <= filled <= payload["qty"]
                        or not isinstance(order.get("id"), str) or not order["id"]
                        or order.get("status") not in statuses):
                    raise AmbiguousOrder("Broker order conflicts with persisted intent")
                status = statuses[order["status"]]
                if status == "ACKNOWLEDGED" and filled:
                    status = "PARTIALLY_FILLED" if filled < payload["qty"] else "FILLED"
                if status == "FILLED" and filled != payload["qty"]:
                    raise AmbiguousOrder("Filled status conflicts with quantity")
                try:
                    self._transition(local["intent_id"], status, filled, order["id"])
                except ValueError as exc:
                    raise AmbiguousOrder("Broker lifecycle conflicts with local order") from exc
                traded = db.execute("SELECT COALESCE(SUM(quantity),0) FROM broker_trades WHERE broker_order_id=?",
                                    (order["id"],)).fetchone()[0]
                if traded > filled:
                    raise AmbiguousOrder("Trade records exceed broker cumulative fill")
        return [dict(r) for r in db.execute("SELECT * FROM live_orders")]

    async def reconcile_account(self, *, intended: dict[str, int], local: dict[str, int]) -> dict:
        """Compare normalized cash, holdings and day positions without adopting broker state."""
        positions, holdings, funds = await asyncio.gather(
            self.client.get_positions(), self.client.get_holdings(), self.client.get_funds())
        position_rows = positions.get("netPositions")
        holding_rows = holdings.get("holdings")
        limits = funds.get("fund_limit")
        if not all(isinstance(rows, list) for rows in (position_rows, holding_rows, limits)):
            raise FyersGatewayError("Invalid account reconciliation snapshot")
        broker: dict[str, int] = {}
        for row, keys in [(row, ("netQty", "qty")) for row in position_rows] + [
                (row, ("quantity", "remainingQuantity")) for row in holding_rows]:
            symbol = row.get("symbol") if isinstance(row, dict) else None
            quantity = next((row.get(key) for key in keys if key in row), None) if isinstance(row, dict) else None
            if not isinstance(symbol, str) or type(quantity) is not int:
                raise FyersGatewayError("Unusable broker position row")
            broker[symbol] = broker.get(symbol, 0) + quantity
        available = next((row.get("equityAmount") for row in limits
                          if isinstance(row, dict) and row.get("id") in {3, 10}), None)
        if isinstance(available, bool) or not isinstance(available, (int, float)) or not math.isfinite(available):
            raise FyersGatewayError("Available broker cash is missing")
        comparison = PositionViews(intended=intended, local=local, broker=broker).reconcile()
        snapshot = {**comparison, "available_cash": float(available), "at": time.time()}
        with self.journal.db:
            self.journal.put("broker_reconciliation", snapshot)
            if comparison["entry_blocked"] and not self.journal.get("halt_reason"):
                self.journal.put("halt_reason", "broker position reconciliation mismatch")
        return snapshot
