"""News source clients.

Every client satisfies the `NewsSource` protocol and obeys one hard rule:
**fetch() never raises**. A dead feed, a Cloudflare challenge, an HTML error
page where JSON was expected, or a malformed date must all degrade to "no
news" — never to an exception that reaches the trading engine.

That rule is not defensive padding. Probing on 2026-08-10:

    CryptoPanic  /api/v1/posts/          -> 403, text/html   (Cloudflare)
    CryptoPanic  /api/developer/v2/posts -> 404, text/html
    CoinGecko    /api/v3/news            -> 401, JSON        (PRO only)
    RSS (CoinDesk, Cointelegraph, Decrypt) -> 200, application/xml

Both keyed APIs answer with HTML rather than JSON when auth fails, so a naive
`await resp.json()` raises rather than returning an error document. RSS needs
no key and is therefore the default source; the keyed clients are wired up and
ready for when credentials exist.
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional, Protocol, runtime_checkable

import aiohttp

from src.news.mapping import assets_for_text
from src.news.models import NewsItem
from src.news.sanitize import sanitize_headline

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 20
USER_AGENT = "crypto-trader/1.0 (+news-poller)"

DEFAULT_RSS_FEEDS: tuple[tuple[str, str], ...] = (
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("cointelegraph", "https://cointelegraph.com/rss"),
    ("decrypt", "https://decrypt.co/feed"),
)


@runtime_checkable
class NewsSource(Protocol):
    name: str

    async def fetch(self, session: aiohttp.ClientSession) -> list[NewsItem]:
        """Return recent items. Must never raise."""
        ...


def _to_naive_utc(dt: datetime) -> datetime:
    """Normalise to naive UTC — the rest of the system stores naive UTC."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_rss_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return _to_naive_utc(parsedate_to_datetime(value))
    except (TypeError, ValueError):
        pass
    # Some feeds emit ISO-8601 despite RSS convention.
    try:
        return _to_naive_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


class RSSNewsClient:
    """Keyless RSS aggregator across several crypto outlets.

    Asset tagging is keyword-based because RSS carries no structured tags.
    """

    name = "rss"

    def __init__(
        self,
        feeds: tuple[tuple[str, str], ...] = DEFAULT_RSS_FEEDS,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.feeds = feeds
        self.timeout_seconds = timeout_seconds

    async def fetch(self, session: aiohttp.ClientSession) -> list[NewsItem]:
        results = await asyncio.gather(
            *(self._fetch_one(session, src, url) for src, url in self.feeds),
            return_exceptions=True,
        )
        items: list[NewsItem] = []
        for outcome in results:
            if isinstance(outcome, BaseException):
                logger.warning("RSS feed task failed: %s", outcome)
                continue
            items.extend(outcome)
        return items

    async def _fetch_one(
        self, session: aiohttp.ClientSession, source: str, url: str
    ) -> list[NewsItem]:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                headers={"User-Agent": USER_AGENT},
            ) as resp:
                if resp.status != 200:
                    logger.warning("RSS %s returned HTTP %d", source, resp.status)
                    return []
                body = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("RSS %s unreachable: %s", source, exc)
            return []

        return self._parse(source, body)

    @staticmethod
    def _parse(source: str, body: str) -> list[NewsItem]:
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            logger.warning("RSS %s returned unparseable XML: %s", source, exc)
            return []

        items: list[NewsItem] = []
        for node in root.iter("item"):
            title_raw = (node.findtext("title") or "").strip()
            title = sanitize_headline(title_raw)
            if not title:
                continue

            published = _parse_rss_date(node.findtext("pubDate")) or _parse_rss_date(
                node.findtext("{http://purl.org/dc/elements/1.1/}date")
            )
            if published is None:
                # No timestamp means no usable label and no leakage control.
                continue

            assets = assets_for_text(title)
            if not assets:
                continue

            items.append(
                NewsItem.create(
                    source=source,
                    title=title,
                    published_at=published,
                    url=(node.findtext("link") or "").strip() or None,
                    assets=assets,
                )
            )
        return items


class CryptoPanicClient:
    """CryptoPanic posts API. Requires an auth token.

    Note the whole universe fits in one request via the comma-separated
    `currencies` parameter — never poll per-symbol.
    """

    name = "cryptopanic"
    BASE_URL = "https://cryptopanic.com/api/v1/posts/"

    def __init__(
        self,
        auth_token: str,
        *,
        currencies: Optional[list[str]] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.auth_token = auth_token
        self.currencies = currencies or []
        self.timeout_seconds = timeout_seconds

    async def fetch(self, session: aiohttp.ClientSession) -> list[NewsItem]:
        if not self.auth_token:
            logger.debug("CryptoPanic skipped — no auth token configured")
            return []

        params: dict[str, Any] = {"auth_token": self.auth_token, "public": "true"}
        if self.currencies:
            params["currencies"] = ",".join(self.currencies)

        payload = await _get_json(
            session, self.BASE_URL, params=params, timeout=self.timeout_seconds, label=self.name
        )
        if not payload:
            return []

        items: list[NewsItem] = []
        for post in payload.get("results", []) or []:
            title = sanitize_headline(str(post.get("title", "")))
            if not title:
                continue
            published = _parse_rss_date(post.get("published_at"))
            if published is None:
                continue

            assets = [
                str(c.get("code", "")).upper()
                for c in (post.get("currencies") or [])
                if c.get("code")
            ]
            votes = post.get("votes") or {}
            items.append(
                NewsItem.create(
                    source=self.name,
                    title=title,
                    published_at=published,
                    url=post.get("url"),
                    assets=assets or assets_for_text(title),
                    votes_important=int(votes.get("important", 0) or 0),
                    source_sentiment=post.get("kind"),
                )
            )
        return items


class CoinGeckoNewsClient:
    """CoinGecko news endpoint. PRO-tier only as of 2026-08."""

    name = "coingecko"
    BASE_URL = "https://api.coingecko.com/api/v3/news"

    def __init__(
        self, api_key: str = "", *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def fetch(self, session: aiohttp.ClientSession) -> list[NewsItem]:
        if not self.api_key:
            logger.debug("CoinGecko skipped — no API key configured")
            return []

        payload = await _get_json(
            session,
            self.BASE_URL,
            params={},
            timeout=self.timeout_seconds,
            label=self.name,
            headers={"x-cg-pro-api-key": self.api_key},
        )
        if not payload:
            return []

        items: list[NewsItem] = []
        for entry in payload.get("data", []) or []:
            title = sanitize_headline(str(entry.get("title", "")))
            if not title:
                continue
            published = _parse_rss_date(entry.get("updated_at") or entry.get("published_at"))
            if published is None:
                continue
            items.append(
                NewsItem.create(
                    source=self.name,
                    title=title,
                    published_at=published,
                    url=entry.get("url"),
                    assets=assets_for_text(title),
                )
            )
        return items


async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, Any],
    timeout: int,
    label: str,
    headers: Optional[dict[str, str]] = None,
) -> Optional[dict[str, Any]]:
    """GET a JSON document, returning None on any failure.

    Checks Content-Type before parsing: both keyed providers answer auth
    failures with an HTML page, so resp.json() would raise rather than yield
    an error document.
    """
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    request_headers.update(headers or {})

    try:
        async with session.get(
            url,
            params=params,
            headers=request_headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if resp.status != 200:
                logger.warning(
                    "%s returned HTTP %d (%s) — treating as no news",
                    label,
                    resp.status,
                    content_type or "unknown content type",
                )
                return None
            if "json" not in content_type.lower():
                logger.warning(
                    "%s returned %s instead of JSON — treating as no news",
                    label,
                    content_type or "an unknown content type",
                )
                return None
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning("%s fetch failed: %s", label, exc)
        return None
