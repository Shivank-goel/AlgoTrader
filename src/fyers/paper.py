"""Long-only delivery simulator. Never calls broker mutation APIs."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
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
            self._ensure_simulation()

    def _file_hash(self, path: str) -> str:
        try:
            return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        except OSError:
            return "missing"

    def _ensure_simulation(self) -> None:
        """Register one durable shadow account; never reset or recreate it on restart."""
        row = self.journal.db.execute(
            "SELECT * FROM shadow_simulations WHERE status='ACTIVE' LIMIT 1").fetchone()
        now = time.time()
        if row:
            if abs(row["initial_capital"] - self.config.capital_inr) > 1e-9:
                raise ValueError("Active shadow simulation capital differs from configuration")
            self.journal.put("simulation_id", row["simulation_id"])
            return
        simulation_id = f"shadow-{uuid.uuid4().hex}"
        code_hash = self._file_hash("src/fyers/paper.py")
        config_hash = hashlib.sha256(self._file_hash("config/fyers_runtime.yaml").encode()).hexdigest()
        cost_hash = self._file_hash("config/fyers_costs.yaml")
        universe = self._file_hash("config/nse_forward_universe.yaml")
        self.journal.db.execute(
            "INSERT INTO shadow_simulations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (simulation_id, "SHADOW", self.config.capital_inr, now, "ACTIVE",
             code_hash, config_hash, cost_hash, "nse_forward_universe", universe, now),
        )
        self.journal.put("simulation_id", simulation_id)
        self._checkpoint("SIMULATION_START", simulation_id, now, self.config.capital_inr,
                         self.config.capital_inr, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                         source_event_id="simulation-start")

    def _checkpoint(self, event_type: str, simulation_id: str, timestamp: float,
                    cash: float, market_value: float, realized: float, unrealized: float,
                    fees: float, equity: float, gross: float, net: float, long: float,
                    *, source_event_id: str) -> None:
        previous = self.journal.db.execute(
            "SELECT peak_equity FROM equity_checkpoints WHERE simulation_id=? ORDER BY timestamp DESC LIMIT 1",
            (simulation_id,)).fetchone()
        peak = max(float(previous[0]) if previous else equity, equity)
        drawdown = max(0.0, (peak - equity) / peak) if peak else 0.0
        checkpoint_id = f"{simulation_id}:{event_type}:{source_event_id}"
        self.journal.db.execute(
            "INSERT OR IGNORE INTO equity_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (checkpoint_id, simulation_id, timestamp, event_type, source_event_id, cash,
             market_value, realized, unrealized, fees, equity, peak, drawdown, gross, net, long),
        )

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

    def _short_positions(self) -> dict[str, dict]:
        return {row["symbol"]: dict(row) for row in self.journal.db.execute(
            "SELECT * FROM short_positions WHERE quantity>0"
        )}

    def _mark(self, quotes: dict[str, Quote], now: float) -> dict:
        """Update valuation/risk inside the caller's transaction; no synthetic marks."""
        if not math.isfinite(now) or now <= 0:
            raise ValueError("Invalid valuation timestamp")
        last = self.journal.get("last_complete_equity")
        if last and now < last["at"]:
            raise ValueError("Valuation timestamp moved backwards")
        positions = self._positions()
        shorts = self._short_positions()
        missing = [s for s in set(positions) | set(shorts) if s not in quotes or quotes[s].symbol != s
                   or not quotes[s].usable(now, self.config.stale_seconds)]
        valuation = {"at": now, "complete": not missing, "missing_symbols": missing,
                     "equity": None, "unrealized_pnl": None,
                     "realized_pnl": self.journal.get("realized_pnl", 0.0),
                     "total_fees": self.journal.get("total_fees", 0.0)}
        if not missing:
            value = sum(qty * quotes[s].bid for s, qty in positions.items())
            short_reserved = sum(row["reserved_capital"] for row in shorts.values())
            short_liability = sum(row["quantity"] * quotes[s].ask for s, row in shorts.items())
            equity = self.journal.get("cash") + value + short_reserved - short_liability
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
            unrealized_long = value - sum(basis.get(s, 0.0) for s in positions)
            unrealized_short = sum((row["entry_price"] - quotes[s].ask) * row["quantity"]
                                   - row["entry_fee"] for s, row in shorts.items())
            valuation.update(equity=equity, daily_pnl=daily_pnl,
                             unrealized_pnl=unrealized_long + unrealized_short,
                             short_reserved=short_reserved, short_liability=short_liability,
                             long_exposure=value, short_exposure=short_liability,
                             gross_exposure=value + short_liability,
                             net_exposure=value - short_liability)
            self._checkpoint("MARK_TO_MARKET", self.journal.get("simulation_id"), now,
                             self.journal.get("cash"), value, valuation["realized_pnl"],
                             valuation["unrealized_pnl"], valuation["total_fees"], equity,
                             value + short_liability, value - short_liability,
                             value, source_event_id=str(now))
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
        if intent.position_side == "SHORT":
            if not intent.intraday_only:
                raise ValueError("Short positions must be intraday_only")
            return self._short_fill(intent, instrument, quotes, now=now, market_open=market_open)
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
                if intent.execution_mode == "SHADOW":
                    row = db.execute(
                        "SELECT payload FROM scheduled_intents WHERE intent_id=?", (intent.intent_id,)
                    ).fetchone()
                    if not row or row["payload"] != encoded:
                        raise ValueError("Unregistered shadow intent")
                else:
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
            if buy and intent.protection_required and intent.stop_loss is None:
                raise ValueError("Shadow entry protection is required but stop_loss is missing")
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
            if buy and intent.stop_loss is not None:
                if intent.stop_loss >= price or (intent.take_profit is not None and intent.take_profit <= price):
                    raise ValueError("Invalid long entry protection")
                if intent.sizing and intent.sizing.get("risk_budget_rupees") is not None:
                    actual_risk = (price - intent.stop_loss) * intent.quantity
                    if actual_risk > float(intent.sizing["risk_budget_rupees"]) * 1.000001:
                        raise ValueError("Actual fill exceeds sized risk budget")
                equity_at_entry = valuation["equity"]
                risk_per_share = price - intent.stop_loss
                protection_id = f"{self.journal.get('simulation_id')}:{intent.intent_id}"
            else:
                equity_at_entry = None
                risk_per_share = None
                protection_id = None
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
            if buy and intent.stop_loss is not None:
                risk_rupees = risk_per_share * intent.quantity
                db.execute(
                    "INSERT INTO position_protection "
                    "(protection_id,simulation_id,intent_id,decision_id,symbol,strategy_family,"
                    "strategy_version,side,entry_time,entry_reference_price,entry_fill_price,quantity,"
                    "stop_loss_price,take_profit_price,initial_risk_per_share,initial_risk_rupees,"
                    "initial_risk_percent,account_equity_at_entry,capital_committed,market_regime,"
                    "stock_state,status,created_at,code_hash,config_hash,cost_model_hash,protection_method,protection_inputs) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (protection_id, self.journal.get("simulation_id"), intent.intent_id,
                     intent.decision_id, intent.symbol, intent.strategy, intent.strategy_version,
                     intent.position_side, now, quote.ask, price, intent.quantity,
                     intent.stop_loss, intent.take_profit, risk_per_share, risk_rupees,
                     risk_rupees / equity_at_entry * 100 if equity_at_entry else 0.0,
                     equity_at_entry, price * intent.quantity, intent.market_regime,
                     intent.stock_state, "ACTIVE", now, intent.code_hash, intent.config_hash,
                     self.costs.tariff_version, intent.protection_method,
                     json.dumps(intent.protection_inputs, sort_keys=True) if intent.protection_inputs else None),
                )
            self._mark(quotes, now)  # include newly charged fees/slippage immediately
            valuation = self.journal.get("paper_valuation")
            if valuation and valuation.get("equity") is not None:
                self._checkpoint("FILL", self.journal.get("simulation_id"), now,
                                 self.journal.get("cash"), valuation["equity"] - self.journal.get("cash"),
                                 valuation["realized_pnl"], valuation["unrealized_pnl"],
                                 valuation["total_fees"], valuation["equity"],
                                 valuation["equity"] - self.journal.get("cash"),
                                 valuation["equity"] - self.journal.get("cash"),
                                 valuation["equity"] - self.journal.get("cash"),
                                 source_event_id=intent.intent_id)
            db.commit()
            return dict(db.execute("SELECT * FROM fills WHERE intent_id=?", (intent.intent_id,)).fetchone())
        except BaseException:
            db.rollback()
            raise

    def _short_fill(self, intent: Intent, instrument: Instrument, quotes: dict[str, Quote],
                    *, now: float, market_open: bool) -> dict:
        db = self.journal.db
        if not market_open:
            raise ValueError("NSE normal session is not open")
        if intent.symbol not in self.config.symbols or instrument.symbol != intent.symbol:
            raise ValueError("Symbol mismatch")
        quote = quotes.get(intent.symbol)
        if quote is None or not quote.usable(now, self.config.stale_seconds):
            raise ValueError("Missing, stale, crossed or future quote")
        previous = db.execute("SELECT * FROM fills WHERE intent_id=?", (intent.intent_id,)).fetchone()
        if previous:
            if previous["intent"] != intent.model_dump_json():
                raise ValueError("Intent ID reused with different payload")
            return dict(previous)
        if intent.side == Side.SELL:
            local = datetime.fromtimestamp(now, ZoneInfo("Asia/Kolkata"))
            cutoff_h, cutoff_m = map(int, self.config.short_entry_cutoff.split(":"))
            if (local.hour, local.minute) >= (cutoff_h, cutoff_m):
                raise ValueError("Short entry cutoff has passed")
            if intent.protection_required and (intent.stop_loss is None or intent.stop_loss <= quote.bid):
                raise ValueError("Invalid or missing short stop protection")
            if intent.take_profit is not None and intent.take_profit >= quote.bid:
                raise ValueError("Invalid short take-profit protection")
            estimate = estimate_fill(side=Side.SELL, quantity=intent.quantity, instrument=instrument,
                                     quote=quote, slippage_bps=self.config.slippage_bps,
                                     costs=self.costs, delivery=False)
            if estimate.status == "UNFILLED" or estimate.price is None:
                raise ValueError("Insufficient displayed liquidity; no optimistic short fill")
            reserve = estimate.notional + estimate.fee
            if intent.stop_loss is not None and intent.stop_loss <= estimate.price:
                raise ValueError("Invalid short stop protection after fill")
            if intent.take_profit is not None and intent.take_profit >= estimate.price:
                raise ValueError("Invalid short take-profit after fill")
            if intent.sizing and intent.sizing.get("risk_budget_rupees") is not None and intent.stop_loss is not None:
                if (intent.stop_loss - estimate.price) * intent.quantity > float(intent.sizing["risk_budget_rupees"]) * 1.000001:
                    raise ValueError("Actual short fill exceeds sized risk budget")
            if reserve > self.journal.get("cash"):
                raise ValueError("Insufficient short margin capital")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("UPDATE state SET value=? WHERE key='cash'", (json.dumps(self.journal.get("cash") - reserve),))
                db.execute("INSERT INTO short_positions VALUES(?,?,?,?,?,?,?)",
                           (intent.symbol, intent.quantity, estimate.price, estimate.fee, reserve, now, intent.intent_id))
                db.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?)",
                           (intent.intent_id, intent.model_dump_json(), intent.symbol, "SELL", intent.quantity,
                            estimate.price, estimate.fee, now))
                if intent.stop_loss is not None:
                    equity = self.journal.get("paper_valuation", {}).get("equity", self.journal.get("cash"))
                    risk_per_share = intent.stop_loss - estimate.price
                    risk_rupees = risk_per_share * intent.quantity
                    db.execute(
                        "INSERT INTO position_protection "
                        "(protection_id,simulation_id,intent_id,decision_id,symbol,strategy_family,strategy_version,side,entry_time,entry_reference_price,entry_fill_price,quantity,stop_loss_price,take_profit_price,initial_risk_per_share,initial_risk_rupees,initial_risk_percent,account_equity_at_entry,capital_committed,market_regime,stock_state,status,created_at,code_hash,config_hash,cost_model_hash,protection_method,protection_inputs) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"{self.journal.get('simulation_id')}:{intent.intent_id}", self.journal.get("simulation_id"), intent.intent_id,
                         intent.decision_id, intent.symbol, intent.strategy, intent.strategy_version, "SHORT", now, quote.bid,
                         estimate.price, intent.quantity, intent.stop_loss, intent.take_profit, risk_per_share, risk_rupees,
                         risk_rupees / equity * 100 if equity else 0.0, equity, reserve, intent.market_regime,
                         intent.stock_state, "ACTIVE", now, intent.code_hash, intent.config_hash, self.costs.tariff_version,
                         intent.protection_method, json.dumps(intent.protection_inputs, sort_keys=True) if intent.protection_inputs else None),
                    )
                db.execute("INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?)",
                           (intent.intent_id, intent.model_dump_json(), intent.quantity, intent.quantity, "FILLED", now, now))
                db.execute("INSERT INTO events(received,kind,payload) VALUES(?,?,?)",
                           (now, "paper_short_entry", json.dumps({"intent_id": intent.intent_id, "price": estimate.price})))
                self._mark(quotes, now)
                db.commit()
                return dict(db.execute("SELECT * FROM fills WHERE intent_id=?", (intent.intent_id,)).fetchone())
            except BaseException:
                db.rollback()
                raise
        row = db.execute("SELECT * FROM short_positions WHERE symbol=?", (intent.symbol,)).fetchone()
        if not row or intent.quantity > row["quantity"]:
            raise ValueError("No covered short position")
        estimate = estimate_fill(side=Side.BUY, quantity=intent.quantity, instrument=instrument,
                                 quote=quote, slippage_bps=self.config.slippage_bps,
                                 costs=self.costs, delivery=False)
        if estimate.status == "UNFILLED" or estimate.price is None:
            raise ValueError("Insufficient displayed liquidity; no optimistic cover")
        db.execute("BEGIN IMMEDIATE")
        try:
            fraction = intent.quantity / row["quantity"]
            released = row["reserved_capital"] * fraction
            entry_cost = row["entry_price"] * intent.quantity
            entry_fee = row["entry_fee"] * fraction
            realized = entry_cost - estimate.notional - entry_fee - estimate.fee
            self.journal.put("realized_pnl", self.journal.get("realized_pnl") + realized)
            self.journal.put("total_fees", self.journal.get("total_fees") + estimate.fee)
            self.journal.put("cash", self.journal.get("cash") + released - estimate.notional - estimate.fee)
            remaining = row["quantity"] - intent.quantity
            if remaining:
                db.execute("UPDATE short_positions SET quantity=?,reserved_capital=? WHERE symbol=?",
                           (remaining, row["reserved_capital"] - released, intent.symbol))
            else:
                db.execute("DELETE FROM short_positions WHERE symbol=?", (intent.symbol,))
            db.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?)",
                       (intent.intent_id, intent.model_dump_json(), intent.symbol, "BUY", intent.quantity,
                        estimate.price, estimate.fee, now))
            db.execute("INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?)",
                       (intent.intent_id, intent.model_dump_json(), intent.quantity, intent.quantity, "FILLED", now, now))
            db.execute("INSERT INTO events(received,kind,payload) VALUES(?,?,?)",
                       (now, "paper_short_cover", json.dumps({"intent_id": intent.intent_id, "price": estimate.price,
                                                                "realized_pnl": realized})))
            self._mark(quotes, now)
            db.commit()
            return dict(db.execute("SELECT * FROM fills WHERE intent_id=?", (intent.intent_id,)).fetchone())
        except BaseException:
            db.rollback()
            raise
