"""Versioned tax/infrastructure scenarios and benchmark-relative economics."""

from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.fyers.models import ROOT


class TaxPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: int = Field(ge=1, le=1)
    effective_from: date
    short_term_days: int = Field(ge=1)
    stcg_rate: float = Field(ge=0, le=1)
    ltcg_rate: float = Field(ge=0, le=1)
    ltcg_annual_exemption_inr: float = Field(ge=0)
    cess_rate: float = Field(ge=0, le=1)
    surcharge_rate: float = Field(ge=0, le=1)
    source: str = Field(min_length=1)

    @classmethod
    def load(cls, path: Path = ROOT / "config/fyers_economics.yaml") -> TaxPolicy:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def tax(self, short_term_gain: float, long_term_gain: float) -> float:
        if not all(math.isfinite(value) for value in (short_term_gain, long_term_gain)):
            raise ValueError("capital gains must be finite")
        base = max(0.0, short_term_gain) * self.stcg_rate
        base += max(0.0, long_term_gain - self.ltcg_annual_exemption_inr) * self.ltcg_rate
        return base * (1 + self.cess_rate) * (1 + self.surcharge_rate)


class EconomicReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    observations: int = Field(ge=2)
    gross_strategy_return: float
    benchmark_return: float
    pre_tax_excess_return: float
    estimated_tax_inr: float = Field(ge=0)
    infrastructure_cost_inr: float = Field(ge=0)
    after_tax_and_infrastructure_excess_return: float
    excess_mean_ci95: tuple[float, float]
    break_even_capital_inr: float | None = Field(default=None, gt=0)


def economic_report(
    returns: list[float], benchmark: list[float], timestamps: list[datetime], *,
    capital_inr: float, monthly_infrastructure_inr: float, policy: TaxPolicy,
    bootstrap_block: int, seed: int,
) -> EconomicReport:
    """Conservative scenario; personal tax treatment still requires review."""
    strategy = np.asarray(returns, dtype=float)
    baseline = np.asarray(benchmark, dtype=float)
    if (len(strategy) != len(baseline) or len(strategy) < 2
            or not np.isfinite(strategy).all() or not np.isfinite(baseline).all()
            or len(timestamps) != len(strategy)
            or any(a >= b for a, b in zip(timestamps, timestamps[1:], strict=False))
            or (strategy <= -1).any() or (baseline <= -1).any()
            or capital_inr <= 0 or monthly_infrastructure_inr < 0):
        raise ValueError("invalid economic-report inputs")
    from src.research.validation import block_bootstrap
    gross = float(np.prod(1 + strategy) - 1)
    passive = float(np.prod(1 + baseline) - 1)
    elapsed_days = max(1, (timestamps[-1] - timestamps[0]).days)
    months = elapsed_days / 365.2425 * 12
    infrastructure = monthly_infrastructure_inr * months
    # Period returns do not identify exact tax lots. Treat every positive realised
    # period as short-term; qualification may replace this with the FIFO-lot report.
    taxable_gain = float(np.maximum(strategy, 0).sum() * capital_inr)
    tax = policy.tax(taxable_gain, 0)
    after = gross - passive - (tax + infrastructure) / capital_inr
    lower, upper = block_bootstrap((strategy - baseline).tolist(), block=bootstrap_block,
                                   seed=seed)
    annual_excess = (1 + max(-0.999999, gross - passive)) ** (365.2425 / elapsed_days) - 1
    break_even = (monthly_infrastructure_inr * 12 / annual_excess
                  if annual_excess > 0 and monthly_infrastructure_inr > 0 else None)
    return EconomicReport(
        observations=len(strategy), gross_strategy_return=gross,
        benchmark_return=passive, pre_tax_excess_return=gross - passive,
        estimated_tax_inr=tax, infrastructure_cost_inr=infrastructure,
        after_tax_and_infrastructure_excess_return=after,
        excess_mean_ci95=(lower, upper), break_even_capital_inr=break_even,
    )
