"""Resolve exact symbols from FYERS' current NSE master, never guessed IDs."""

from __future__ import annotations

from urllib.parse import urlsplit

import aiohttp

from src.fyers.models import Instrument


async def resolve_instruments(url: str, symbols: list[str]) -> dict[str, Instrument]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "public.fyers.in" or parsed.path != "/sym_details/NSE_CM_sym_master.json" or parsed.query or parsed.fragment:
        raise ValueError("Use the official FYERS NSE cash master")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async with session.get(url, allow_redirects=False) as response:
            response.raise_for_status()
            master = await response.json(content_type=None)
    return parse_instruments(master, symbols)


def parse_instruments(master: dict, symbols: list[str]) -> dict[str, Instrument]:
    result = {}
    for symbol in symbols:
        if not symbol.startswith("NSE:") or not symbol.endswith("-EQ"):
            raise ValueError("Only NSE cash equity is supported")
        if symbol not in master:
            raise ValueError(f"Symbol missing from current master: {symbol}")
        row = master[symbol]
        if row.get("tradeStatus", 1) != 1:
            raise ValueError(f"Instrument is not enabled for trading: {symbol}")
        result[symbol] = Instrument(symbol=symbol, isin=row["isin"],
                                    lot_size=row["minLotSize"], tick_size=row["tickSize"],
                                    trading_session=row.get("tradingSession", ""),
                                    master_updated=row.get("lastUpdate", ""))
    return result
