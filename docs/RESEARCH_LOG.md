# RESEARCH LOG

Append-only history of hypotheses tested. **`KNOWLEDGE.md` is the answer; this is the
journey.** Read this only when you need to know *why* a fact in KNOWLEDGE is what it is, or
to check whether something has already been tried.

## Rules

1. **Append only.** Never edit a past entry. If an entry turns out to be wrong, add a new one
   that supersedes it and update the corresponding `K-xx` fact in `KNOWLEDGE.md`.
2. **One entry per hypothesis**, written *before* looking at the result where possible.
3. Every entry states which `K-xx` facts it **used** and which it **changed**.
4. **Record failures in full.** A failure that isn't written down gets re-tested — that is
   exactly the loop this file exists to prevent.
5. Every tested configuration goes into `data/trials.json` via `TrialsRegistry`, because the
   deflated-Sharpe bar depends on an honest trial count (K-60).

## Entry template

```
### R-NN — <hypothesis in one line>
Date · Status: PASS / FAIL / INCONCLUSIVE / SUPERSEDED
Uses: K-xx, K-yy    Changes: K-zz
Method:   what was run, on what data, with what costs
Result:   the numbers
Verdict:  what it means, and what it rules in or out
```

---

## R-01 — Do the 7 built-in indicator strategies have an edge on crypto?
2026-08-10 · **FAIL** · Changes: K-40

**Method** 15 configs × 6 symbols × 730 days of real Delta production data, taker and maker
costs, walk-forward, both halves.
**Result** 0/15 significant (best t=1.45). 0/15 positive in both years. Gross edge 1.5–7.8
bps/trade against a ~10.3 bps break-even. 26/49 pairs profitable before costs, **0/49 after**.
**Verdict** The apparent winners were long-beta: they earned everything in the bull year and
gave it back in the bear. Not alpha. This framed everything after — market-neutral structures
became the priority.

## R-02 — Does maker execution rescue them?
2026-08-10 · **FAIL** · Uses: K-20

**Method** Post-only limit at the signal bar's close, filled only if the next bar trades
through; non-fills counted as missed trades (the honest version — booking the maker fee while
assuming every fill lands hands the strategy free participation).
**Result** Fees 11.80 → 8.26 bps at a ~98% fill rate. BTCUSD ema_crossover −169.7% → −105.4%.
**Verdict** A real improvement, nowhere near enough. Break-even needs ~10.3 bps of gross edge;
observed is 1.5–7.8.

## R-03 — A 4h configuration looked profitable
2026-08-10 · **SUPERSEDED — false positive** · Changes: K-72

**Method** Swept timeframe × stop/target on **BTC/ETH/SOL only**, reported the best.
**Result** 4h supertrend 2/6 showed +41.6% net, edge/cost 5.55. On six symbols it pools to
**−58.4%** (t=−0.53). All profit in H1, none in H2, on all three majors.
**Verdict** Four apparent winners out of thirty in-sample configs is what multiple testing
looks like. Directly motivated the trials registry and deflated Sharpe.

## R-04 — Hour-of-day seasonality
2026-08-10 · **FAIL** · Changes: K-50

**Result** Best hour +3.22 bps (t=1.21) against 8.26 bps of cost; worst −5.93 bps. No \|t\|
above 2.2 across 24 hours, where ~1 is expected by chance.

## R-05 — Cross-sectional momentum / reversal on crypto
2026-08-10 · **FAIL** · Changes: K-41, K-42, **K-70**

**Method** Long/short, market-neutral, 4–6 symbols, formation × holding sweep.
**Result** Momentum best t=1.83, +49.3% over 2y, max DD 10.5%, **positive in both years** —
the only strategy that ever achieved that. But DSR **0.19**, and Q1 alone carries +41.2 of the
+49.3 points.
**Correction recorded** An exploratory script first reported momentum as reliably *negative*
(t=−2.95). It used `sort_values(ascending=reverse)`, so its "momentum" branch sorted
descending and went long the lowest-ranked names. **Both labels were inverted**, and a round
of planning was built on the wrong sign. See K-70.
**Verdict** Closest anything has come. Rejected on trial-count-adjusted significance, and the
rejection looks right given the quarterly concentration.

## R-06 — Pair-spread mean reversion
2026-08-10 · **FAIL** · Changes: K-51

**Result** 54 configs across 6 pairs, 0 with t>2. Best ADA/XRP: +25% annualised, 65% win rate,
t=0.82 — high variance, small edge.

## R-07 — Price action with structural stops
2026-08-10 · **FAIL** · Changes: K-43

**Method** Rejection candle (pin bar / engulfing) at a confirmed swing level, stop placed at
the structure. Required first making the backtester honour `signal.stop_loss` — it previously
imposed a uniform 2×ATR trade on every strategy while *live* honoured the strategy's own stop,
so a structural-stop hypothesis was untestable. Swing pivots shifted by their confirmation lag
to avoid lookahead.
**Result** All 18 configs deeply negative: **−41 to −55 bps/trade**, t from −4 to −24, against
the indicator strategies' −19.
**Verdict** The hypothesis was that a structural stop justifies itself better than an
arbitrary ATR multiple. The opposite: structural stops here are *tight*, often below the 0.57%
sizable floor (K-33), and get hit far more often than 2–3× reward:risk compensates for. Win
rates 20–36% against the 25–40% needed.

## R-08 — Port to NSE for cross-sectional breadth
2026-08-17 · **INFRASTRUCTURE** · Changes: K-21, K-22, K-23, K-80

**Method** Delta's 4-name cross-section (K-16) was the binding constraint on the only
promising family. NSE offers 196 names after coverage filtering — 49× wider. Universe from
Dhan's public instrument master (no credentials), daily OHLCV from Yahoo, split/dividend
adjusted.
**Result** 196 × 1,085 sessions, 2021-11 → 2026-08. NSE cost model verified to the paisa
against Dhan's calculator.
**Verdict** Two properties the crypto cost model could not express and that change which
strategies are viable: fees are **not scale-invariant** (₹20/order cap binds above ₹66,667),
and legs are **asymmetric** (STT sell-side, stamp duty buy-side).

## R-09 — NSE overnight-gap intraday reversal
2026-08-17 · **FAIL on cost** · Changes: K-44, K-46, **K-71**

**Method** Rank on the overnight gap, long losers / short winners, hold open-to-close, MIS.
**First result — contaminated** Sharpe 6.48, t=13.41, +173.8% net. Not plausible in liquid
equities, and not real: signal `O_t/C_{t-1}` and return `C_t/O_t` **share the price `O_t`**,
so noise in that print makes a name look like a bigger loser *and* gives it a higher return.
The tell was already visible — the close-to-close signal, sharing no price with the return,
showed t=0.45 while only the gap variant exploded.
**Clean result** Removing the shared price: 55.3 → **18.0 bps/day, t=6.87**. A real signal at
about a third the apparent size. At 20 legs: gross 8.96 bps/day (t=6.82), fees 8.77 bps/day,
**break-even spread 0.23 bps** against real spreads of several bps.
**Verdict** Statistically strong, economically absent — fees consume 98%. Turnover cannot be
cut: the extreme gappers are different names daily, so the rebalance band never binds
(verified identical across 0.0/0.5/1.0).
**But** see K-46: because fees fall with order size (K-22), the same signal becomes viable
around ₹10–30 lakh. First time "no edge" resolved into "edge above ₹X".

## R-10 — What strategy actually fits ₹10,000?
2026-08-17 · **FAIL** · Changes: K-25, K-31, K-45, **K-73**

**Method** Asked the sizing question directly. At ₹10,000 the fee *rate* is fixed (K-22), so
turnover is the only lever. Measured fee drag by rebalance frequency, then tested the
structure that fits: long-only, monthly, 10–15 names.
**Result** Fee drag: daily 47.8%/yr, weekly 6.0%, **monthly 0.7%**, quarterly 0.2% — so
monthly-or-slower is essentially free at this size, *inverting* R-09's capital threshold,
which binds only on high-turnover strategies. Long-only monthly momentum then showed **56.77%
CAGR** against a 30.37% benchmark.
**Then the survivorship check** Restricting to names already in the top 100 by turnover at the
start of the sample — a point-in-time proxy — excess return collapses:

```
today's F&O list (196)      +17.78pp excess   t = 1.46   H2 excess −4.72pp
already liquid in Nov 2021  + 1.70pp excess   t = 0.38   H2 excess −2.27pp
```

**90% of the apparent alpha was the universe.** DSR on excess returns: 0.023.
**Verdict** The bias is specific and flatters momentum above all (K-73). Combined with K-12,
this leaves the structural picture in K-12/K-44/K-45: two of three available structures are
closed at ₹10,000, and the third cannot be measured honestly until the universe is
point-in-time. Makes **K-90 the highest-value open item**, ahead of any further strategy
search.

---

## Next candidates, ranked

| Priority | Item | Refs |
|---|---|---|
| 1 | Obtain a **survivorship-free NSE universe** | K-90 |
| 2 | Test whether R-09's signal survives at **weekly** turnover — testable today, no new data | K-93 |
| 3 | One live ₹500 round trip to settle the per-order minimum fee | K-92 |
| 4 | Research agent (propose → backtest → pass mark → registry). **Deliberately deferred**: pointing an automated search at survivorship-biased data generates confident nonsense faster. | K-90 |
