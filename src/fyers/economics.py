"""Shared FYERS cash-equity sizing, reservation and conservative fill rules."""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from pydantic import BaseModel, ConfigDict, Field

from src.fyers.costs import FyersCosts
from src.fyers.models import Instrument, Quote, Side


def settlement_day(trade_day: date, *, business_days: int = 1,
                   holidays: frozenset[date] = frozenset()) -> date:
    """Compute conservative T+n settlement from an explicit exchange holiday set."""
    if type(business_days) is not int or business_days < 0:
        raise ValueError("Settlement business days must be non-negative")
    due = trade_day
    remaining = business_days
    while remaining:
        due += timedelta(days=1)
        if due.weekday() < 5 and due not in holidays:
            remaining -= 1
    return due


class FillEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)
    requested_quantity: int = Field(gt=0)
    filled_quantity: int = Field(ge=0)
    remaining_quantity: int = Field(ge=0)
    price: float | None = Field(default=None, gt=0)
    notional: float = Field(ge=0)
    fee: float = Field(ge=0)
    cash_required: float = Field(ge=0)
    status: str


def estimate_fill(*, side: Side, quantity: int, instrument: Instrument, quote: Quote,
                  slippage_bps: float, costs: FyersCosts, delivery: bool = True,
                  charge_dp: bool = False, allow_partial: bool = False) -> FillEstimate:
    """Price one executable quote without inventing liquidity or fractional shares."""
    if type(quantity) is not int or quantity <= 0 or quantity % instrument.lot_size:
        raise ValueError("Quantity must be a positive whole lot")
    if not math.isfinite(slippage_bps) or not 0 <= slippage_bps <= 100:
        raise ValueError("Invalid slippage")
    available = quote.ask_size if side == Side.BUY else quote.bid_size
    filled = min(quantity, available) if allow_partial else (quantity if quantity <= available else 0)
    if not filled:
        return FillEstimate(requested_quantity=quantity, filled_quantity=0,
                            remaining_quantity=quantity, notional=0, fee=0,
                            cash_required=0, status="UNFILLED")
    reference = quote.ask if side == Side.BUY else quote.bid
    raw = reference * (1 + (1 if side == Side.BUY else -1) * slippage_bps / 10000)
    rounding = ROUND_CEILING if side == Side.BUY else ROUND_FLOOR
    ticks = (Decimal(str(raw)) / instrument.tick_size).to_integral_value(rounding=rounding)
    price = float(ticks * instrument.tick_size)
    notional = price * filled
    fee = costs.fee(notional, side, delivery=delivery, charge_dp=charge_dp)
    return FillEstimate(requested_quantity=quantity, filled_quantity=filled,
                        remaining_quantity=quantity - filled, price=price,
                        notional=notional, fee=fee,
                        cash_required=notional + fee if side == Side.BUY else 0,
                        status="FILLED" if filled == quantity else "PARTIALLY_FILLED")


def maximum_buy_quantity(*, cash: float, max_notional: float, instrument: Instrument,
                         quote: Quote, slippage_bps: float, costs: FyersCosts) -> int:
    """Return the largest whole-lot buy whose notional and fees fit both limits."""
    if not math.isfinite(cash) or not math.isfinite(max_notional) or cash <= 0 or max_notional <= 0:
        return 0
    upper = min(quote.ask_size, int(min(cash, max_notional) / quote.ask))
    upper -= upper % instrument.lot_size
    while upper > 0:
        fill = estimate_fill(side=Side.BUY, quantity=upper, instrument=instrument,
                             quote=quote, slippage_bps=slippage_bps, costs=costs)
        if fill.notional <= max_notional and fill.cash_required <= cash:
            return upper
        upper -= instrument.lot_size
    return 0
