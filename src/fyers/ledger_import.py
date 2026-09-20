"""Read-only reconciliation of normalized FYERS ledger/charge exports."""

from __future__ import annotations

import csv
import hashlib
import math
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from src.fyers.journal import Journal


class LedgerRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    trade_date: date
    reference: str = Field(min_length=1)
    category: str = Field(min_length=1)
    amount_inr: float


def reconcile_ledger_export(path: Path, journal: Journal, *, tolerance_inr: float = 1.0) -> dict:
    """Compare a normalized, operator-exported report with modelled paper fees.

    The source is never modified. Required columns are deliberately strict so a
    FYERS report-format change cannot silently alter accounting semantics.
    """
    if not math.isfinite(tolerance_inr) or tolerance_inr < 0:
        raise ValueError("reconciliation tolerance must be finite and non-negative")
    raw = path.read_bytes()
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["trade_date", "reference", "category", "amount_inr"]:
            raise ValueError("expected normalized FYERS ledger columns: trade_date,reference,category,amount_inr")
        rows = [LedgerRow.model_validate(row) for row in reader]
    if not rows or len({row.reference for row in rows}) != len(rows):
        raise ValueError("FYERS ledger export is empty or contains duplicate references")
    actual = sum(abs(row.amount_inr) for row in rows if row.category.lower() in {
        "brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp",
    })
    start, end = min(row.trade_date for row in rows), max(row.trade_date for row in rows)
    modelled = journal.db.execute(
        "SELECT COALESCE(SUM(fee),0) FROM fills WHERE date(timestamp,'unixepoch') BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()[0]
    difference = float(actual - modelled)
    report = {
        "source_sha256": hashlib.sha256(raw).hexdigest(), "rows": len(rows),
        "start": start.isoformat(), "end": end.isoformat(),
        "actual_charges_inr": actual, "modelled_charges_inr": modelled,
        "difference_inr": difference, "within_tolerance": abs(difference) <= tolerance_inr,
        "read_only": True,
    }
    with journal.db:
        journal.put("ledger_reconciliation", report)
    return report
