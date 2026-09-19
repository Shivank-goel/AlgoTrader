"""Read-only market-data check: python -m src.data.fyers_check --symbol NSE:SBIN-EQ."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.execution.fyers import FyersClient, FyersGatewayError


async def check(symbol: str) -> None:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)
    client = FyersClient.from_env()
    try:
        quotes = await client.get_quotes([symbol])
        row = quotes["d"][0]
        print(f"Quote received: {row['n']} LTP={row['v'].get('lp')} (snapshot; may be last-session data)")
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        history = await client.get_history(symbol, today - timedelta(days=10), today - timedelta(days=1))
        print(f"Historical daily candles received: {len(history['candles'])}")
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(check(args.symbol))
    except (FyersGatewayError, ValueError) as exc:
        print(str(exc))
        raise SystemExit(1) from None
