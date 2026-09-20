"""Long-only delivery simulator. Never calls broker mutation APIs."""

from __future__ import annotations

import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from src.fyers.costs import FyersCosts
from src.fyers.economics import estimate_fill, settlement_day
from src.fyers.journal import Journal
from src.fyers.models import ROOT, Instrument, Intent, Quote, RuntimeConfig, Side
from src.fyers.qualification import qualification


class PaperBroker:
    def __init__(self, journal: Journal, config: RuntimeConfig, costs: FyersCosts) -> None:
        self.journal, self.config, self.costs = journal, config, costs
        with journal.db:
            if journal.get("cash") is None:
                journal.put("cash", config.capital_inr)
                journal.put("initial_capital", config.capital_inr)
            elif journal.get("initial_capital") != config.capital_inr:
                raise ValueError("Capital differs from persisted paper account")
            if journal.get("position_cost_basis") is None:
                self._recover_accounting()
            if not journal.db.execute("SELECT 1 FROM tax_lots LIMIT 1").fetchone():
                self._recover_tax_lots()

    def _recover_tax_lots(self) -> None:
        """Additive FIFO tax-lot migration derived only from immutable fill history."""
        for row in self.journal.db.execute("SELECT * FROM fills ORDER BY timestamp,rowid"):
            if row["side"] == Side.BUY.value:
                unit = (row["price"] * row["quantity"] + row["fee"]) / row["quantity"]
                self.journal.db.execute(
                    "INSERT OR IGNORE INTO tax_lots VALUES(?,?,?,?,?)",
                    (row["intent_id"], row["symbol"], row["timestamp"], row["quantity"], unit),
                )
            else:
                self._consume_tax_lots(row["intent_id"], row["symbol"], row["quantity"],
                                       row["price"] * row["quantity"] - row["fee"], row["timestamp"])

    def _consume_tax_lots(self, execution_id: str, symbol: str, quantity: int,
                          proceeds: float, sold_at: float) -> None:
        remaining = quantity
        for lot in self.journal.db.execute(
            "SELECT * FROM tax_lots WHERE symbol=? AND remaining>0 ORDER BY acquired_at,lot_id",
            (symbol,),
        ).fetchall():
            used = min(remaining, lot["remaining"])
            allocated = proceeds * used / quantity
            basis = lot["unit_cost"] * used
            holding_days = max(0, int((sold_at - lot["acquired_at"]) // 86400))
            self.journal.db.execute(
                "INSERT OR IGNORE INTO realized_tax_lots VALUES(?,?,?,?,?,?,?,?,?)",
                (execution_id, lot["lot_id"], symbol, sold_at, used, allocated, basis,
                 allocated - basis, holding_days),
            )
            self.journal.db.execute(
                "UPDATE tax_lots SET remaining=remaining-? WHERE lot_id=?", (used, lot["lot_id"]),
            )
            remaining -= used
            if remaining == 0:
                break
        if remaining:
            raise ValueError("Paper positions and FIFO tax lots do not reconcile")

    def _recover_accounting(self) -> None:
        """Upgrade legacy journals from committed fills without moving cash."""
        basis, quantities = {}, {}
        realized, fees = 0.0, 0.0
        for row in self.journal.db.execute("SELECT * FROM fills ORDER BY rowid"):
            symbol, qty = row["symbol"], row["quantity"]
            held = quantities.get(symbol, 0)
            value = row["price"] * qty
            fees += row["fee"]
            if row["side"] == Side.BUY.value:
                basis[symbol] = basis.get(symbol, 0.0) + value + row["fee"]
                quantities[symbol] = held + qty
            else:
                if qty > held:
                    raise ValueError("Paper fill history contains an uncovered sell")
                released = basis[symbol] * qty / held
                basis[symbol] -= released
                quantities[symbol] = held - qty
                realized += value - row["fee"] - released
        actual = self._positions()
        if actual != {s: q for s, q in quantities.items() if q}:
            raise ValueError("Paper positions do not reconcile with fill history")
        self.journal.put("position_cost_basis", basis)
        self.journal.put("realized_pnl", realized)
        self.journal.put("total_fees", fees)

    def _positions(self) -> dict[str, int]:
        return {row["symbol"]: row["quantity"] for row in self.journal.db.execute(
            "SELECT * FROM positions WHERE quantity>0"
        )}

    def _mark(self, quotes: dict[str, Quote], now: float) -> dict:
        """Update valuation/risk inside the caller's transaction; no synthetic marks."""
        if not math.isfinite(now) or now <= 0:
            raise ValueError("Invalid valuation timestamp")
        last = self.journal.get("last_complete_equity")
        if last and now < last["at"]:
            raise ValueError("Valuation timestamp moved backwards")
        positions = self._positions()
        missing = [s for s in positions if s not in quotes or quotes[s].symbol != s
                   or not quotes[s].usable(now, self.config.stale_seconds)]
        valuation = {"at": now, "complete": not missing, "missing_symbols": missing,
                     "equity": None, "unrealized_pnl": None,
                     "realized_pnl": self.journal.get("realized_pnl", 0.0),
                     "total_fees": self.journal.get("total_fees", 0.0)}
        if not missing:
            value = sum(qty * quotes[s].bid for s, qty in positions.items())
            equity = self.journal.get("cash") + value
            basis = self.journal.get("position_cost_basis", {})
            day = datetime.fromtimestamp(now, ZoneInfo("Asia/Kolkata")).date().isoformat()
            baseline = self.journal.get("day_equity")
            if not baseline or baseline["day"] != day:
                # Carry the last observed prior-session equity across restart/day
                # changes. Never reset the baseline to an already-gapped opening mark.
                reference = last["equity"] if last else self.journal.get("initial_capital")
                baseline = {"day": day, "value": reference}
                self.journal.put("day_equity", baseline)
            daily_pnl = equity - baseline["value"]
            valuation.update(equity=equity, daily_pnl=daily_pnl,
                             unrealized_pnl=value - sum(basis.get(s, 0.0) for s in positions))
            self.journal.put("last_complete_equity", {"at": now, "day": day, "equity": equity})
            if daily_pnl <= -self.config.max_daily_loss_inr and not self.journal.get("halt_reason"):
                self.journal.put("halt_reason", "daily paper loss limit")
                self.journal.db.execute(
                    "INSERT INTO events(received,kind,payload) VALUES(?,?,?)",
                    (now, "halt", json.dumps({"reason": "daily paper loss limit"})),
                )
        self.journal.put("paper_valuation", valuation)
        return valuation

    def mark_to_market(self, quotes: dict[str, Quote], *, now: float) -> dict:
        """Monitor losses independently of orders; persist across process restarts."""
        self.journal.db.execute("BEGIN IMMEDIATE")
        try:
            result = self._mark(quotes, now)
            self.journal.db.commit()
            return result
        except BaseException:
            self.journal.db.rollback()
            raise

    def fill(self, intent: Intent, instrument: Instrument, quotes: dict[str, Quote],
             *, now: float, market_open: bool) -> dict:
        """Atomic fill; duplicate intents return the original without charging twice."""
        db = self.journal.db
        db.execute("BEGIN IMMEDIATE")
        try:
            previous = db.execute("SELECT * FROM fills WHERE intent_id=?", (intent.intent_id,)).fetchone()
            encoded = intent.model_dump_json()
            if previous:
                if previous["intent"] != encoded:
                    raise ValueError("Intent ID reused with different payload")
                db.rollback()
                return dict(previous)
            buy = intent.side == Side.BUY
            if buy:
                if self.journal.get("halt_reason"):
                    raise ValueError(f"Paper account halted: {self.journal.get('halt_reason')}")
                ok, strategy = qualification(ROOT / self.config.qualification_file, ROOT / self.config.trials_file)
                if not ok or strategy != intent.strategy:
                    raise ValueError("Strategy not qualified for paper deployment")
            if not market_open:
                raise ValueError("NSE normal session is not open")
            intent_age = now - intent.created_at.timestamp()
            if intent.created_at.tzinfo is None or not -0.001 <= intent_age <= self.config.stale_seconds:
                raise ValueError("Intent timestamp is missing timezone, stale or future")
            if (buy and intent.symbol not in self.config.symbols) or instrument.symbol != intent.symbol or intent.quantity % instrument.lot_size:
                raise ValueError("Symbol or lot size mismatch")
            positions = self._positions()
            held = positions.get(intent.symbol, 0)
            if not buy and intent.quantity > held:
                raise ValueError("Delivery short selling is prohibited")
            quote = quotes.get(intent.symbol)
            if quote is None or quote.symbol != intent.symbol or not quote.usable(now, self.config.stale_seconds):
                raise ValueError("Missing, stale, crossed or future quote")
            valuation = self._mark(quotes, now)
            if buy and (not valuation["complete"] or self.journal.get("halt_reason")):
                db.commit()  # keep the risk observation/halt even though the order fails
                raise ValueError("Incomplete valuation or daily loss limit reached")
            cash = self.journal.get("cash")
            day = datetime.fromtimestamp(now, ZoneInfo("Asia/Kolkata")).date().isoformat()
            charged = db.execute("SELECT 1 FROM dp_charges WHERE isin=? AND day=?", (instrument.isin, day)).fetchone()
            estimate = estimate_fill(side=intent.side, quantity=intent.quantity,
                                     instrument=instrument, quote=quote,
                                     slippage_bps=self.config.slippage_bps, costs=self.costs,
                                     delivery=True, charge_dp=not buy and not charged)
            if estimate.status == "UNFILLED":
                raise ValueError("Insufficient displayed liquidity; no optimistic fill")
            assert estimate.price is not None
            price, notional, fee = estimate.price, estimate.notional, estimate.fee
            if notional <= 0 or (buy and notional > self.config.max_order_inr):
                raise ValueError("Order notional outside risk limit")
            if buy and valuation["daily_pnl"] + intent.quantity * quote.bid - notional - fee <= -self.config.max_daily_loss_inr:
                raise ValueError("Entry costs would breach daily loss limit")
            new_cash = cash - notional - fee if buy else cash + notional - fee
            if buy and new_cash < 0:
                raise ValueError("Insufficient paper cash including fees")
            basis = self.journal.get("position_cost_basis")
            if buy:
                basis[intent.symbol] = basis.get(intent.symbol, 0.0) + notional + fee
                db.execute("INSERT INTO tax_lots VALUES(?,?,?,?,?)", (
                    intent.intent_id, intent.symbol, now, intent.quantity,
                    (notional + fee) / intent.quantity,
                ))
            else:
                released = basis[intent.symbol] * intent.quantity / held
                basis[intent.symbol] -= released
                self.journal.put("realized_pnl", self.journal.get("realized_pnl") + notional - fee - released)
                self._consume_tax_lots(intent.intent_id, intent.symbol, intent.quantity,
                                       notional - fee, now)
            self.journal.put("position_cost_basis", basis)
            self.journal.put("total_fees", self.journal.get("total_fees") + fee)
            self.journal.put("cash", new_cash)
            db.execute("INSERT OR REPLACE INTO positions VALUES(?,?)", (intent.symbol, held + (intent.quantity if buy else -intent.quantity)))
            if not buy:
                db.execute("INSERT OR IGNORE INTO dp_charges VALUES(?,?)", (instrument.isin, day))
            db.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?)", (intent.intent_id, encoded, intent.symbol, intent.side.value, intent.quantity, price, fee, now))
            db.execute("INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?)", (
                intent.intent_id, encoded, intent.quantity, intent.quantity, "FILLED", now, now,
            ))
            due = settlement_day(datetime.fromtimestamp(now, ZoneInfo("Asia/Kolkata")).date())
            db.execute("INSERT INTO settlement_obligations VALUES(?,?,?,?,?,?,?)",
                       (intent.intent_id, day, due.isoformat(), intent.symbol,
                        intent.side.value, intent.quantity, notional + fee if buy else notional - fee))
            db.execute("INSERT INTO events(received,kind,payload) VALUES(?,?,?)", (now, "paper_fill", json.dumps({"intent_id": intent.intent_id, "price": price, "fee": fee})))
            self._mark(quotes, now)  # include newly charged fees/slippage immediately
            db.commit()
            return dict(db.execute("SELECT * FROM fills WHERE intent_id=?", (intent.intent_id,)).fetchone())
        except BaseException:
            db.rollback()
            raise
