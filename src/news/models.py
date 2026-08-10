"""Data models for ingested news."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace.

    Used for dedup keys so "Bitcoin ETF Approved!" and "Bitcoin ETF approved"
    hash identically.
    """
    lowered = title.strip().lower()
    lowered = _NON_ALNUM.sub(" ", lowered)
    return _WHITESPACE.sub(" ", lowered).strip()


def content_hash(title: str, published_at: datetime) -> str:
    """Stable dedup key: normalized title + publication date.

    Deliberately date-granular rather than timestamp-granular — syndicating
    outlets publish the same story minutes apart with slightly different
    timestamps.
    """
    key = f"{normalize_title(title)}|{published_at.date().isoformat()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class NewsItem(BaseModel):
    """A single headline as ingested from a source.

    `published_at` is when the outlet says it published. `fetched_at` is when
    we actually saw it. Backtests must filter on `fetched_at`, never
    `published_at` — filtering on the latter hands the strategy the
    publication-to-availability latency for free, which is pure lookahead.
    """

    content_hash: str
    source: str
    title: str
    url: Optional[str] = None
    published_at: datetime
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    assets: list[str] = Field(default_factory=list)
    votes_important: int = 0
    source_sentiment: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        source: str,
        title: str,
        published_at: datetime,
        url: Optional[str] = None,
        assets: Optional[list[str]] = None,
        votes_important: int = 0,
        source_sentiment: Optional[str] = None,
        raw: Optional[dict[str, Any]] = None,
    ) -> NewsItem:
        return cls(
            content_hash=content_hash(title, published_at),
            source=source,
            title=title,
            url=url,
            published_at=published_at,
            assets=assets or [],
            votes_important=votes_important,
            source_sentiment=source_sentiment,
            raw=raw or {},
        )
