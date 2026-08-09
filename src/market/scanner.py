"""Universe scanner: discovers and ranks candidate perpetuals on the exchange."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from src.execution.exchange import DeltaExchangeClient, ExchangeError

logger = logging.getLogger(__name__)


@dataclass
class SymbolInfo:
    symbol: str
    product_id: Optional[int]
    underlying: str = ""
    quote: str = "USD"
    tick_size: Optional[float] = None
    min_size: Optional[float] = None
    volume_24h: float = 0.0
    open_interest: float = 0.0
    mark_price: float = 0.0
    turnover_usd_24h: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "product_id": self.product_id,
            "underlying": self.underlying,
            "quote": self.quote,
            "volume_24h": self.volume_24h,
            "turnover_usd_24h": self.turnover_usd_24h,
            "open_interest": self.open_interest,
            "mark_price": self.mark_price,
            "tick_size": self.tick_size,
            "min_size": self.min_size,
        }


class SymbolScanner:
    """Discovers active perpetual contracts and ranks them by liquidity."""

    def __init__(
        self,
        exchange: DeltaExchangeClient,
        top_n: int = 50,
        quote_currencies: tuple[str, ...] = ("USD", "USDT"),
        min_turnover_usd: float = 1_000_000.0,
        excluded_underlyings: tuple[str, ...] = (),
    ) -> None:
        self.exchange = exchange
        self.top_n = top_n
        self.quote_currencies = quote_currencies
        self.min_turnover_usd = min_turnover_usd
        self.excluded_underlyings = set(excluded_underlyings)

    async def scan(self) -> list[SymbolInfo]:
        """Return top-N most liquid perpetuals sorted by 24h turnover (USD)."""
        try:
            products = await self.exchange.get_products()
        except ExchangeError as exc:
            logger.error("Scanner failed to fetch products: %s", exc)
            return []

        candidates: list[SymbolInfo] = []
        for p in products:
            if p.get("contract_type") != "perpetual_futures":
                continue
            symbol = p.get("symbol", "")
            if not symbol:
                continue

            quoting = (p.get("quoting_asset") or {}).get("symbol", "")
            underlying = (p.get("underlying_asset") or {}).get("symbol", "")
            if quoting and self.quote_currencies and quoting not in self.quote_currencies:
                continue
            if underlying in self.excluded_underlyings:
                continue

            candidates.append(
                SymbolInfo(
                    symbol=symbol,
                    product_id=p.get("id"),
                    underlying=underlying,
                    quote=quoting or "USD",
                    tick_size=self._to_float(p.get("tick_size")),
                    min_size=self._to_float(p.get("contract_value") or p.get("min_size")),
                    raw={k: p.get(k) for k in ("state", "trading_status", "settlement_time")},
                )
            )

        if not candidates:
            logger.warning("Scanner found no perpetual candidates from products endpoint")
            return []

        enriched = await self._enrich_with_tickers(candidates)

        with_turnover = [s for s in enriched if s.turnover_usd_24h >= self.min_turnover_usd]
        if with_turnover:
            enriched = with_turnover
        else:
            # Ticker data missing or testnet has low turnover — keep all perpetuals
            logger.warning(
                "No symbols met turnover floor $%.0f — using all %d perpetuals",
                self.min_turnover_usd,
                len(enriched),
            )

        enriched.sort(key=lambda s: (s.turnover_usd_24h, s.volume_24h), reverse=True)
        top = enriched[: self.top_n]
        logger.info(
            "Scanner selected %d/%d symbols (min turnover $%.0f)",
            len(top),
            len(candidates),
            self.min_turnover_usd,
        )
        return top

    async def _enrich_with_tickers(self, syms: list[SymbolInfo]) -> list[SymbolInfo]:
        """Fetch 24h volumes/prices for each candidate. Uses bulk tickers if available."""
        try:
            bulk = await self.exchange._request("GET", "/v2/tickers")
            if isinstance(bulk, list):
                by_symbol = {t.get("symbol"): t for t in bulk if isinstance(t, dict)}
                for s in syms:
                    tk = by_symbol.get(s.symbol)
                    if tk:
                        self._apply_ticker(s, tk)
                return syms
        except Exception as exc:
            logger.debug("Bulk tickers unavailable, falling back to per-symbol: %s", exc)

        sem = asyncio.Semaphore(8)

        async def _fill(s: SymbolInfo) -> None:
            async with sem:
                try:
                    tk = await self.exchange.get_ticker(s.symbol)
                    self._apply_ticker(s, tk)
                except Exception as exc:
                    logger.debug("Ticker failed for %s: %s", s.symbol, exc)

        await asyncio.gather(*[_fill(s) for s in syms], return_exceptions=True)
        return syms

    @staticmethod
    def _apply_ticker(sym: SymbolInfo, ticker: dict[str, Any]) -> None:
        sym.volume_24h = SymbolScanner._to_float(ticker.get("volume") or ticker.get("volume_24h"), 0.0)
        sym.mark_price = SymbolScanner._to_float(
            ticker.get("mark_price") or ticker.get("close") or ticker.get("spot_price"),
            0.0,
        )
        sym.open_interest = SymbolScanner._to_float(ticker.get("open_interest"), 0.0)
        turnover = ticker.get("turnover_usd") or ticker.get("turnover")
        if turnover is not None:
            sym.turnover_usd_24h = SymbolScanner._to_float(turnover, 0.0)
        else:
            sym.turnover_usd_24h = sym.volume_24h * sym.mark_price

    @staticmethod
    def _to_float(v: Any, default: float = 0.0) -> float:
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default
