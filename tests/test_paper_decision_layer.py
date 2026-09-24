from src.fyers.models import Side
from src.shadow.decisions import target_deltas
from src.strategies.hierarchical import PortfolioAllocator


def test_allocator_is_whole_share_and_capped():
    allocator = PortfolioAllocator(capital_inr=10000, per_position_inr=2000, max_positions=2)
    targets, rejected = allocator.allocate([("NSE:A-EQ", 2, 100), ("NSE:B-EQ", 1, 500), ("NSE:C-EQ", 0, 100)])
    assert targets == {"NSE:A-EQ": 20, "NSE:B-EQ": 4}
    assert rejected == [{"symbol": "NSE:C-EQ", "reason": "position_limit"}]


def test_target_deltas_create_entries_and_exits_deterministically():
    result = target_deltas({"NSE:A-EQ": 4, "NSE:C-EQ": 1}, {"NSE:A-EQ": 2, "NSE:B-EQ": 3})
    assert result == {
        "NSE:A-EQ": (Side.BUY, 2), "NSE:B-EQ": (Side.SELL, 3), "NSE:C-EQ": (Side.BUY, 1),
    }
