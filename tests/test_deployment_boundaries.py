import pytest

from src.execution.reconciliation import PositionViews
from src.risk.deployment import paper_execution


@pytest.mark.parametrize("mode", ["live", "LIVE", "papre", "shadow", "", None, False])
def test_unreleased_or_invalid_mode_never_enables_live(mode):
    with pytest.raises(ValueError):
        paper_execution(mode)


def test_only_paper_is_released():
    assert paper_execution("paper") is True


def test_manual_positions_cannot_be_silently_adopted():
    result = PositionViews(intended={"A": 2}, local={"A": 1}, broker={"A": 1, "EXTERNAL": 1}).reconcile()
    assert result["entry_blocked"] and result["reconciled"] is None
    assert result["target_delta"] == {"A": 1}


def test_pending_target_is_not_a_broker_discrepancy():
    result = PositionViews(intended={"A": 2}, local={"A": 1}, broker={"A": 1}).reconcile()
    assert not result["entry_blocked"]
    assert result["reconciled"] == {"A": 1}
