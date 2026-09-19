"""Versioned forward-universe contract with captured market provenance."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class UniverseMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    symbol: str = Field(pattern=r"^NSE:.+-EQ$")
    isin: str = Field(pattern=r"^INE[0-9A-Z]{9}$")
    reference_price: float = Field(gt=0)
    turnover_inr: float = Field(gt=0)


class ForwardUniverse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(ge=1, le=1)
    universe_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{4,99}$")
    captured_at: date
    source: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection: str = Field(min_length=1)
    members: list[UniverseMember] = Field(min_length=20, max_length=25)

    @model_validator(mode="after")
    def unique_identity(self) -> ForwardUniverse:
        if len({row.symbol for row in self.members}) != len(self.members):
            raise ValueError("forward-universe symbols must be unique")
        if len({row.isin for row in self.members}) != len(self.members):
            raise ValueError("forward-universe ISINs must be unique")
        return self

    @classmethod
    def load(cls, path: Path) -> ForwardUniverse:
        return cls.model_validate(yaml.safe_load(path.read_text()))
