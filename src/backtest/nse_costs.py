"""NSE cash-equity intraday costs (Dhan).

The crypto `CostModel` assumes two things that are false on NSE, and both matter
enough to change which strategies are viable:

1. **Cost in bps is scale-invariant.** Dhan charges `min(₹20, 0.03%)` per order,
   so above ₹66,667 of notional the fee stops growing and the effective rate
   falls. A model without the cap overstates cost for large orders and — worse —
   makes the backtester blind to the fact that concentrating notional is cheaper
   than spreading it.
2. **Fees are symmetric across buy and sell.** They are not. STT is charged on
   the **sell** side only for intraday, and stamp duty on the **buy** side only.
   A round trip is therefore not two identical legs, and a long and a short of
   the same size do not cost the same.

Measured total for a ₹10,000 round trip: **₹10.60, or 10.60 bps** — close enough
to crypto's 8.26 bps that strategy economics carry over, but the composition is
completely different.

Rates as published for 2026; `main.py nse-costs` prints a breakdown to check
against Dhan's own calculator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.backtest.costs import BPS, CostModel
from src.core.models import Direction, OrderSide


@dataclass(frozen=True)
class NSEEquityCostModel(CostModel):
    """Intraday (MIS) cash-equity costs on NSE via Dhan.

    Subclasses `CostModel` so `QuickBacktester` and `PortfolioBacktester` accept
    it unchanged; the fee methods are overridden with the real structure.
    """

    # Dhan intraday: 0.03% or ₹20 per order, whichever is LOWER.
    brokerage_bps: float = 3.0
    brokerage_cap_inr: float = 20.0
    # Equity delivery is zero-brokerage; intraday is not.
    delivery_brokerage_bps: float = 0.0

    # Statutory, and deliberately asymmetric.
    stt_sell_bps: float = 2.5        # 0.025%, intraday, SELL side only
    stamp_buy_bps: float = 0.3       # 0.003%, BUY side only
    exchange_txn_bps: float = 0.297  # NSE 0.00297%, both sides
    sebi_bps: float = 0.01           # 0.0001%, both sides
    gst_pct: float = 18.0            # on brokerage + exchange + SEBI

    # Intraday equity has no funding; MIS is squared off same day.
    funding_bps_per_period: float = 0.0

    # Half the effective bid-ask spread, paid on every leg. Zero by default so
    # the fee arithmetic stays exact and auditable against Dhan's calculator —
    # but a strategy evaluated at zero is being handed free execution, and for a
    # book that deliberately selects the day's most dislocated names that
    # assumption is doing real work. Set it explicitly, or use
    # `breakeven_half_spread_bps` to report what the strategy can tolerate.
    half_spread_bps: float = 0.0

    # -- fees -----------------------------------------------------------

    def brokerage_inr(self, notional: float, *, delivery: bool = False) -> float:
        """Per-order brokerage, respecting the absolute cap."""
        if delivery:
            return abs(notional) * self.delivery_brokerage_bps * BPS
        uncapped = abs(notional) * self.brokerage_bps * BPS
        return min(self.brokerage_cap_inr, uncapped)

    def leg_fee_inr(
        self,
        notional: float,
        side: OrderSide,
        *,
        delivery: bool = False,
    ) -> float:
        """Total statutory + brokerage cost for ONE leg, in rupees.

        Asymmetric by construction: STT on sells, stamp duty on buys.
        """
        notional = abs(notional)
        brokerage = self.brokerage_inr(notional, delivery=delivery)
        exchange = notional * self.exchange_txn_bps * BPS
        sebi = notional * self.sebi_bps * BPS

        stt = notional * self.stt_sell_bps * BPS if side == OrderSide.SELL else 0.0
        stamp = notional * self.stamp_buy_bps * BPS if side == OrderSide.BUY else 0.0

        gst = (self.gst_pct / 100.0) * (brokerage + exchange + sebi)
        # Spread is an execution cost, not a statutory one, so no GST on it.
        spread = notional * self.half_spread_bps * BPS

        return brokerage + exchange + sebi + stt + stamp + gst + spread

    def breakeven_half_spread_bps(
        self, gross_bps_per_period: float, turnover_per_period: float, notional_per_order: float
    ) -> float:
        """Half-spread at which a strategy exactly breaks even.

        The honest way to report a strategy when no quote data exists: rather
        than assume a spread, state the one it must beat. Compare against a
        measured or quoted spread to decide.
        """
        if turnover_per_period <= 0:
            return 0.0
        fee_bps = turnover_per_period * self.round_trip_fee_bps_at(notional_per_order) / 2.0
        return (gross_bps_per_period - fee_bps) / turnover_per_period

    def round_trip_fee_inr(self, notional: float, *, delivery: bool = False) -> float:
        """Both legs of a round trip at the same notional."""
        return self.leg_fee_inr(notional, OrderSide.BUY, delivery=delivery) + self.leg_fee_inr(
            notional, OrderSide.SELL, delivery=delivery
        )

    def round_trip_fee_bps_at(self, notional: float, *, delivery: bool = False) -> float:
        """Effective round-trip cost in bps — falls above the brokerage cap."""
        if notional <= 0:
            return 0.0
        return self.round_trip_fee_inr(notional, delivery=delivery) / notional / BPS

    # -- CostModel interface overrides ----------------------------------

    def fee_usd(self, notional: float, *, maker: bool = False) -> float:
        """One leg, side-agnostic — the average of a buy and a sell.

        `QuickBacktester` calls this without a side. Averaging keeps a round
        trip exact even though neither individual leg is; use `leg_fee_inr`
        when the side is known. Named `fee_usd` only to satisfy the parent
        interface; the unit here is rupees.
        """
        return self.round_trip_fee_inr(notional) / 2.0

    def entry_exit_fee_bps(self, *, maker_entry: bool = False) -> float:
        """Round-trip cost in bps at a reference notional.

        `PortfolioBacktester` multiplies this by turnover, so it needs a single
        rate. Below the ₹66,667 cap the rate is flat, which is the regime a
        ₹10,000 account trades in — position count is free, only turnover costs.
        """
        return self.round_trip_fee_bps_at(self.reference_notional_inr)

    # Notional at which `entry_exit_fee_bps` is evaluated. Set to a typical
    # per-name order for a small account, where brokerage is uncapped.
    reference_notional_inr: float = 10_000.0

    def round_trip_cost_pct(
        self,
        *,
        entry_price: float,
        exit_price: float,
        direction: Direction,
        entry_ts=None,
        exit_ts=None,
        size: float = 1.0,
        maker: bool = False,
        maker_entry: bool = False,
        funding_rate_bps: Optional[float] = None,
    ) -> float:
        """Round-trip cost as a percentage of entry notional.

        No funding: MIS positions are squared off the same session.
        """
        if entry_price <= 0 or size <= 0:
            return 0.0

        entry_notional = entry_price * size
        exit_notional = exit_price * size

        if direction == Direction.LONG:
            entry_side, exit_side = OrderSide.BUY, OrderSide.SELL
        else:
            entry_side, exit_side = OrderSide.SELL, OrderSide.BUY

        total = self.leg_fee_inr(entry_notional, entry_side) + self.leg_fee_inr(
            exit_notional, exit_side
        )
        return total / entry_notional * 100.0

    def breakdown(self, notional: float) -> dict[str, float]:
        """Itemised round-trip cost, for checking against Dhan's calculator."""
        n = abs(notional)
        brokerage = self.brokerage_inr(n) * 2
        exchange = n * self.exchange_txn_bps * BPS * 2
        sebi = n * self.sebi_bps * BPS * 2
        stt = n * self.stt_sell_bps * BPS
        stamp = n * self.stamp_buy_bps * BPS
        gst = (self.gst_pct / 100.0) * (brokerage + exchange + sebi)
        total = brokerage + exchange + sebi + stt + stamp + gst
        return {
            "notional": n,
            "brokerage": round(brokerage, 4),
            "stt": round(stt, 4),
            "exchange_txn": round(exchange, 4),
            "sebi": round(sebi, 4),
            "stamp_duty": round(stamp, 4),
            "gst": round(gst, 4),
            "total_inr": round(total, 4),
            "total_bps": round(total / n / BPS, 4) if n else 0.0,
        }
