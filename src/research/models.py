"""Frozen, versioned experiment specifications and output contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, revalidate_instances="always")
    schema_version: int = Field(default=1, ge=1, le=2)
    experiment_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    parent_experiment_id: str | None = None
    hypothesis: str = Field(min_length=1)
    strategy: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)
    git_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    timeframe: str = Field(min_length=1)
    universe: list[str] = Field(min_length=1)
    start: datetime
    end: datetime
    features: list[str]
    parameters: dict[str, Any]
    search_space: dict[str, Any]
    baseline: str = Field(min_length=1)
    cost_model: dict[str, Any]
    slippage: dict[str, Any]
    seed: int = Field(ge=0, strict=True)
    execution_assumptions: dict[str, Any]
    # Explicit file manifests bind dirty-worktree sources as well as datasets.
    artifacts: dict[str, str] = Field(min_length=1)
    environment: dict[str, str] = Field(min_length=1)
    train_end: datetime
    validation_end: datetime
    holdout_end: datetime
    max_drawdown: float = Field(gt=0, lt=1)
    bootstrap_block: int = Field(ge=1, strict=True)
    evaluation_stage: Literal["development", "holdout"] = "development"
    rejection_rules: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def chronology(self):
        dates = (self.start, self.train_end, self.validation_end, self.holdout_end, self.end)
        if any(d.tzinfo is None or d.utcoffset() is None for d in dates):
            raise ValueError("Use timezone-aware experiment boundaries")
        if not self.start < self.train_end < self.validation_end < self.holdout_end <= self.end:
            raise ValueError("Require disjoint chronological train/validation/holdout boundaries")
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("Duplicate universe members")
        if self.parent_experiment_id == self.experiment_id:
            raise ValueError("An experiment cannot parent itself")
        if self.evaluation_stage == "holdout" and not self.parent_experiment_id:
            raise ValueError("A holdout experiment requires a published parent experiment")
        if self.schema_version >= 2 and (not self.rejection_rules
                                         or any(not rule.strip() for rule in self.rejection_rules)):
            raise ValueError("Version 2 experiments require explicit rejection rules")
        return self


class ExperimentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, revalidate_instances="always")
    timestamps: list[datetime] = Field(min_length=2)
    net_returns: list[float] = Field(min_length=2)
    baseline_returns: list[float] = Field(min_length=2)
    turnover: list[float] = Field(min_length=2)

    @model_validator(mode="after")
    def aligned(self):
        if len({len(self.timestamps), len(self.net_returns), len(self.baseline_returns), len(self.turnover)}) != 1:
            raise ValueError("Output series must be paired")
        if any(t.tzinfo is None or t.utcoffset() is None for t in self.timestamps):
            raise ValueError("Output timestamps must be timezone-aware")
        if any(a >= b for a, b in zip(self.timestamps, self.timestamps[1:], strict=False)):
            raise ValueError("Output timestamps must increase strictly")
        if any(r <= -1 for r in self.net_returns + self.baseline_returns):
            raise ValueError("Returns imply insolvency or invalid accounting")
        if any(t < 0 for t in self.turnover):
            raise ValueError("Turnover cannot be negative")
        return self


class ExperimentResult(BaseModel):
    """Durable terminal outcome; failed attempts cannot carry invented returns."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, revalidate_instances="always")
    status: Literal["completed", "failed"]
    output: ExperimentOutput | None = None
    error_type: str | None = Field(default=None, min_length=1)
    resolution: dict[str, str] | None = None

    @model_validator(mode="after")
    def outcome(self):
        if self.status == "completed":
            if self.output is None or self.error_type is not None or self.resolution is not None:
                raise ValueError("Completed result requires output only")
        elif self.output is not None or not self.error_type:
            raise ValueError("Failed result requires error_type and no output")
        if self.resolution is not None:
            if set(self.resolution) != {"operator", "reason"} or not all(v.strip() for v in self.resolution.values()):
                raise ValueError("Failure resolution requires operator and reason")
        return self

    def validate_spec(self, spec: ExperimentSpec) -> None:
        if self.output is not None:
            if self.output.timestamps[0] < spec.start or self.output.timestamps[-1] > spec.end:
                raise ValueError("Output outside registered dates")
            if len(self.output.net_returns) < spec.bootstrap_block:
                raise ValueError("Output shorter than registered bootstrap block")
