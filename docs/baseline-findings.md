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

---

# Round 2 — cross-sectional strategies

**Date:** 2026-08-10. Same data, same cost model, maker entries throughout.

## A correction to Round 2's exploratory phase

An exploratory script reported that cross-sectional **momentum was reliably
negative** (t = −2.95) and that reversal was therefore the signal. **That was
backwards.** The script used `sort_values(ascending=reverse)`, so its "momentum"
branch sorted *descending* and then took `index[-k:]` — the **lowest**-ranked
names — and went long them. Both labels were inverted.

Corrected, with a hand-rolled unambiguous check (168h formation / 72h hold, six
symbols, gross):

```
MOMENTUM (long winners, short losers):  n=240  +76.8%  +32.0 bps/reb  t = +2.10
REVERSAL (long losers, short winners):  n=240  -76.8%  -32.0 bps/reb  t = -2.10
```

Momentum is the positive side. This also agrees with the published crypto
cross-sectional momentum literature — the earlier result contradicting it should
have been treated as a red flag rather than a finding.

`src/strategies/xs_momentum.py` now uses an explicit `Tilt` enum
(`LONG_WINNERS` / `LONG_LOSERS`) instead of a bare sign, and
`test_long_winners_actually_buys_the_winners` pins the semantics.

## Result: cross-sectional momentum — FAILS the pass mark

Universe: ADAUSD, XRPUSD, DOGEUSD, ETHUSD (the four with workable contract
granularity at ₹10,000). 1h bars, 730 days, market-neutral, maker entries.

Best configuration — 168h formation, 168h hold:

| Metric | Value |
|---|---|
| Gross / costs / **net** | 58.6% / 9.3% / **+49.3%** over 2 years |
| Rebalances | 102 |
| Mean | 48.3 bps per rebalance |
| Sharpe (annualised) | 1.31 |
| t-statistic | 1.83 |
| Max drawdown | 10.5% |
| Win rate | 51% |

**Pre-committed pass mark:**

| Criterion | Result | |
|---|---|---|
| Deflated Sharpe > 0.95 | **0.19** (n_trials = 144) | **FAIL** |
| Positive in both years | Y1 +54.6%, Y2 +4.6% | PASS |
| ≥ 100 observations | 102 | PASS |

**Verdict: FAIL.**

This is the closest anything has come — the first and only strategy to be
profitable in both the bull and the bear year, and with a 10.5% drawdown rather
than the 30%+ of the reversal variants. But the quarterly breakdown shows why
the deflated Sharpe is right to reject it:

```
Q1 (H1 2025): n=24  +41.24%   Sharpe +3.38
Q2:           n=24   +6.30%   Sharpe +0.71
Q3:           n=24   -2.14%   Sharpe -0.34
Q4 (H1 2026): n=24   +3.84%   Sharpe +0.60
```

**Q1 carries +41.2 of the +49.3 points.** The remaining eighteen months produce
+8%. That is one good half-year, not a persistent edge, and with only 102
observations against 144 recorded trials it cannot be separated from luck.

## Also recorded as negative

- **Cross-sectional reversal**: 36 configurations, 0 with t > 2, best t = 0.49,
  best config +36% in the bull year and −22.5% in the bear.
- **Volatility-normalised ranking** and **inverse-volatility leg weighting**:
  both were expected to raise power and both made results *worse* on this
  universe. They remain in the code but default to off.
- **Hour-of-day seasonality**: 24 hours tested, best +3.22 bps against an 8.26
  bps cost, no |t| above 2.2. Dead.
- **Pair spread mean-reversion**: 54 configurations across six pairs, 0 with
  t > 2, best t = 0.82 (ADA/XRP, +25% annualised, 65% win rate).

## Result: price action (swing rejection, structural stops) — FAILS, badly

Rejection candle (pin bar or engulfing) at a confirmed swing level, with the
stop placed just beyond the level rather than at a fixed ATR multiple. Run with
`QuickBacktester(honor_signal_levels=True)` so the strategy's own stop is
actually the one tested — under the default the backtester overrides every
strategy with a uniform 2×ATR stop, which would make a structural-stop
hypothesis untestable.

18 configurations across 1h and 4h, four symbols, maker entries:

| tf | proximity | RR | trades | bps/trade | net | win% | t |
|---|---|---|---|---|---|---|---|
| 4h | 0.3 | 3.0 | 89 | −41.6 | −37.1% | 22.5 | −4.03 |
| 4h | 0.3 | 2.0 | 89 | −42.8 | −38.1% | 29.2 | −5.04 |
| 1h | 0.3 | 1.5 | 865 | −41.3 | −357.6% | 35.7 | −16.86 |
| 1h | 1.0 | 1.5 | 2964 | −44.8 | −1327.6% | 35.4 | −24.36 |

**Every configuration is deeply negative**, at −41 to −55 bps per trade against
the indicator strategies' −19. t-statistics run from −4 to −24; the *best* case
is significantly bad.

The hypothesis was that a structural stop would be better justified than an
arbitrary ATR multiple and so raise edge per trade. The opposite happened:
structural stops at these timeframes are *tight*, frequently below the 0.57%
sizable floor and clamped up to it, and they get hit far more often than the
2–3× reward:risk compensates for. Win rates land at 20–36% when 25–40% is
needed just to break even before costs.

Recorded so it is not retried: price action was tested properly, with lookahead
control on the swing columns and with the backtester honouring its stops, and it
is materially worse than what it was meant to replace.

## The venue constraint, measured

Delta India lists **8 liquid perpetuals**, and only BTC/ETH/SOL/XRP have real
depth — BMTUSD shows $48M turnover against $236k open interest, which is churn,
not liquidity. Cross-sectional strategies are the most promising family found,
and they are precisely the family most starved by a four-name cross-section. The
published results this approach is based on use hundreds of coins.

## Reproducing

```bash
python main.py fetch-history --symbol BTCUSD --timeframe 4h --days 730
python -m pytest tests/test_quick_backtest.py tests/test_costs.py
```

`QuickBacktester(cost_model=CostModel(), maker_entry=True)` is the configuration
used throughout. `CostModel.zero()` isolates the gross signal.
