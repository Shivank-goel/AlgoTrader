"""Dynamic whole-share sizing for the shared FYERS shadow account."""

from __future__ import annotations

import math
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.fyers.economics import maximum_buy_quantity
from src.fyers.models import Instrument, Quote, RuntimeConfig
from src.fyers.paper import PaperBroker


class SizingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    risk_per_trade_fraction: float = Field(gt=0, le=0.1)
    max_position_fraction: float = Field(gt=0, le=1)
    max_gross_exposure_fraction: float = Field(gt=0, le=1)
    max_symbol_exposure_fraction: float = Field(gt=0, le=1)
    max_open_positions: int = Field(gt=0, le=50)
    minimum_cash_buffer_inr: float = Field(ge=0)
    minimum_trade_value_inr: float = Field(gt=0)
    maximum_trade_value_inr: float = Field(gt=0)

    @classmethod
    def load(cls, path: Path) -> SizingConfig:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class SizingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)
    symbol: str
    current_equity: float
    available_cash: float
    risk_fraction: float
    risk_multiplier: float
    risk_budget_rupees: float
    stop_distance: float
    quantity_by_risk: int
    quantity_by_cash: int
    quantity_by_exposure: int
    quantity_by_liquidity: int
    requested_quantity: int
    final_quantity: int
    limiting_constraint: str | None = None
    rejection_reason: str | None = None


class DynamicShadowSizer:
    def __init__(self, paper: PaperBroker, config: RuntimeConfig | None = None,
                 sizing: SizingConfig | None = None) -> None:
        self.paper = paper
        self.runtime = config or paper.config
        self.config = sizing or SizingConfig.load(self.runtime_path)

    @property
    def runtime_path(self) -> Path:
        return Path(__file__).resolve().parents[2] / self.runtime.sizing_file

    def size_long(self, *, symbol: str, requested_quantity: int, reference_price: float,
                  stop_loss: float, instrument: Instrument, quote: Quote,
                  risk_multiplier: float = 1.0) -> SizingDecision:
        if type(requested_quantity) is not int or requested_quantity <= 0:
            raise ValueError("requested quantity must be a positive integer")
        if not all(math.isfinite(v) and v > 0 for v in (reference_price, stop_loss)):
            raise ValueError("sizing prices must be positive")
        if stop_loss >= reference_price or risk_multiplier <= 0:
            raise ValueError("long stop and risk multiplier are invalid")
        equity = float(self.paper.journal.get("paper_valuation", {}).get("equity")
                       or self.paper.journal.get("cash") or 0.0)
        cash = float(self.paper.journal.get("cash") or 0.0)
        if equity <= 0 or cash <= 0:
            return self._decision(symbol, equity, cash, risk_multiplier, 0, requested_quantity,
                                  "INSUFFICIENT_CAPITAL")
        stop_distance = reference_price - stop_loss
        risk_budget = equity * self.config.risk_per_trade_fraction * risk_multiplier
        by_risk = int(risk_budget // stop_distance) // instrument.lot_size * instrument.lot_size
        by_cash = maximum_buy_quantity(
            cash=max(0.0, cash - self.config.minimum_cash_buffer_inr),
            max_notional=self.config.maximum_trade_value_inr,
            instrument=instrument, quote=quote, slippage_bps=self.runtime.slippage_bps,
            costs=self.paper.costs,
        )
        positions = self.paper.journal.db.execute("SELECT symbol,quantity FROM positions WHERE quantity>0").fetchall()
        existing_value = max(0.0, equity - cash)
        gross = existing_value
        if symbol not in {row["symbol"] for row in positions} and len(positions) >= self.config.max_open_positions:
            return self._decision(symbol, equity, cash, risk_multiplier, stop_distance,
                                  requested_quantity, "POSITION_LIMIT")
        symbol_cap = equity * self.config.max_symbol_exposure_fraction
        position_cap = equity * self.config.max_position_fraction
        by_exposure = int(max(0, min(symbol_cap, position_cap) - existing_value) // quote.ask)
        gross_left = max(0, equity * self.config.max_gross_exposure_fraction - gross)
        by_exposure = min(by_exposure, int(gross_left // quote.ask))
        by_liquidity = int(quote.ask_size) // instrument.lot_size * instrument.lot_size
        final = min(requested_quantity, by_risk, by_cash, by_exposure, by_liquidity)
        final -= final % instrument.lot_size
        limits = {"risk": by_risk, "cash": by_cash, "exposure": by_exposure, "liquidity": by_liquidity, "strategy": requested_quantity}
        limiting = min(limits, key=limits.get)
        reason = None if final > 0 and final * quote.ask >= self.config.minimum_trade_value_inr else {
            "risk": "RISK_BUDGET_TOO_SMALL", "cash": "INSUFFICIENT_CAPITAL",
            "exposure": "EXPOSURE_LIMIT", "liquidity": "LIQUIDITY_LIMIT",
            "strategy": "STRATEGY_LIMIT",
        }[limiting]
        return SizingDecision(symbol=symbol, current_equity=equity, available_cash=cash,
                              risk_fraction=self.config.risk_per_trade_fraction,
                              risk_multiplier=risk_multiplier, risk_budget_rupees=risk_budget,
                              stop_distance=stop_distance, quantity_by_risk=by_risk,
                              quantity_by_cash=by_cash, quantity_by_exposure=by_exposure,
                              quantity_by_liquidity=by_liquidity, requested_quantity=requested_quantity,
                              final_quantity=final, limiting_constraint=limiting,
                              rejection_reason=reason)

    def size_short(self, *, symbol: str, requested_quantity: int, reference_price: float,
                   stop_loss: float, instrument: Instrument, quote: Quote,
                   risk_multiplier: float = 1.0) -> SizingDecision:
        if stop_loss <= reference_price:
            raise ValueError("short stop must be above entry reference")
        # The same conservative limits apply; short entry liquidity is the bid.
        short_quote = quote.model_copy(update={"ask": quote.bid, "ask_size": quote.bid_size})
        return self.size_long(symbol=symbol, requested_quantity=requested_quantity,
                              reference_price=stop_loss, stop_loss=reference_price,
                              instrument=instrument, quote=short_quote,
                              risk_multiplier=risk_multiplier)

    @staticmethod
    def _decision(symbol: str, equity: float, cash: float, multiplier: float, stop: float,
                  requested: int, reason: str) -> SizingDecision:
        return SizingDecision(symbol=symbol, current_equity=equity, available_cash=cash,
                              risk_fraction=0, risk_multiplier=multiplier, risk_budget_rupees=0,
                              stop_distance=stop, quantity_by_risk=0, quantity_by_cash=0,
                              quantity_by_exposure=0, quantity_by_liquidity=0,
                              requested_quantity=requested, final_quantity=0,
                              limiting_constraint=reason, rejection_reason=reason)
