from datetime import UTC, datetime, timedelta

import pytest

from src.data.nse.corporate_actions import load_corporate_actions
from src.fyers.daily_data import _artifact, _publish, completed_bar_panel
from src.fyers.journal import Journal
from src.fyers.ledger_import import reconcile_ledger_export
from src.fyers.order_stream import DisabledOrderStream
from src.fyers.profitability import TaxPolicy, economic_report


def bar_payload(day: str, close: float) -> dict:
    return {
        "schema_version": 1, "session_date": day, "source": "fixture",
        "captured_at": f"{day}T12:00:00+00:00", "universe_sha256": "a" * 64,
        "bars": {"NSE:A-EQ": {"open": close - 1, "high": close + 1,
                                  "low": close - 2, "close": close, "volume": 100}},
        "benchmark_symbol": "NSE:NIFTY50-INDEX",
        "benchmark": {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
        "missing_symbols": [],
    }


def test_completed_bar_artifacts_are_immutable_and_replayable(tmp_path):
    first = _artifact(bar_payload("2026-09-17", 100))
    second = _artifact(bar_payload("2026-09-18", 101))
    _publish(tmp_path / "2026-09-17.json", first)
    _publish(tmp_path / "2026-09-18.json", second)
    closes, opens, benchmark, artifacts = completed_bar_panel(tmp_path, ["NSE:A-EQ"])
    assert closes.iloc[-1, 0] == 101 and opens.iloc[-1, 0] == 100
    assert benchmark.iloc[-1] == 101 and [a.content_sha256 for a in artifacts] == [first.content_sha256, second.content_sha256]
    retry_payload = bar_payload("2026-09-18", 101)
    retry_payload["captured_at"] = "2026-09-19T12:00:00+00:00"
    assert _publish(tmp_path / "2026-09-18.json", _artifact(retry_payload)).content_sha256 == second.content_sha256
    changed = _artifact(bar_payload("2026-09-18", 999))
    with pytest.raises(ValueError, match="different data"):
        _publish(tmp_path / "2026-09-18.json", changed)


def test_disabled_order_stream_deduplicates_and_cannot_connect(tmp_path):
    journal = Journal(tmp_path / "runtime.db")
    stream = DisabledOrderStream(journal)
    payload = {"trade_id": "t1", "order_id": "o1", "symbol": "NSE:SBIN-EQ",
               "quantity": 1, "price": 100.0, "side": 1,
               "timestamp": datetime.now(UTC).isoformat()}
    assert stream.accept_trade(payload, received=1000)
    assert not stream.accept_trade(payload, received=1001)
    with pytest.raises(ValueError, match="not authorized"):
        DisabledOrderStream(journal, enabled=True)
    journal.close()


def test_economic_report_includes_tax_infrastructure_and_break_even():
    policy = TaxPolicy.load()
    start = datetime(2025, 1, 1, tzinfo=UTC)
    timestamps = [start + timedelta(days=30 * i) for i in range(12)]
    report = economic_report(
        [0.02] * 12, [0.005] * 12, timestamps, capital_inr=10000,
        monthly_infrastructure_inr=10, policy=policy, bootstrap_block=2, seed=1,
    )
    assert report.estimated_tax_inr > 0 and report.infrastructure_cost_inr > 0
    assert report.break_even_capital_inr is not None


def test_normalized_ledger_import_is_read_only_and_hash_bound(tmp_path):
    journal = Journal(tmp_path / "runtime.db")
    path = tmp_path / "ledger.csv"
    path.write_text("trade_date,reference,category,amount_inr\n2026-09-18,r1,brokerage,12.50\n")
    before = path.read_bytes()
    report = reconcile_ledger_export(path, journal)
    assert report["actual_charges_inr"] == 12.5 and path.read_bytes() == before
    assert journal.get("ledger_reconciliation")["source_sha256"] == report["source_sha256"]
    journal.close()


def test_corporate_action_artifact_is_strict_and_point_in_time(tmp_path):
    path = tmp_path / "actions.csv"
    path.write_text(
        "isin,announced_date,ex_date,kind,share_multiplier,cash_per_share\n"
        "INE062A01020,2026-08-01,2026-08-15,split,2,0\n"
    )
    action = load_corporate_actions(path)[0]
    assert action.announced_date < action.ex_date and action.share_multiplier == 2
    path.write_text(
        "isin,announced_date,ex_date,kind,share_multiplier,cash_per_share\n"
        "INE062A01020,2026-08-20,2026-08-15,split,2,0\n"
    )
    with pytest.raises(ValueError, match="announcement"):
        load_corporate_actions(path)
