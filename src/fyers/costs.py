"""FYERS published NSE cash tariff; distinct from historical Dhan research."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.fyers.models import ROOT, Side


class FyersCosts(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    tariff_version: str = Field(min_length=1)
    effective_from: date
    brokerage_intraday_bps: float = Field(ge=0)
    brokerage_delivery_bps: float = Field(ge=0)
    brokerage_cap_inr: float = Field(ge=0)
    exchange_bps: float = Field(ge=0)
    sebi_bps: float = Field(ge=0)
    ipft_bps: float = Field(ge=0)
    gst_rate: float = Field(ge=0, le=1)
    stt_intraday_sell_bps: float = Field(ge=0)
    stt_delivery_bps: float = Field(ge=0)
    stamp_intraday_buy_bps: float = Field(ge=0)
    stamp_delivery_buy_bps: float = Field(ge=0)
    dp_sell_inr: float = Field(ge=0)

    @classmethod
    def load(cls, path: Path = ROOT / "config/fyers_costs.yaml") -> FyersCosts:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def fee(self, notional: float, side: Side, *, delivery: bool,
            charge_dp: bool = False) -> float:
        """DP must be charged by caller once per ISIN/day, not once per fill."""
        import math
        if not math.isfinite(notional) or notional <= 0:
            raise ValueError("Notional must be positive and finite")
        rate = self.brokerage_delivery_bps if delivery else self.brokerage_intraday_bps
        brokerage = min(self.brokerage_cap_inr, notional * rate / 10000)
        taxable = brokerage + notional * (self.exchange_bps + self.sebi_bps + self.ipft_bps) / 10000
        if delivery and side == Side.SELL and charge_dp:
            taxable += self.dp_sell_inr
        stt = self.stt_delivery_bps if delivery else (self.stt_intraday_sell_bps if side == Side.SELL else 0)
        stamp = (self.stamp_delivery_buy_bps if delivery else self.stamp_intraday_buy_bps) if side == Side.BUY else 0
        return taxable * (1 + self.gst_rate) + notional * (stt + stamp) / 10000

    @property
    def provenance(self) -> dict[str, str]:
        return {"tariff_version": self.tariff_version,
                "effective_from": self.effective_from.isoformat()}
