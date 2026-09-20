"""Strict normalized official-NSE corporate-action artifact boundary."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CorporateAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    isin: str = Field(pattern=r"^INE[0-9A-Z]{9}$")
    announced_date: date
    ex_date: date
    kind: str
    share_multiplier: int = Field(default=1, ge=1)
    cash_per_share: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def supported(self) -> CorporateAction:
        if self.announced_date > self.ex_date:
            raise ValueError("corporate action announcement cannot follow its ex-date")
        if self.kind not in {"split", "bonus", "dividend"}:
            raise ValueError("unsupported corporate action; fail rather than guess")
        if self.kind in {"split", "bonus"} and self.share_multiplier <= 1:
            raise ValueError("split/bonus needs an integer share multiplier")
        if self.kind == "dividend" and self.cash_per_share <= 0:
            raise ValueError("dividend needs positive cash_per_share")
        return self


def load_corporate_actions(path: Path) -> list[CorporateAction]:
    """Load a normalized export derived from the registered official NSE report."""
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        expected = ["isin", "announced_date", "ex_date", "kind", "share_multiplier",
                    "cash_per_share"]
        if reader.fieldnames != expected:
            raise ValueError("invalid normalized NSE corporate-action columns")
        actions = [CorporateAction.model_validate(row) for row in reader]
    identities = [(row.isin, row.ex_date, row.kind) for row in actions]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate corporate action identity")
    return sorted(actions, key=lambda row: (row.ex_date, row.isin, row.kind))
