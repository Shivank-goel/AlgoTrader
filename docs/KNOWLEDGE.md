# KNOWLEDGE — what we actually know

**Read this before proposing or testing anything.** It is the deduplicated, current-state
record of established facts. `RESEARCH_LOG.md` is the append-only history of how we got here;
this file is the answer, not the journey.

## How to use and maintain this file

- **Facts are corrected in place**, not appended to. When a fact turns out to be wrong,
  rewrite it and move the old version to *Errors made* with its root cause. A file that
  accumulates contradictions is worse than no file.
- **Every fact carries provenance** — how it was measured, and how to re-verify it. A number
  with no provenance is an opinion.
- **Cite fact IDs** (`K-12`) in the research log, in commit messages, and in code comments.
- **Confidence tags**: `[measured]` reproducible from data in this repo · `[external]` from a
  cited source, not independently verified · `[inferred]` reasoning from other facts ·
  `[assumed]` load-bearing but unverified — these are the dangerous ones.

Last updated: 2026-08-17 · Trials recorded: **162** (see `data/trials.json`)

---

## 1. Objective and constraints

| ID | Fact | Confidence |
|---|---|---|
| K-01 | Capital is **₹10,000**, growing only from profits. Not the user's income; patient. | given |
| K-02 | Broker is **Dhan** (free API). Zero brokerage on equity delivery; `min(₹20, 0.03%)` per order intraday. | [external] |
| K-03 | Goal is a system that trades, adapts and learns, running **multiple** strategies over time — not one bet. | given |
| K-04 | The user codes and understands maths. Explanations should be quantitative, not simplified. | given |

## 2. Market structure — the constraints no code can remove

| ID | Fact | Confidence |
|---|---|---|
| K-10 | **Retail cannot hold an overnight short in NSE cash equity.** Shorts must be squared off intraday (MIS). SLB exists but is illiquid and impractical at this size. | [external] |
| K-11 | NSE F&O lot sizes are **₹5–15 lakh notional** post-SEBI Oct-2024. Unreachable at ₹10,000. | [external] |
| K-12 | **Therefore: positional long/short is structurally impossible at ₹10,000.** Only long-only positional, or intraday long/short, are available. | [inferred] from K-10, K-11 |
| K-13 | SEBI algo rules fully enforced since **1 Apr 2026**. Under **10 orders/sec** counts as a normal API user — no algo registration. Static IP whitelisting with the broker is mandatory. | [external] |
| K-14 | **91% of individual F&O traders lost money in FY25** (₹1.05 trillion aggregate). **96–97% of prop/FPI profits come from algorithms.** Speed-dependent strategies are unwinnable from a home connection. | [external] SEBI |
| K-15 | India taxes crypto gains at **30% flat with no loss offset**; equity F&O is business income where **losses offset gains**. This alone favours equities strongly for a loss-making-tail strategy. | [external] |
| K-16 | Delta Exchange India lists only **8 liquid perps**, and just BTC/ETH/SOL/XRP have real depth (BMTUSD shows $48M turnover on $236k OI — churn, not liquidity). | [measured] |

## 3. Costs — all measured, all reproducible

| ID | Fact | Confidence |
|---|---|---|
| K-20 | **Delta crypto**: taker 5.9 bps, maker 2.36 bps (0.05%/0.02% + 18% GST). Maker-entry + taker-exit round trip = **8.26 bps**; taker both sides = 11.80 bps. | [measured] `src/backtest/costs.py` |
| K-21 | **NSE intraday (Dhan)**: **₹10.6045 per ₹10,000 round trip = 10.60 bps.** Verified against Dhan's calculator. Breakdown: brokerage 6.00, STT 2.50, exchange 0.594, SEBI 0.02, stamp 0.30, GST 1.1905. | [measured] `tests/test_nse_costs.py` |
| K-22 | **NSE fees are NOT scale-invariant.** The ₹20-per-order cap binds above **₹66,667** notional, after which the effective rate falls: 10.60 bps at ₹10k → 8.24 at ₹100k → 5.10 at ₹300k → 4.47 at ₹500k. | [measured] |
| K-23 | **NSE legs are asymmetric**: STT 0.025% is **sell-side only**, stamp duty 0.003% is **buy-side only**. A buy leg costs ₹4.20 per ₹10k, a sell leg ₹6.40. | [measured] |
| K-24 | **NSE delivery** costs ~21.5 bps round trip (zero brokerage on Dhan; STT 0.1% *both* sides dominates). Cheaper than intraday only below ~2 round trips/year — otherwise intraday's 10.6 bps wins. | [measured] |
| K-25 | **Fee drag by rebalance frequency at ₹10,000** (delivery): daily **47.8%/yr**, weekly 6.0%, monthly **0.7%**, quarterly 0.2%. **Turnover is the single most important design parameter at this account size.** | [measured] |
| K-26 | Our cost models charge **no bid-ask spread** by default (`half_spread_bps = 0`), deliberately, so fee arithmetic stays auditable. Results must therefore be reported as a **break-even spread**, never as a net return. | [measured] |
| K-28 | **An intraday (MIS) book pays a full round trip every session — 10.60 bps/day on gross 1.0 — and this is a FLOOR that no turnover control can lower.** Positions cannot be carried (K-10), so a name kept in the book is still sold at the close and rebought at the next open. Rebalance bands, signal smoothing and slower ranking all fail to help. | [measured] |
| K-29 | The overnight-gap signal **persists for at least 8 sessions** (t = 2.3–6.8 at every horizon t+1…t+8), but **multi-day holds destroy it**: gross/rebalance is ~8 bps at H=1, −2.12 at H=5. The edge is purely *intraday*; the overnight legs reverse it (close-to-close is −6.1 bps/day). Therefore it cannot be made cheaper by holding longer. | [measured] |
| K-27 | Corwin–Schultz estimates NSE F&O effective spread at ~42.7 bps median, ~68.7 bps for extreme-gap names. **Known to be biased high when daily ranges are wide**, so treat as an upper bound, not truth. Real quotes for liquid large caps are ~2–10 bps. | [external] + [measured] |

## 4. Integer-size granularity

| ID | Fact | Confidence |
|---|---|---|
| K-30 | **Delta crypto at ₹10,000**: BTCUSD 1 contract = $64.92 = 55% of the account; SOLUSD 0.7 contracts at 2% risk — unsizable. Only ADA/XRP/DOGE/ETH have workable granularity. | [measured] |
| K-31 | **NSE at ₹10,000**: no lot size, but 1 share is the quantum. 10 names at ₹1,000 → 83/196 affordable, 14.2% median sizing error. 20 names at ₹500 → 60/196 affordable, 21.9% error. | [measured] |
| K-32 | NSE F&O share prices: median ₹1,237, 25th pct ₹393, 75th pct ₹2,515. Cheapest IDEA ₹13.8, dearest BOSCHLTD ₹46,875. | [measured] |
| K-33 | Delta's position sizer silently misbehaves outside a stop window of **[0.57%, 23.6%]** of price: below it the balance cap under-risks (a 0.2% stop risks ₹0.83 not ₹2.36), above it `PositionSizer.calculate` returns 0 and the trade vanishes. | [measured] |

## 5. Edges tested — results

**Nothing has passed the pass mark (§7). ~160 crypto hypotheses plus the NSE work below.**

| ID | Hypothesis | Result | Verdict |
|---|---|---|---|
| K-40 | 7 indicator strategies, crypto, 2y bull+bear | 0/15 significant, best t=1.45; 0/15 positive in both years | **FAIL** — directional beta, not alpha |
| K-41 | Crypto cross-sectional **momentum** | Best t=1.83, +49.3% over 2y, DSR **0.19**, max DD 10.5%. Positive in both years — the only thing that ever was. But **Q1 carries +41.2 of +49.3 points**. | **FAIL** — one good quarter |
| K-42 | Crypto cross-sectional reversal | 36 configs, 0 with t>2, best t=0.49 | **FAIL** |
| K-43 | Price action, structural stops, crypto | All 18 configs negative, **−41 to −55 bps/trade**, t from −4 to −24 | **FAIL** — worse than the indicators it replaced |
| K-44 | **NSE overnight-gap intraday reversal** | Gross **10.06 bps/day (10 legs), t=6.82** on the clean spec. Honest intraday cost is **10.60 bps/day** (K-28). **Net −0.54 to −3.24 bps/day, negative at every leg count.** | **FAIL on cost, not on signal** |
| K-45 | NSE long-only monthly momentum | +17.78pp excess on today's F&O list → **+1.70pp (t=0.38)** on a point-in-time proxy universe. DSR **0.023**. | **FAIL** — 90% was survivorship bias |

**K-46 — The most useful result so far**: K-44's signal is real and statistically strong
(t=6.82, and it still predicts intraday returns **8 sessions later** at t=2.3–6.8). It fails
only because the intraday cost floor (K-28) exceeds it *at this account size*. Because of
K-22, the same signal turns positive once per-order notional clears the brokerage cap. With a
20-name book at 4× MIS and 10.06 bps/day gross:

| Capital | Per-order | Cost bps/day | Net bps/day | Net %/yr |
|---|---|---|---|---|
| ₹10,000 | ₹2,000 | 10.60 | −0.54 | −1.4% |
| ₹1,00,000 | ₹20,000 | 10.60 | −0.54 | −1.4% |
| **₹5,00,000** | ₹1,00,000 | 8.24 | **+1.82** | **+4.6%** |
| ₹10,00,000 | ₹2,00,000 | 5.88 | +4.18 | +10.5% |
| ₹30,00,000 | ₹6,00,000 | 4.31 | +5.75 | +14.5% |

**Break-even is ~₹5 lakh**, and this is before any bid-ask spread (K-26), which pushes it
higher. Still the only case where "no edge" has resolved into "edge above ₹X". `[measured]`

## 6. Dead ends — do not re-test without a new reason

| ID | Dead end | Why |
|---|---|---|
| K-50 | Hour-of-day seasonality (crypto) | Best hour +3.22 bps against 8.26 bps cost; no \|t\| above 2.2 across 24 hours |
| K-51 | Pair-spread mean reversion (crypto) | 54 configs, 0 with t>2, best t=0.82 |
| K-52 | `volume_breakout` strategy | Fires **zero** trades across 7 symbols over a year — entry conditions never co-occur |
| K-53 | TradingView integration | 25–45 s webhook latency, ~34 s median; sends indicators we compute locally in <1 s; its tester has no GST-inclusive fees, no funding, no walk-forward |
| K-54 | Funding carry on Delta | Median funding is exactly 0.0100%/8h — the **floor rate**, not a market signal. ~11%/yr, needs two legs across INR-spot and USD-perp. ₹13/year at ₹10,000. |
| K-55 | Vol-normalised ranking, inverse-vol leg weighting | Both expected to raise power; both made crypto XS results **worse**. Default off. |

## 7. The pass mark — pre-committed, do not weaken

A strategy ships to paper **only if all four hold**:

1. **Deflated Sharpe > 0.95** against the *full* trial count (currently 162 and rising)
2. **Positive in both halves** of the sample
3. **≥ 100 observations**
4. **Net of measured costs**

`K-60` — The trial count is self-penalising by design: every hypothesis tested raises the bar
for the next one. An automated search that generates thousands of candidates is defeated by
arithmetic rather than by good intentions. This rule has already killed three convincing
false positives (K-72, K-73, K-75).

## 8. Errors made, and their root causes

**This section exists so they are not repeated. Every one produced a confident wrong answer.**

| ID | Error | Root cause | Guard now in place |
|---|---|---|---|
| K-78 | The NSE intraday backtester charged only `book - held`, so a name kept in the book cost **nothing** to hold. Understated every intraday cost and made the seed sleeve look profitable. | Turnover accounting copied from a *positional* backtester, where carrying inventory is real. Under MIS you cannot carry. The code's own comment claimed it charged for re-establishing; it did not. | `squares_off_daily=True` charges a full round trip per session. `test_daily_square_off_imposes_a_cost_floor`. |
| K-79 | The `IntradayCrossSectional` strategy class shipped with the K-71 contamination baked in: `Signal.OVERNIGHT_GAP` used *this* session's open. | Fixed the diagnosis in the analysis script but not in the class the backtester actually runs. | Signal now lags one session; `test_close_to_close_signal_ignores_todays_prices`. |
| K-70 | Reported crypto momentum as reliably **negative** (t=−2.95); the true sign is **positive** (t=+2.10). A whole round was planned on the inverted conclusion. | `sort_values(ascending=reverse)` — the "momentum" branch sorted *descending*, so `index[-k:]` took the **lowest**-ranked names and went long them. | Explicit `Tilt` enum (`LONG_WINNERS`/`LONG_LOSERS`), never a bare sign. `test_long_winners_actually_buys_the_winners`. |
| K-71 | NSE overnight-gap showed **Sharpe 6.48, t=13.41**. | Signal `O_t/C_{t-1}` and return `C_t/O_t` **share the price `O_t`**. Noise in that print makes a name look like a bigger loser *and* gives it a higher return. Mechanical, not economic. | Always check whether signal and return share a price. Clean spec uses `O_{t+1}→C_{t+1}`. |
| K-72 | A 4h crypto config showed **+41.6%**; on six symbols instead of three it was **−58.4%**. | Swept 30 configs on a 3-symbol subset and reported the best. Textbook multiple testing. | Trials registry + deflated Sharpe (K-60). |
| K-73 | NSE long-only momentum showed **56.77% CAGR**, +17.78pp excess. | **Survivorship bias**: the universe is *today's* F&O list backfilled 5 years. Stocks promoted after a run-up appear with their whole run-up; demoted stocks are absent. Momentum buys past winners — it selects exactly what the bias inserted. | K-80. Always test against a point-in-time proxy universe. |
| K-74 | Deflated Sharpe computed as exactly **0.0000**, which looked like a bug in the statistics. | Trials registry stored **annualised** Sharpe while `evaluate()` computes **per-period** — inflating trial dispersion ~7×. | Registry stores per-period Sharpe. Units asserted in tests. |
| K-75 | Believed `swing_high >= swing_low` was an invariant. | They are independent forward-fills confirmed at different bars; in a trend the newer low can sit above an older high. **The test was wrong, the code was right.** | Replaced with "levels are drawn from actual past bars". |
| K-76 | Paper trading booked −100% of notional on every close. | `close_position_verified` built its Order without a price, then `avg_fill_price = order.price or 0.0`. | `mark_price` is required in paper mode; `_on_fill` rejects non-positive prices. |
| K-77 | A duplicate exit fill opened a **phantom position facing the wrong way**. | `_on_fill` inferred intent from "is there a local position?" rather than from the order. | `Order.is_exit` flag; late exit fills discarded. |

**Meta-lesson**: every one of these produced a *plausible* number. The defences that caught
them were structural (typed enums, shared-price checks, trial counting, point-in-time
universes) — not care or attention.

## 9. Data assets and their known defects

| ID | Asset | Defect |
|---|---|---|
| K-80 | `data/nse/hist/` — 208 names, 196 after coverage filter, 1,085 sessions (2021-11→2026-08), split/dividend adjusted | **SURVIVORSHIP BIASED.** Today's F&O list backfilled. Inflates long-only momentum ~10× (K-73). **Blocking defect for any long-only backtest.** |
| K-81 | `data/hist/` — Delta crypto, 6 symbols, 730 days, 15m/1h/4h, real production prices | Only ~2 years available; venue has no more. Y1 bull, Y2 severe bear. |
| K-82 | `data/trades.db` `news_items` — RSS headlines, collecting since 2026-08-10 | No backfill possible on free tiers. Coverage is heavily BTC-skewed (26 BTC / 4 XRP / 1 ETH in the first poll). |
| K-83 | `data/microstructure.parquet` — funding, OI, basis, per scan | **No history exists and none can be obtained.** Records only; no feature may be built on it until enough time has passed. |
| K-84 | Delta cached CSVs under `data/*.csv` were **testnet** prices — frozen tails, flat bars, PAXGUSD at ₹0.01. Superseded by `data/hist/` parquet from production. | Do not use the CSVs for research. |

## 10. Open questions — ranked by value

| ID | Question | Why it matters | Blocked on |
|---|---|---|---|
| K-90 | **Survivorship-free NSE universe** | Long-only positional is the *only* structure available at ₹10,000 (K-12), and it cannot be measured honestly without this (K-73, K-80). Highest-value open item. | Historical NIFTY 500 constituents, or a broad universe including delisted names |
| K-91 | Real bid-ask spread on NSE names | Decides K-44/K-46 — whether the intraday signal is tradeable at scale | Quote/tick data from Dhan |
| K-92 | Does a per-order **minimum** fee exist on Dhan? | On ₹1,000–10,000 orders an absolute floor would multiply effective bps and invalidate K-21 | One live ₹500 round trip |
| K-93 | Does K-44's signal survive at lower turnover? | Fees scale with turnover (K-25); a weekly version might clear costs at ₹10,000 | Nothing — testable now |
| K-94 | Is the illiquid-momentum premium (19.43% vs 8.51% CAGR) reproducible? | The strongest external claim we have, and small capital is *advantaged* by it | K-90, plus a universe wider than F&O names |

## 11. External claims not yet independently verified

Treat as hypotheses, not facts, until reproduced here.

- `[external]` NSE momentum alpha concentrates in **low-turnover** names: 19.43% CAGR vs
  8.51% for the liquid half, Nifty 50 at 10.41% (19-year backtest, costs and taxes modelled).
  Both the advocate and the sceptic agree the mechanism is an **illiquidity premium** that
  funds above ~₹100cr AUM cannot harvest — so **small capital is the qualification, not the
  handicap**. Capacity roughly ₹15–75 lakh.
- `[external]` Crypto cross-sectional momentum: 2.62% weekly alpha (t=4.22) on a broad
  cross-section. Our 4–6 name universe is far too thin to resolve this (K-16).
- `[external]` Short-term reversal is among the most robustly documented equity anomalies
  globally.
