"""Deterministic order transitions; UNKNOWN never authorizes resubmission."""

from enum import Enum


class ExecutionState(str, Enum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


S = ExecutionState
TRANSITIONS = {
    S.CREATED: {S.VALIDATED, S.REJECTED},
    S.VALIDATED: {S.SUBMITTING, S.REJECTED},
    S.SUBMITTING: {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.REJECTED, S.UNKNOWN, S.CANCELLED, S.EXPIRED},
    S.ACKNOWLEDGED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCEL_REQUESTED, S.CANCELLED, S.REJECTED, S.EXPIRED, S.UNKNOWN},
    S.PARTIALLY_FILLED: {S.FILLED, S.CANCEL_REQUESTED, S.CANCELLED, S.EXPIRED, S.UNKNOWN},
    S.CANCEL_REQUESTED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.EXPIRED, S.ACKNOWLEDGED, S.UNKNOWN},
    S.UNKNOWN: {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED},
    S.FILLED: set(), S.CANCELLED: set(), S.REJECTED: set(), S.EXPIRED: set(),
}


def validate_transition(old: str, new: str, *, quantity: int, previous_filled: int, filled: int) -> None:
    before, after = S(old), S(new)
    if any(type(v) is not int for v in (quantity, previous_filled, filled)) or not 0 <= previous_filled <= filled <= quantity or quantity <= 0:
        raise ValueError("Invalid cumulative fill quantity")
    if before != after and after not in TRANSITIONS[before]:
        raise ValueError(f"Illegal order transition: {before.value} -> {after.value}")
    if before in {S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED} and filled != previous_filled:
        raise ValueError("Terminal order changed; requires investigation")
    if after == S.FILLED and filled != quantity:
        raise ValueError("Filled status requires the full quantity")
    if after == S.PARTIALLY_FILLED and not 0 < filled < quantity:
        raise ValueError("Partial status requires a partial quantity")
    if after in {S.CREATED, S.VALIDATED, S.SUBMITTING, S.ACKNOWLEDGED, S.REJECTED} and filled:
        raise ValueError("Status conflicts with executed quantity")
