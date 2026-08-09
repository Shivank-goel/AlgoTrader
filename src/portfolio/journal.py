"""Trade journal with SQLite persistence."""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.core.models import Regime, TradeRecord

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class TradeModel(Base):
    __tablename__ = "trades"

    id = Column(String, primary_key=True)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)
    strategy_name = Column(String, nullable=False)
    regime = Column(String, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float)
    size = Column(Float, nullable=False)
    pnl = Column(Float, default=0.0)
    pnl_pct = Column(Float, default=0.0)
    fees = Column(Float, default=0.0)
    entry_time = Column(DateTime, nullable=False)
    exit_time = Column(DateTime)
    duration_seconds = Column(Integer)
    indicators_at_entry = Column(Text, default="{}")
    exit_reason = Column(String)


class ReassignmentModel(Base):
    __tablename__ = "reassignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False)
    symbol = Column(String, nullable=False)
    old_strategy = Column(String, nullable=False)
    new_strategy = Column(String, nullable=False)
    old_score = Column(Float, default=0.0)
    new_score = Column(Float, default=0.0)
    old_trades = Column(Integer, default=0)
    new_trades = Column(Integer, default=0)
    new_t_stat = Column(Float, default=0.0)
    significance_met = Column(Boolean, default=False)
    margin_met = Column(Boolean, default=False)
    reason = Column(String, nullable=False)


class TradeJournal:
    """SQLite-backed trade journal."""

    def __init__(self, db_path: str = "data/trades.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def record_trade(self, trade: TradeRecord) -> None:
        with self.Session() as session:
            model = TradeModel(
                id=trade.id,
                symbol=trade.symbol,
                side=trade.side.value,
                strategy_name=trade.strategy_name,
                regime=trade.regime.value if isinstance(trade.regime, Regime) else str(trade.regime),
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                size=trade.size,
                pnl=trade.pnl,
                pnl_pct=trade.pnl_pct,
                fees=trade.fees,
                entry_time=trade.entry_time,
                exit_time=trade.exit_time,
                duration_seconds=trade.duration_seconds,
                indicators_at_entry=json.dumps(trade.indicators_at_entry),
                exit_reason=trade.exit_reason,
            )
            session.merge(model)
            session.commit()

    def get_trades(
        self,
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
        limit: int = 100,
    ) -> list[TradeRecord]:
        with self.Session() as session:
            query = select(TradeModel).order_by(TradeModel.entry_time.desc())
            if symbol:
                query = query.where(TradeModel.symbol == symbol)
            if strategy:
                query = query.where(TradeModel.strategy_name == strategy)
            query = query.limit(limit)

            results = session.execute(query).scalars().all()
            return [self._to_record(m) for m in results]

    def get_trades_by_strategy(self) -> dict[str, list[TradeRecord]]:
        trades = self.get_trades(limit=1000)
        grouped: dict[str, list[TradeRecord]] = {}
        for t in trades:
            grouped.setdefault(t.strategy_name, []).append(t)
        return grouped

    def record_reassignment(
        self,
        *,
        symbol: str,
        old_strategy: str,
        new_strategy: str,
        old_score: float,
        new_score: float,
        old_trades: int,
        new_trades: int,
        new_t_stat: float,
        significance_met: bool,
        margin_met: bool,
        reason: str,
        timestamp: Optional[datetime] = None,
    ) -> None:
        with self.Session() as session:
            model = ReassignmentModel(
                timestamp=timestamp or datetime.utcnow(),
                symbol=symbol,
                old_strategy=old_strategy,
                new_strategy=new_strategy,
                old_score=old_score,
                new_score=new_score,
                old_trades=old_trades,
                new_trades=new_trades,
                new_t_stat=new_t_stat,
                significance_met=significance_met,
                margin_met=margin_met,
                reason=reason,
            )
            session.add(model)
            session.commit()

    def get_reassignments(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.Session() as session:
            query = (
                select(ReassignmentModel)
                .order_by(ReassignmentModel.timestamp.desc())
                .limit(limit)
            )
            rows = session.execute(query).scalars().all()
            return [
                {
                    "id": row.id,
                    "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                    "symbol": row.symbol,
                    "old_strategy": row.old_strategy,
                    "new_strategy": row.new_strategy,
                    "old_score": row.old_score,
                    "new_score": row.new_score,
                    "old_trades": row.old_trades,
                    "new_trades": row.new_trades,
                    "new_t_stat": row.new_t_stat,
                    "significance_met": row.significance_met,
                    "margin_met": row.margin_met,
                    "reason": row.reason,
                }
                for row in rows
            ]

    def export_csv(self, path: str) -> None:
        trades = self.get_trades(limit=10000)
        if not trades:
            return
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "id", "symbol", "side", "strategy_name", "regime",
                "entry_price", "exit_price", "size", "pnl", "pnl_pct",
                "entry_time", "exit_time", "exit_reason",
            ])
            writer.writeheader()
            for t in trades:
                writer.writerow({
                    "id": t.id,
                    "symbol": t.symbol,
                    "side": t.side.value,
                    "strategy_name": t.strategy_name,
                    "regime": t.regime.value if isinstance(t.regime, Regime) else t.regime,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "size": t.size,
                    "pnl": t.pnl,
                    "pnl_pct": t.pnl_pct,
                    "entry_time": t.entry_time.isoformat() if t.entry_time else "",
                    "exit_time": t.exit_time.isoformat() if t.exit_time else "",
                    "exit_reason": t.exit_reason,
                })

    @staticmethod
    def _to_record(model: TradeModel) -> TradeRecord:
        from src.core.models import Direction

        return TradeRecord(
            id=model.id,
            symbol=model.symbol,
            side=Direction(model.side),
            strategy_name=model.strategy_name,
            regime=Regime(model.regime) if model.regime in [r.value for r in Regime] else Regime.UNKNOWN,
            entry_price=model.entry_price,
            exit_price=model.exit_price,
            size=model.size,
            pnl=model.pnl,
            pnl_pct=model.pnl_pct,
            fees=model.fees,
            entry_time=model.entry_time,
            exit_time=model.exit_time,
            duration_seconds=model.duration_seconds,
            indicators_at_entry=json.loads(model.indicators_at_entry or "{}"),
            exit_reason=model.exit_reason,
        )
