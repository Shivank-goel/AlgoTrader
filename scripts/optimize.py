"""Walk-forward optimization script."""

from __future__ import annotations

import argparse
import asyncio
import logging

from src.core.engine import TradingEngine
from src.learning.optimizer import WalkForwardOptimizer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PARAM_GRIDS = {
    "ema_crossover": {
        "fast_period": [9, 12, 20],
        "slow_period": [21, 26, 50],
    },
    "rsi_reversion": {
        "rsi_period": [7, 14],
        "rsi_lower": [25, 30],
        "rsi_upper": [70, 75],
    },
}


def simple_backtest(strategy_name: str, df, params: dict) -> float:
    raise NotImplementedError("Placeholder scores are not research evidence; use src.research.runner")


async def run_optimization(symbol: str, is_window: int, oos_window: int) -> None:
    raise RuntimeError("Legacy unregistered optimizer disabled; use a frozen experiment and real backtest")
    engine = TradingEngine()
    optimizer = WalkForwardOptimizer(
        engine.strategy_config,
        is_window_days=is_window,
        oos_window_days=oos_window,
    )

    df = await engine.data_manager.get_dataframe(symbol, "15m", limit=500)
    if df.empty:
        logger.error("No data for optimization")
        return

    for strategy_name, grid in PARAM_GRIDS.items():
        best = optimizer.optimize(strategy_name, df, grid, simple_backtest)
        engine.param_adapter.update_from_optimizer(strategy_name, best)
        logger.info("%s optimized: %s", strategy_name, best)

    await engine.exchange.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walk-forward optimization")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--is-window", type=int, default=90)
    parser.add_argument("--oos-window", type=int, default=30)
    args = parser.parse_args()
    asyncio.run(run_optimization(args.symbol, args.is_window, args.oos_window))
