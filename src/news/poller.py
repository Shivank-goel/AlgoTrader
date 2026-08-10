"""Periodic news collection.

Runs independently of the trading engine. Nothing here can block a scan, and
a total collection outage is invisible to trading — the agent gate simply sees
no news and stays neutral.

Start this early and leave it running: with no backfillable free history
source, the training set for Phase 3 only begins accumulating from the moment
the poller first runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Sequence

import aiohttp

from src.news.client import (
    CoinGeckoNewsClient,
    CryptoPanicClient,
    NewsSource,
    RSSNewsClient,
)
from src.news.mapping import covered_assets
from src.news.store import NewsStore

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 900  # 15 min


def build_default_sources() -> list[NewsSource]:
    """RSS always; keyed sources only when credentials are present.

    Keys are optional by design — the pipeline must work on day one without
    anyone signing up for anything.
    """
    sources: list[NewsSource] = [RSSNewsClient()]

    cryptopanic_token = os.getenv("CRYPTOPANIC_AUTH_TOKEN", "").strip()
    if cryptopanic_token:
        sources.append(
            CryptoPanicClient(cryptopanic_token, currencies=covered_assets())
        )
        logger.info("CryptoPanic source enabled")

    coingecko_key = os.getenv("COINGECKO_API_KEY", "").strip()
    if coingecko_key:
        sources.append(CoinGeckoNewsClient(coingecko_key))
        logger.info("CoinGecko source enabled")

    return sources


class NewsPoller:
    """Polls every configured source on an interval and stores new items."""

    def __init__(
        self,
        store: Optional[NewsStore] = None,
        sources: Optional[Sequence[NewsSource]] = None,
        *,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self.store = store or NewsStore()
        self.sources = list(sources) if sources is not None else build_default_sources()
        self.interval_seconds = interval_seconds

    async def poll_once(self, session: aiohttp.ClientSession) -> int:
        """Fetch from every source and persist. Returns rows newly inserted."""
        results = await asyncio.gather(
            *(source.fetch(session) for source in self.sources),
            return_exceptions=True,
        )

        items = []
        for source, outcome in zip(self.sources, results):
            if isinstance(outcome, BaseException):
                # Clients promise not to raise; if one does, that's a bug in the
                # client, not a reason to take the poller down.
                logger.error(
                    "News source %s raised (it should not): %s", source.name, outcome
                )
                continue
            logger.debug("News source %s returned %d item(s)", source.name, len(outcome))
            items.extend(outcome)

        inserted = self.store.upsert_many(items)
        logger.info(
            "News poll: %d fetched, %d new, %d total stored",
            len(items),
            inserted,
            self.store.count(),
        )
        return inserted

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        """Poll until `stop_event` is set (or forever)."""
        stop_event = stop_event or asyncio.Event()

        async with aiohttp.ClientSession() as session:
            while not stop_event.is_set():
                try:
                    await self.poll_once(session)
                except Exception:
                    logger.exception("News poll cycle failed; continuing")

                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self.interval_seconds
                    )
                except asyncio.TimeoutError:
                    continue
