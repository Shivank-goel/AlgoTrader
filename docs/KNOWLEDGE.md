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

Last updated: 2026-09-18 · Trials recorded: **206** (verified against `data/trials.json`; previous header was stale)

Integration status (2026-09-18): FYERS login, REST data, public instrument
resolution and SDK streaming have been verified. A 15-second stream check recorded
two SBIN/INFY ticks; neither had a usable two-sided book. `main.py fyers` now
provides recording, readiness, cost estimates, halt and gated paper commands.
The paper ledger and disabled limit-order transport have mocked tests. No live
order was placed; no strategy qualified. Production position reconciliation,
order-update integration and market-hours deployment drills remain outstanding.
See `docs/FYERS_OPERATIONS.md` for exact capabilities and blockers.
Remaining work is planned in `docs/IMPLEMENTATION_PLAN.md`; that document describes
future work and does not change the evidence or deployment status recorded here.
P0 paper-risk update: validated reductions remain available under entry halts;
valuation now runs independently of orders and preserves prior-day equity, P&L
and fee accounting across restart. Regression tests use synthetic data; no new
strategy evidence or live order was generated.

---

## 0. CONCLUSION (2026-08-25)

**None of the tested candidates has qualified for deployment at ₹10,000.**
The historical research below used the then-current cost assumptions. It does
not prove that no possible strategy can work, and is not a FYERS net-return
validation. Preserve the negative results without treating them as a universal claim.

- Every **active** structure is closed: positional long/short is impossible (K-12), intraday
  long/short sits below a cost floor it cannot amortise (K-28, K-44).
- The one **real** effect found — NSE 12-1 momentum, survivorship-free, persistent across all
  five 3-year windows, concentrated in low-turnover names (K-47, K-48) — degrades badly at
  ₹10,000 because only 40% of its picks are affordable (K-49), and is **available in a fund
  that beats DIY after tax** (K-50b).
- It also never cleared the pre-committed pass mark (DSR 0.354 against 0.95).

**What to do with ₹10,000: buy a NIFTY200 Momentum 30 index fund.** It captures the same
effect, defers tax that DIY realises monthly, and costs nothing to operate.

**This is not "nobody trades ₹10,000 in India."** Millions do — options alone are affordable
at this size (K-11), which an earlier version of K-11 wrongly denied. The claim is narrower
and survives the correction: of the structures reachable at ₹10,000, the systematic ones fail
on cost and the affordable derivative one (option buying) is the negative-expectancy side of a
trade whose profitable side needs ~₹1.5 lakh of margin (K-11b). SEBI measures the outcome:
**91% of individual F&O traders lose** (K-14).

**Is a profitable algo system possible at all? Yes — but it is gated on capital, not code
(K-56, K-57).** Low-turnover momentum captures the capacity-constrained premium funds cannot
touch, beating the fund replica by +5.26pp/yr. The catch is diversification: at ₹10,000 only a
**concentrated 8-name** version is affordable, and it carries a **52% drawdown** for an excess
that is not statistically significant (t=1.08). At **₹50,000–1 lakh** you hold 15 names at
93–97% affordability with the same edge and far less concentration risk. That is the growth
path: fund the account, not the algorithm.

**What the system is worth**: a research instrument that reached a correct negative
conclusion and rejected four convincing false positives on the way (K-70 to K-79). Revisit
active strategies at **₹5 lakh+** (K-46), where the intraday cost floor stops binding.

## 1. Objective and constraints

| ID | Fact | Confidence |
|---|---|---|
| K-01 | Capital is **₹10,000**, growing only from profits. Not the user's income; patient. | given |
| K-02 | User's broker and selected NSE target is **FYERS**. `main.py fyers` runs NSE observation and qualified paper intents; `main.py run` remains the Delta engine (now defaults to paper). FYERS live transport is disabled and has no production enable command. Dhan is legacy/optional. | given; `src/fyers/`, `config/settings.yaml` |
| K-03 | Goal is a system that trades, adapts and learns, running **multiple** strategies over time — not one bet. | given |
| K-04 | The user codes and understands maths. Explanations should be quantitative, not simplified. | given |

## 2. Market structure — the constraints no code can remove

| ID | Fact | Confidence |
|---|---|---|
| K-10 | **Retail cannot hold an overnight short in NSE cash equity.** Shorts must be squared off intraday (MIS). SLB exists but is illiquid and impractical at this size. | [external] |
| K-11 | NSE F&O **futures** lots are ₹5–15 lakh notional — unreachable at ₹10,000. **But options are bought on PREMIUM, not notional, and are very much reachable**: on 2026-08-21, **9,232 of 16,751 traded option contracts (55%) cost ≤ ₹10,000 per lot**, including liquid NIFTY strikes at ₹773/lot against 17.7M open interest. An earlier version of this fact wrongly generalised the futures constraint to all F&O. | [measured] |
| K-11b | **Access is not edge, and at ₹10,000 you get the losing side.** Option *buying* is affordable and carries the volatility risk premium against it — index implied vol persistently exceeds realised, which is why sellers win on average. Option *selling* has the positive expectancy but needs SPAN margin of roughly **₹1.3–1.9 lakh per NIFTY lot** (~8–12% of ₹15.8 lakh notional). So ₹10,000 buys entry to the structurally negative-expectancy side only. | [external] + [inferred] |
| K-12 | **Positional long/short in CASH equity is impossible at ₹10,000** (K-10 forbids overnight shorts; futures lots are out of reach). Available: long-only positional, intraday long/short, and **option buying** — the last being affordable but negative-expectancy (K-11b). | [inferred] from K-10, K-11, K-11b |
| K-13 | SEBI algo rules fully enforced since **1 Apr 2026**. Under **10 orders/sec** counts as a normal API user — no algo registration. Static IP whitelisting with the broker is mandatory. | [external] |
| K-14 | **91% of individual F&O traders lost money in FY25** (₹1.05 trillion aggregate). **96–97% of prop/FPI profits come from algorithms.** Speed-dependent strategies are unwinnable from a home connection. | [external] SEBI |
| K-15 | India taxes crypto gains at **30% flat with no loss offset**; equity F&O is business income where **losses offset gains**. This alone favours equities strongly for a loss-making-tail strategy. | [external] |
| K-16 | Delta Exchange India lists only **8 liquid perps**, and just BTC/ETH/SOL/XRP have real depth (BMTUSD shows $48M turnover on $236k OI — churn, not liquidity). | [measured] |

## 3. Costs — all measured, all reproducible

The NSE measurements below use historical **Dhan** assumptions. They are retained
for reproducibility, not validated FYERS costs. FYERS brokerage, DP charges and
applicable taxes must be verified before qualifying a strategy for FYERS.

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
| K-45 | NSE long-only monthly momentum, **F&O-list universe** | +17.78pp excess → **+1.70pp (t=0.38)** on a crude point-in-time proxy. Superseded by K-47, which measures the bias properly. | **FAIL** |
| K-56 | **There IS a path, and it runs through capital, not cleverness.** Low-turnover momentum captures the capacity-constrained premium a fund structurally cannot: top-8 low-turnover returns 27.24% CAGR vs 16.06% for the high-turnover half, and beats the fund replica by **+5.26pp/yr**. Concentration also fixes granularity — **top-8 has 79% of picks affordable at ₹10,000** vs 40% at top-25. | [measured] |
| K-57 | **But the ₹10,000 version costs a 52% drawdown for an edge that is not significant.** Top-8: max DD **52.3%** (fund replica 36.2%), worst 12 months **−49.1%**, excess t=**1.08**, DSR 0.096 full / 0.432 family-scoped. Both halves positive but H1 only +1.68pp. At ₹50k–1 lakh you hold 15 names at **93–97% affordability** with the same edge and far less concentration risk — that is where this becomes attractive. | [measured] |
| K-58 | Break-even on the low-turnover tilt is roughly a **50 bps half-spread**; at 100 bps it loses to the fund. Illiquid names are where the alpha is *and* where spreads are widest, so K-91 (real quote data) now gates a live decision rather than being merely interesting. | [measured] |
| K-48 | **The momentum effect is real, persistent, and concentrated where theory says.** Positive in **all five** rolling 3-year windows (+4.4 to +11.9pp/yr, no single window significant, t 0.6–1.7). Splitting the universe by turnover: **low half +5.73pp (t=2.07), high half +2.31pp (t=0.79)** — the alpha lives in the less liquid names, exactly as the external capacity-constrained claim predicts (§11). | [measured] |
| K-49 | **It does not survive ₹10,000 intact.** Integer shares: unconstrained t=2.36 → ₹1L t=2.25 (91% of picks affordable) → **₹10,000 t=1.68, only 40% of picks affordable, 17.6% median sizing error**. You end up holding a price-biased subset of the signal, not the signal. | [measured] |
| K-50b | **DECISIVE: the same effect is buyable in a fund, and the fund wins.** NIFTY200 Momentum 30 index funds exist (UTI, ICICI, Motilal; expense ~0.3–0.5%) and their methodology — NIFTY200 universe, 6m+12m momentum, top 30, semi-annual — is a near-replica of our best config, which returns 19.10% CAGR. **DIY monthly after 20% STCG ≈ 15.3%/yr; the fund after fee and 12.5% LTCG ≈ 16.4%/yr.** The gap is structural, not a tuning artefact: **a fund's internal rebalancing is not a taxable event for the unitholder, while every DIY rebalance is.** DIY cannot replicate that. | [measured] + [external] |
| K-47 | **NSE 12-1 momentum, true point-in-time universe** (K-85) | 15 years, 168 months, top-25 of a top-200 universe, delivery costs. Excess **+7.79pp over an equal-weight benchmark, t=2.36**, H1 +6.78pp / H2 **+8.91pp**. DSR **0.354** (0.59 family-scoped). 9/18 configs positive in both halves. | **FAIL** — passes 2 of 3, misses DSR |

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

1. **Deflated Sharpe > 0.95** against the *full* trial count (206 as verified 2026-09-18; always read the registry)
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
| K-73 | NSE long-only momentum showed **56.77% CAGR**, +17.78pp excess. | **Survivorship bias**: the universe was *today's* F&O list backfilled. Stocks promoted after a run-up appear with their whole run-up; demoted stocks are absent. Momentum buys past winners — it selects exactly what the bias inserted. **Measured properly (K-85): the bias is worth 7.18pp of 13.87pp excess, i.e. ~52%.** The earlier '90%' from a crude proxy overstated it. | K-85 gives a true point-in-time universe. |
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
| K-85 | **`data/nse/bhavcopy/` is the survivorship-free universe.** NSE publishes, per trading day, one row per security that actually traded — so reading day by day *is* the point-in-time universe, with no reconstruction. Public, unauthenticated, two formats (UDiFF from 2024-01, legacy before). **4,064 trading days cached, 2011-07 → 2026-08, 3,483 securities.** | [measured] |
| K-86 | **ISIN only appears in bhavcopy from ~2011-07.** Earlier files carry SYMBOL only, and a ticker freed by a delisting can be reassigned — keying on it would splice two companies into one history. `fetch_day` **raises** before that date rather than degrading silently. Archives do go back to 2005 if a symbol-keyed panel is ever acceptable. | [measured] |
| K-87 | **Bhavcopy prices are RAW, not adjusted** (unlike Yahoo). Chain `close/prev_close` instead — NSE adjusts `prev_close` on ex-dates. Raw close-to-close chaining reads WINSOME's consolidation as **+3750%** where the true return is −1.9%. 2,661 such days in 3.38M rows: rare, but momentum selects extreme movers, so it concentrates in exactly them. | [measured] |
| K-84 | Delta cached CSVs under `data/*.csv` were **testnet** prices — frozen tails, flat bars, PAXGUSD at ₹0.01. Superseded by `data/hist/` parquet from production. | Do not use the CSVs for research. |

## 10. Open questions — ranked by value

| ID | Question | Why it matters | Blocked on |
|---|---|---|---|
| K-90 | Survivorship-free NSE universe | **SOLVED (2026-08-18)** by K-85 — daily bhavcopy, 4,064 days, 15 years, no credentials. Kept here rather than deleted so the reference stays resolvable. | — |
| K-91 | Real bid-ask spread on NSE names | Decides K-44/K-46 — whether the intraday signal is tradeable at scale | Recorder implemented; representative market-hours two-sided quotes still needed |
| K-92 | Does the FYERS cost estimate match actual account charges and rounding? | Published tariff is encoded in `config/fyers_costs.yaml`: intraday min(₹20, 0.03%), delivery min(₹20, 0.3%), delivery DP ₹12.5 + GST per ISIN/day. Existing Dhan results are not FYERS returns. | [external] [FYERS charges](https://fyers.in/charges-list), contract-note reconciliation pending |
| K-93 | Does K-44's signal survive at lower turnover? | Fees scale with turnover (K-25); a weekly version might clear costs at ₹10,000 | Nothing — testable now |
| K-95 | **Should the DSR trial count be family-scoped?** The pass mark says "full trial count". For K-47 that is 198, of which 120 are crypto trials on a different market — giving DSR 0.0002 versus 0.5897 scoped to the 36 NSE momentum trials. Bailey/López de Prado deflate for trials *within a search*. **Not urgent: K-47 fails either way**, so nothing currently hinges on it. Decide before a result sits between the two. | Deliberate, recorded decision — never a silent change after seeing a result |
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

## 12. Deployment facts

| ID | Fact | Confidence |
|---|---|---|
| K-96 | Dhan's v2 order API accepts a user-generated `correlationId` (maximum 30 permitted characters) and exposes order lookup by that ID. Order placement/modification/cancellation requires static-IP allowlisting; individual access tokens expire after 24 hours. The Dhan gateway must resolve an ambiguous order request by correlation ID before any retry. | [external] [Dhan orders](https://dhanhq.co/docs/v2/orders/), [authentication](https://dhanhq.co/docs/v2/authentication/) |
| K-97 | SEBI's retail API-algo framework applies to all stock brokers from **1 Apr 2026**. Dhan documents a 10 order/second order-API limit. Deployment must include the broker/exchange controls and rate limiting; this is not a strategy concern. | [external] [SEBI circular](https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf), [Dhan releases](https://dhanhq.co/docs/v2/releases/) |
| K-98 | Dhan trading APIs are free, but Dhan's current public pricing lists its real-time/historical Data API at ₹499/month. FYERS states that its trading, historical, quote, and market-data APIs are free for its clients. Neither removes statutory transaction costs or retail-algo controls. | [external] [Dhan authentication](https://dhanhq.co/docs/v2/authentication/), [Dhan pricing](https://dhanhq.co/trading-apis), [FYERS fees](https://support.fyers.in/portal/en/kb/articles/does-fyers-charge-any-subscription-fees-for-trading-api) |
