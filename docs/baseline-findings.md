# Baseline findings — do the strategies have an edge?

**Date:** 2026-08-10
**Data:** Delta Exchange India production OHLCV, 2024-08-10 → 2026-08-10 (730 days)
**Symbols:** BTCUSD, ETHUSD, SOLUSD, XRPUSD, ADAUSD, DOGEUSD
**Costs:** taker 5.9 bps, maker 2.36 bps (0.05% / 0.02% + 18% GST), slippage, 8h funding

## Answer

**No.** Across 2 years spanning a full bull–bear cycle, no configuration of the
seven built-in strategies produces a statistically significant edge after
realistic costs.

| Measure | Result |
|---|---|
| Configs with net return > 0 | 5 / 15 |
| Configs with **t > 2.0** | **0 / 15** (best: 1.45) |
| Configs positive in **both** years | **0 / 15** |

The five "profitable" configurations made all of their money in the bull year
and gave it back in the bear year. That is directional beta, not alpha.

```
strategy       s/t      n   bps/trade    net%      t    Y1(bull)   Y2(bear)
ema_crossover  3/9    581       53.28  309.56   1.45     374.57     -65.00
ema_crossover  2/6    726       32.61  236.77   1.15     242.06      -5.28
supertrend     3/9    392       47.07  184.50   0.91     327.19    -142.70
supertrend     2/6    486       23.27  113.07   0.63     237.65    -124.58
macd           2/6   1175      -10.58 -124.36  -0.53     215.57    -339.93
bollinger      2/6    337      -76.25 -256.95  -2.50    -201.88     -55.07
```

## Why: the edge per trade is smaller than the cost per trade

On BTCUSD 15m over one year, with taker execution:

| Strategy | Trades | Gross edge/trade | Cost/trade | Edge/cost |
|---|---|---|---|---|
| donchian | 53 | 7.81 bps | 19.28 bps | 0.41 |
| supertrend | 934 | 4.71 bps | 20.04 bps | 0.23 |
| ema_crossover | 1073 | 3.03 bps | 18.85 bps | 0.16 |
| macd | 1626 | 2.88 bps | 20.02 bps | 0.14 |
| bollinger | 460 | 1.53 bps | 19.28 bps | 0.08 |
| rsi_reversion | 88 | −9.96 bps | 18.95 bps | −0.53 |

Break-even requires roughly **10.3 bps** of gross edge per trade (8.26 bps
maker/taker fees + ~2 bps exit slippage). Observed gross edge is 1.5–7.8 bps.
The strategies need 2.5–12× more edge per trade merely to break even.

Over the same year, 26/49 symbol-strategy pairs were profitable **before** costs
and **0/49 after**. Median cost drag: 211 percentage points.

## What was tried

1. **Maker entries.** Post-only limit resting at the signal bar's close, filled
   only if the next bar trades back through it; non-fills counted as missed
   trades. Cuts round-trip fees from 11.80 → 8.26 bps at a ~98% fill rate.
   BTCUSD ema_crossover improved from −169.7% to −105.4% — a real improvement,
   nowhere near enough.
2. **Slower timeframes.** 4h cuts trade count ~13× versus 15m. Still negative.
3. **Wider targets.** ATR stop/target of 2/3, 2/6, 3/9. Wider targets raise edge
   per trade but lower the win rate; no combination clears costs robustly.

### A trap worth recording

A sweep over timeframe × stop/target on **BTC/ETH/SOL only** appeared to find
winners: 4h supertrend 2/6 at +41.6% net (edge/cost 5.55) and 4h ema_crossover
3/9 at +40.8%. Both evaporated under scrutiny:

- Extending to 6 symbols: supertrend 2/6 pools to **−58.4%** over 272 trades
  (t = −0.53); ema_crossover 3/9 to **−19.2%** over 275 trades (t = −0.18).
- Splitting the year: all profit in H1, none in H2, on all three majors.

Four apparent winners out of thirty in-sample configurations is what multiple
testing looks like. Any future search needs walk-forward validation and a
multiple-testing correction, not a leaderboard.

## Sample caveats

- The venue only has ~2 years of history for these perps; 3 years returns
  nothing. Y1 was a bull market (BTC 60,504 → 118,308), Y2 a severe bear
  (118,308 → 64,953).
- Buy-and-hold over the most recent year: BTC −45%, ETH −54%, SOL −57%,
  XRP −68%, ADA −75%, DOGE −70%. Every strategy beat buy-and-hold; that is a
  low bar and not a reason to trade one.

## Incidental findings

- **`volume_breakout` never fires.** Zero trades across 7 symbols over a full
  year. Its preferred regimes do occur (trending 15,425 bars; quiet 3,506), so
  the entry conditions — Donchian break *and* 2× volume *and* narrow range —
  simply never co-occur.
- **ONDOUSD is 50.8% flat bars at 15m** (13.9% at 1h): half its bars have no
  price movement. It is nonetheless eligible for the traded top-10 because
  `universe.min_turnover_usd` is `0`.
- **ADAUSD is 6.9% flat bars** at 15m over 2 years.

## Implications

1. The LLM veto layer cannot be evaluated against this baseline. A veto-only
   overlay can only remove trades; on a negative-expectancy strategy set the
   optimal policy degenerates to vetoing everything. A baseline with positive
   expectancy is a prerequisite, not a nicety.
2. Effort is better spent finding a genuine edge than gating a non-existent one.
3. Whatever is tried next must be validated walk-forward with a multiple-testing
   correction, or it will keep producing results like the 4h "winner" above.

## Reproducing

```bash
python main.py fetch-history --symbol BTCUSD --timeframe 4h --days 730
python -m pytest tests/test_quick_backtest.py tests/test_costs.py
```

`QuickBacktester(cost_model=CostModel(), maker_entry=True)` is the configuration
used throughout. `CostModel.zero()` isolates the gross signal.
