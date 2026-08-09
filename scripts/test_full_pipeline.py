#!/usr/bin/env python3
"""Run the full trading pipeline once against the Delta Exchange demo.

Tests the entire chain: Data → Indicators → Regime → Strategy → Signal → Risk Check.
No orders are placed (observation mode only).

Usage:
    python scripts/test_full_pipeline.py
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pipeline_test")

from src.core.models import Regime
from src.data.indicators import IndicatorEngine
from src.execution.exchange import DeltaExchangeClient
from src.market.regime import RegimeDetector
from src.market.state import MarketStateBuilder
from src.risk.position_sizer import PositionSizer
from src.strategies.selector import StrategySelector


async def main():
    print("=" * 60)
    print("Full Pipeline Test — Delta Exchange Demo")
    print("=" * 60)

    base_url = "https://cdn-ind.testnet.deltaex.org"
    api_key = os.getenv("DELTA_API_KEY", "")
    api_secret = os.getenv("DELTA_API_SECRET", "")

    exchange = DeltaExchangeClient(
        api_key=api_key,
        api_secret=api_secret,
        base_url=base_url,
        testnet=True,
    )

    try:
        # 1. Fetch account balance
        print("\n[1] Fetching account balance...")
        balance = await exchange.get_balance()
        if isinstance(balance, list) and balance:
            equity = float(balance[0].get("balance", 0))
        elif isinstance(balance, dict):
            equity = float(balance.get("balance", balance.get("available_balance", 0)))
        else:
            equity = 100.0
        print(f"    Account equity: ${equity:.2f}")

        # 2. Fetch candles
        symbol = "BTCUSD"
        print(f"\n[2] Fetching 15m candles for {symbol}...")
        candles = await exchange.get_candles(symbol, "15m", limit=200)
        print(f"    Received {len(candles)} candles")
        if candles:
            latest = candles[-1]
            print(f"    Latest: {latest.timestamp} | O={latest.open} H={latest.high} L={latest.low} C={latest.close} V={latest.volume}")

        # 3. Compute indicators
        print(f"\n[3] Computing indicators...")
        engine = IndicatorEngine()
        df = engine.candles_to_df(candles)
        df = engine.compute_all(df)
        values = engine.get_latest_values(df)
        print(f"    Indicators computed: {len(values)} values")
        key_indicators = ["rsi_14", "ADX_14", "atr_14", "ema_50", "ema_200", "volume_ratio"]
        for k in key_indicators:
            if k in values:
                print(f"    {k}: {values[k]:.4f}")

        # 4. Detect market regime
        print(f"\n[4] Detecting market regime...")
        detector = RegimeDetector()
        regime, confidence = detector.detect(df)
        print(f"    Regime: {regime.value} (confidence: {confidence:.2%})")

        # 5. Build market state
        print(f"\n[5] Building market state...")
        builder = MarketStateBuilder(indicator_engine=engine, regime_detector=detector)
        
        # Fetch higher timeframe data
        print(f"    Fetching 1h candles...")
        candles_1h = await exchange.get_candles(symbol, "1h", limit=100)
        df_1h = engine.compute_all(engine.candles_to_df(candles_1h)) if candles_1h else None
        
        multi_tf = {"1h": df_1h} if df_1h is not None and not df_1h.empty else {}
        market_state = builder.build(symbol, df, multi_tf, candles)
        print(f"    Market State: price=${market_state.price:.2f}, regime={market_state.regime.value}")
        if market_state.support_levels:
            print(f"    Support levels: {[f'${s:.0f}' for s in market_state.support_levels]}")
        if market_state.resistance_levels:
            print(f"    Resistance levels: {[f'${r:.0f}' for r in market_state.resistance_levels]}")

        # 6. Run strategy selector
        print(f"\n[6] Running strategy selector...")
        import yaml
        config_path = Path(__file__).parent.parent / "config" / "strategies.yaml"
        with open(config_path) as f:
            strategy_config = yaml.safe_load(f)

        selector = StrategySelector(strategy_config)
        eligible = selector.get_eligible_strategies(regime)
        print(f"    Eligible strategies for {regime.value}: {[s.name for s in eligible]}")

        signal = selector.select_signal(market_state)
        if signal:
            print(f"\n    *** SIGNAL GENERATED ***")
            print(f"    Direction: {signal.direction.value.upper()}")
            print(f"    Strategy:  {signal.strategy_name}")
            print(f"    Confidence: {signal.confidence:.2%}")
            print(f"    Entry:     ${signal.entry_price:.2f}")
            print(f"    Stop Loss: ${signal.stop_loss:.2f}")
            print(f"    Take Profit: ${signal.take_profit:.2f}")

            # 7. Position sizing
            print(f"\n[7] Position sizing check...")
            risk_config = {
                "position_sizing": {
                    "method": "fixed_fractional",
                    "risk_per_trade_pct": 2.0,
                    "max_leverage": 5,
                    "min_order_size_usd": 10,
                },
            }
            sizer = PositionSizer(risk_config)
            size = sizer.calculate(signal, equity=equity)
            notional = size * signal.entry_price if signal.entry_price else 0
            print(f"    Size: {size:.6f} BTC")
            print(f"    Notional: ${notional:.2f}")
            print(f"    Risk: ${equity * 0.02:.2f} (2% of ${equity:.2f})")
            
            if size > 0:
                print(f"\n    Ready to trade! (No order placed — observation mode)")
            else:
                print(f"\n    Size too small for minimum order ($10)")
        else:
            print(f"\n    No signal generated — current conditions don't meet any strategy criteria")
            print(f"    This is normal! Strategies only fire when high-confidence setups appear.")

        # 8. Check existing positions
        print(f"\n[8] Checking existing positions...")
        positions = await exchange.get_positions("BTC")
        if positions:
            for pos in positions:
                print(f"    {pos.side.value.upper()} {pos.symbol}: size={pos.size}, entry=${pos.entry_price:.2f}")
        else:
            print(f"    No open positions")

    finally:
        await exchange.close()

    print("\n" + "=" * 60)
    print("Pipeline test complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
