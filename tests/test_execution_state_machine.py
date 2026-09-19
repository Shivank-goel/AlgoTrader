import pytest

from src.execution.state_machine import validate_transition


@pytest.mark.parametrize("old,new,previous,filled", [
    ("SUBMITTING", "UNKNOWN", 0, 0),
    ("UNKNOWN", "FILLED", 0, 10),
    ("ACKNOWLEDGED", "PARTIALLY_FILLED", 0, 4),
    ("PARTIALLY_FILLED", "CANCEL_REQUESTED", 4, 4),
    ("CANCEL_REQUESTED", "FILLED", 4, 10),
    ("PARTIALLY_FILLED", "CANCELLED", 4, 4),
    ("FILLED", "FILLED", 10, 10),
])
def test_valid_transitions(old, new, previous, filled):
    validate_transition(old, new, quantity=10, previous_filled=previous, filled=filled)


@pytest.mark.parametrize("old,new,previous,filled", [
    ("UNKNOWN", "SUBMITTING", 0, 0),
    ("PARTIALLY_FILLED", "ACKNOWLEDGED", 4, 4),
    ("ACKNOWLEDGED", "FILLED", 0, 4),
    ("PARTIALLY_FILLED", "PARTIALLY_FILLED", 4, 3),
    ("CANCELLED", "CANCELLED", 4, 5),
    ("FILLED", "SUBMITTING", 10, 10),
])
def test_illegal_transitions(old, new, previous, filled):
    with pytest.raises(ValueError):
        validate_transition(old, new, quantity=10, previous_filled=previous, filled=filled)
