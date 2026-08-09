"""Backtest runner script."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime

from src.core.engine import TradingEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def run_backtest(symbol: str, start: str, end: str) -> None:
    engine = TradingEngine()
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)

    logger.info("Fetching data for %s (%s to %s)", symbol, start, end)
    df = await engine.data_manager.get_dataframe(symbol, "15m", limit=500)

    if df.empty:
        logger.error("No data available for backtest")
        return

    logger.info("Loaded %d candles", len(df))
    trades = 0
    for i in range(50, len(df)):
        subset = df.iloc[: i + 1]
        market_state = engine.market_builder.build(symbol, subset)
        signal = engine.strategy_selector.select_signal(market_state)
        if signal:
            trades += 1
            logger.info(
                "Bar %d: %s %s (conf=%.2f)",
                i,
                signal.direction.value,
                signal.strategy_name,
                signal.confidence,
            )

    logger.info("Backtest complete: %d signals generated", trades)
    await engine.exchange.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run backtest")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-06-30")
    args = parser.parse_args()
    asyncio.run(run_backtest(args.symbol, args.start, args.end))
