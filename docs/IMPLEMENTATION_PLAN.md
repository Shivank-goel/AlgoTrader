# Remaining implementation: evidence-led profitable trading system

## Architecture-plan update — 2026-09-18

The approved platform-first sequence is now tracked in [ARCHITECTURE.md](ARCHITECTURE.md).
Its twelve phases supersede the ordering below; the detailed P0–P6 requirements
remain useful acceptance criteria, not evidence of completion.

First batch implemented: fail-closed trial-history validation, locked atomic
appends, dashboard control authorization and offline CI configuration. Phase 1
is not complete: shared boundary contracts and broader release controls remain.
No research experiment, deployment promotion or broker order is part of this batch.


Planned 2026-09-18. This is a backlog, not a claim that the work is implemented.
Goal: establish a repeatable positive net trading edge, then automate its execution
within an agreed loss budget. Profitability is an empirical outcome, not a software
deliverable that can be guaranteed.

## Starting point and assumptions

- Retain the recorded ₹10,000 capital constraint (K-01) unless the user changes it.
  Do not assume extra deposits, leverage or a move to derivatives to rescue results.
- Start with FYERS/NSE cash equities and long-only delivery, matching the current
  paper infrastructure. Other products require a separate economic case and implementation.
- Login, REST data, master resolution and short streaming checks work. The previous
  implementation run reported 412 passing tests; that is not strategy evidence.
- No candidate has passed K-60. The full registry last contained 206 trials; read
  its actual count on every research run. Historical Dhan results are not FYERS returns.
- Current paper mode consumes explicit intents; there is no autonomous NSE strategy loop.
- Define the risk-adjusted benchmark and acceptable total drawdown before selecting
  a candidate. Existing daily/order limits are configuration defaults, not evidence
  that the user has accepted a live loss budget.

## Priority and dependencies

| Phase | Deliverable | Exit condition |
|---|---|---|
| P0 | Correct risk and qualification boundaries | Regression tests establish fail-closed entries and controlled risk reduction |
| P1 | Market-hours dataset and quality reports | Session coverage and data defects measured; replay is deterministic |
| P2 | Shared FYERS backtest/paper economics | Same trades produce matching fees/accounting; conservative cost sensitivity published |
| P3 | Bounded, reproducible strategy research | A candidate passes K-60 and the predeclared holdout/risk criteria, or is rejected |
| P4 | Autonomous qualified paper strategy | Signals, orders and accounting survive replay, restart and forward observation |
| P5 | Production execution and reconciliation | Full lifecycle and failure drills pass; all release blockers are enforced in code |
| P6 | Small, explicitly enabled live pilot | Measured execution/costs stay within predeclared tolerances before any scaling |

Start P0 and P1 together. Build P2 while recording. P3 depends on P2 and suitable
historical data; a short live recording does not supply years of research history.
P4 strategy deployment requires P3. Build/test P5 offline in parallel with P4,
but live activation requires both. Estimates should follow task sizing; do not
promise a fixed date for strategy discovery or a sufficient forward sample.

## P0 — Fix correctness gaps before more trading features

Progress 2026-09-18: items 1–2 implemented for the paper service, including
runtime monitoring, legacy accounting recovery and regression tests. Qualification
provenance hardening and the live release boundary (items 3–5) remain pending.
These paper fixes do not establish live execution readiness.

Targets: `src/fyers/paper.py`, `orders.py`, `qualification.py`, `runtime.py`, tests.

1. Separate opening/increasing exposure from reducing/closing it. The original paper
   implementation applied halts, qualification, order caps and stale unrelated
   holdings to sells as well as buys; this has now been corrected. Exits still
   require validated position quantity, session and executable price; never create
   a short or invent a fill to bypass a data outage.
2. Mark equity independently of order submissions. Persist prior-close equity,
   fees, realized/unrealized P&L and daily loss state; account for overnight gaps.
   The original baseline initialized on a fill attempt. The paper monitor now runs
   on stream processing/timeouts and retains its last complete prior-day valuation.
   If a closing mark is missing, that carry-forward is explicitly a last observation,
   not a fabricated official closing price.
3. Bind qualification to strategy code/config hashes, data manifest, timestamps,
   return frequency/units, benchmark and cost/slippage assumptions. A matching
   name, sample count and Sharpe are insufficient to identify an exact strategy run.
4. Validate malformed evidence and zero/invalid trial dispersion explicitly. Audit
   whether cross-trial Sharpe units/frequencies are comparable; retain the full
   trial count and 0.95 threshold, and do not silently substitute family-only tests.
5. Make live release a checked application boundary. `enable=True` is currently a
   transport switch, not qualification, portfolio risk or deployment authorization.

Acceptance: stale/revoked qualification prevents new exposure; a validated close
can reduce an existing position; overnight losses trigger the configured policy;
changed code/config/data invalidates evidence; malformed evidence cannot enable orders.

## P1 — Collect usable data, not just successful connections

Targets: `src/fyers/runtime.py`, `stream_worker.py`, `instruments.py`, journal and CLI.

- Add a session supervisor, holiday/session awareness, daily master refresh and
  token-expiry alerts. Keep daily user authentication explicit.
- Record exchange/receive timestamps and connection generations. Track missing
  subscriptions, queue overflow, duplicate/out-of-order events, timestamp skew,
  stale books and SDK transport status even when markets are quiet.
- Build a daily report: eligible recording windows, connection uptime, per-symbol
  valid-book availability, spread distribution and missing intervals. Zero activity
  must not automatically be labelled a network fault.
- Add deterministic journal replay and session-based export, schema versions,
  rotation/retention and tested backups. Never fill gaps silently with future data.
- Extend beyond the two smoke-test symbols only after selecting a research universe;
  record both liquid and less-liquid names if investigating the liquidity hypothesis.

Initial engineering target: five complete market sessions with disconnect, token
failure and process-restart drills, no silent data loss, and every invalid interval
flagged. Five sessions validate operation only; cost/edge estimates may need much
more coverage across symbols and conditions. Set numeric data-quality tolerances
before using the dataset for a candidate and report them explicitly.

## P2 — Make economics and accounting trustworthy

Targets: `src/fyers/costs.py`, `paper.py`, existing `src/backtest/` and NSE data modules.

- Adapt the existing backtest interfaces to the FYERS cost model; preserve the
  original Dhan models/results for reproducibility. Include delivery brokerage,
  per-ISIN/day DP charges, statutory fees, price/quantity rounding and whole shares.
- Model product/holding-period treatment, settlement and available cash. The current
  simulator charges delivery fees on same-day closes; document and correct this
  before comparing product-specific strategy returns.
- Separate actual order fills from price marks. Add pending orders, latency,
  cancel/replace and conservative partial fills without assuming resting limits
  fill merely because a candle touched their price.
- Use measured bid/ask and slippage distributions when available; otherwise publish
  assumptions and break-even spread. Include infrastructure costs at ₹10,000 and
  report pre-personal-tax results separately from tax scenarios.
- Reconcile against the official calculator and existing account records when
  available; do not place a trade solely to obtain a contract note during this phase.

Acceptance: deterministic ledger replay and backtest/paper fee parity for identical
orders, including repeated sells, partial fills, settlement boundaries and restarts.
Produce a capital/turnover/cost feasibility report before searching more parameters.
FYERS charges are sourced from its [published tariff](https://fyers.in/charges-list);
the tariff must be rechecked before deployment.

## P3 — Find out whether there is a deployable edge

Targets: existing backtest/statistics/trials machinery, `qualification.py`, research docs.

1. Reproduce the strongest prior result as an audit baseline: low-turnover NSE
   momentum on the point-in-time universe (K-47, K-56, K-57), using FYERS costs,
   realistic liquidity and ₹10,000 integer allocation. This is a feasibility
   re-evaluation, not a presumption that the previously failed candidate now qualifies.
2. Before running it, freeze the specification, benchmark, data split and cost
   scenarios. Register each evaluated configuration using `TrialsRegistry`.
3. If it fails, diagnose the failure: absent gross edge, execution costs, insufficient
   diversification or uncertainty. Revisit closed hypotheses only with genuinely
   new data or a documented changed premise; cap each research batch in advance.
4. Build walk-forward reports and preserve an untouched chronological holdout.
   Data already inspected in past research cannot be relabelled as untouched.
   New forward data may be needed; tuning after seeing it consumes that holdout.
5. Produce an evidence bundle with data/code/config/cost hashes, all trial identities,
   net returns, benchmark excess, drawdown, turnover, exposure and cost sensitivity.
   Account for overlapping/correlated observations rather than manufacturing sample
   size by splitting one trade into many rows.

Non-negotiable K-60: DSR > 0.95 against the full registry, positive both halves,
at least 100 observations, net of measured costs. Additionally require the
predeclared untouched evaluation and acceptable risk/execution robustness. Generate
the qualification artifact from the reviewed run, not by manually writing a pass flag.

Stop rule: if no candidate passes at the actual budget, keep observation/research
mode and report that outcome. More engineering, capital or a product change must
not be presented as proof of an edge. No open-ended automated strategy search.

## P4 — Turn a qualified specification into an autonomous paper system

- Reuse the existing strategy abstraction where suitable, with explicit NSE session
  and portfolio sizing adapters. Do not route NSE symbols through Delta execution.
- Implement completed-bar scheduling, warmup, deterministic signal IDs, target
  holdings → order intents, exits, rebalance timing and cash/exposure constraints.
- Run the identical strategy code/config in backtest, replay and paper. Compare
  signals and intended holdings before attributing differences to execution.
- Add continuous equity/risk monitoring, P&L attribution, reject reasons and
  broker-independent paper accounting. Restart must not generate duplicate signals.

Acceptance: no unexplained replay/accounting divergence and successful recovery
drills. Predeclare a forward-observation period and required independent signal
opportunities appropriate to the strategy; do not count a fixed number of days
as proof of profitability or tune on the evaluation period.

## P5 — Complete production execution while live stays disabled

- Integrate broker order updates with REST reconciliation and trade-ID deduplication.
  Reconcile holdings, positions, pending orders, fills and cash on startup/reconnect;
  explicitly distinguish external/manual positions from strategy-owned positions.
- Complete create/cancel/modify/expire/partial-fill/exit transitions, price protection,
  reserved cash and quantities, bounded queues and broker-wide rate limiting.
- Persist kill-switch/risk state. Halt entries on discrepancies; preserve a separately
  validated risk-reduction path. Unknown submission remains unknown until resolved;
  never infer that an absent order is permission to send it again.
- Add tests at every crash boundary: before send, after acceptance before local save,
  partial fill during cancellation, duplicate/late events, restart and token expiry.
- Add supervision, health alerts, backups and a minimal FYERS operations view;
  refactor the old crypto dashboard only where useful for these operations.
- Verify the registered outbound IP and current daily-authentication/order rules
  against [FYERS requirements](https://support.fyers.in/portal/en/kb/articles/what-are-the-new-sebi-rules-for-retail-algo-trading-from-april-01-2026).

Acceptance: no unexplained broker/local discrepancy, no duplicate economic fills,
all failure drills pass, and release authorization cannot bypass qualification/risk.

## P6 — Pilot and maintain an edge

Agree a small loss budget and exposure limit before enabling real orders. Compare
actual fills and charges with the frozen model, reconcile daily and stop entries
on predefined cost, drawdown or operational breaches. Short-term profits alone
do not justify scaling. Scale only after a predeclared sample meets its criteria.

Add drift monitoring and periodic requalification. Changes to signals, parameters
or execution assumptions create a new version and a recorded research trial where
evaluated. Automatic parameter learning remains off until this process is reliable.

## Immediate implementation batch

1. Completed for paper: exit-vs-entry permissions, continuous mark-to-market and durable loss baseline.
2. P1: daily feed-quality report and deterministic replay; begin supervised session collection.
3. P2: connect FYERS costs to the existing NSE backtest and produce a feasibility report.
4. P0/P3: strengthen and automatically generate reproducible qualification evidence.

Track completion against this plan in `AUTOMATED_SYSTEM_REFERENCE.md`. Record actual
findings in `KNOWLEDGE.md` and experiments in `RESEARCH_LOG.md`; planned features
must not become facts until implemented and verified.
