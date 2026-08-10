"""SQLite persistence for ingested news.

Shares data/trades.db (and journal.py's declarative Base) so headlines can be
joined against trades with plain SQL. Base.metadata.create_all handles table
creation, so no migration tooling is needed for the new tables.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.orm import sessionmaker

from src.news.models import NewsItem
from src.portfolio.journal import Base

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/trades.db"


class NewsItemModel(Base):
    __tablename__ = "news_items"

    content_hash = Column(String, primary_key=True)
    source = Column(String, nullable=False)
    title = Column(Text, nullable=False)
    url = Column(Text)
    published_at = Column(DateTime, nullable=False)
    # When we actually saw it. Backtests filter on this, never published_at.
    fetched_at = Column(DateTime, nullable=False)
    assets = Column(Text, default="[]")
    votes_important = Column(Integer, default=0)
    source_sentiment = Column(String)
    # Set when a later near-duplicate pass folds this item into another.
    duplicate_of = Column(String)


Index("ix_news_items_published_at", NewsItemModel.published_at)
Index("ix_news_items_fetched_at", NewsItemModel.fetched_at)


class NewsAssetLink(Base):
    """Normalised asset tags, so per-asset queries don't LIKE over JSON."""

    __tablename__ = "news_assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    content_hash = Column(String, nullable=False)
    asset = Column(String, nullable=False)


Index("ix_news_assets_asset", NewsAssetLink.asset)
Index("ix_news_assets_hash", NewsAssetLink.content_hash)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NewsStore:
    """Upsert and query ingested headlines."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # The engine, dashboard and poller all write this file from different
        # processes. WAL plus a busy timeout avoids "database is locked".
        self.engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"timeout": 30}
        )

        @event.listens_for(self.engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def upsert_many(self, items: Iterable[NewsItem]) -> int:
        """Insert items not already stored. Returns the number newly inserted."""
        items = list(items)
        if not items:
            return 0

        inserted = 0
        with self.Session() as session:
            hashes = {item.content_hash for item in items}
            existing = set(
                session.scalars(
                    select(NewsItemModel.content_hash).where(
                        NewsItemModel.content_hash.in_(hashes)
                    )
                ).all()
            )

            seen_this_batch: set[str] = set()
            for item in items:
                if item.content_hash in existing or item.content_hash in seen_this_batch:
                    continue
                seen_this_batch.add(item.content_hash)

                session.add(
                    NewsItemModel(
                        content_hash=item.content_hash,
                        source=item.source,
                        title=item.title,
                        url=item.url,
                        published_at=item.published_at,
                        fetched_at=item.fetched_at,
                        assets=json.dumps(item.assets),
                        votes_important=item.votes_important,
                        source_sentiment=item.source_sentiment,
                    )
                )
                for asset in dict.fromkeys(item.assets):
                    session.add(
                        NewsAssetLink(content_hash=item.content_hash, asset=asset)
                    )
                inserted += 1

            session.commit()
        return inserted

    def recent_for_asset(
        self,
        asset: str,
        *,
        limit: int = 8,
        max_age_hours: float = 24.0,
        as_of: Optional[datetime] = None,
    ) -> list[NewsItem]:
        """Most recent headlines for an asset that were *available* by `as_of`.

        Filtering on fetched_at rather than published_at is what keeps this
        honest in a backtest.
        """
        as_of = as_of or _utcnow()
        cutoff = as_of - timedelta(hours=max_age_hours)

        with self.Session() as session:
            rows = session.execute(
                select(NewsItemModel)
                .join(
                    NewsAssetLink,
                    NewsAssetLink.content_hash == NewsItemModel.content_hash,
                )
                .where(
                    NewsAssetLink.asset == asset.upper(),
                    NewsItemModel.fetched_at <= as_of,
                    NewsItemModel.fetched_at >= cutoff,
                    NewsItemModel.duplicate_of.is_(None),
                )
                .order_by(NewsItemModel.published_at.desc())
                .limit(limit)
            ).scalars().all()

        return [
            NewsItem(
                content_hash=row.content_hash,
                source=row.source,
                title=row.title,
                url=row.url,
                published_at=row.published_at,
                fetched_at=row.fetched_at,
                assets=json.loads(row.assets or "[]"),
                votes_important=row.votes_important or 0,
                source_sentiment=row.source_sentiment,
            )
            for row in rows
        ]

    def count(self) -> int:
        with self.Session() as session:
            return int(session.scalar(select(func.count()).select_from(NewsItemModel)) or 0)

    def stats(self) -> dict[str, int]:
        """Row counts by source, for operational sanity checks."""
        with self.Session() as session:
            rows = session.execute(
                select(NewsItemModel.source, func.count())
                .group_by(NewsItemModel.source)
                .order_by(func.count().desc())
            ).all()
        by_source = {source: int(n) for source, n in rows}
        return {"total": sum(by_source.values()), **by_source}

    def asset_stats(self) -> dict[str, int]:
        """Row counts by asset tag — the practical coverage map."""
        with self.Session() as session:
            rows = session.execute(
                select(NewsAssetLink.asset, func.count())
                .group_by(NewsAssetLink.asset)
                .order_by(func.count().desc())
            ).all()
        return {asset: int(n) for asset, n in rows}
