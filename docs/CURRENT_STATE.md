# Current state

Updated: 2026-09-20.

## Implemented utilities

- Historical crypto/NSE backtests, point-in-time bhavcopy ingestion, fee models,
  statistics and the protected 206-trial legacy history.
- Experiment specifications, campaign budgets, immutable results, validation/critic
  reports, explicit order states and broker-independent position comparison.
- FYERS bounded session exports, event replay, executable-quote diagnostics, shared
  fill estimates, latency metrics, T+1 obligation tracking, verified backups and
  fail-closed readiness reports. The reviewed exchange calendar is local and must be
  replaced annually; autonomous strategy generation remains incomplete.

## Integrated workflows

- Delta event-driven engine, risk, paper execution, portfolio and reconciliation remain
  separate legacy functionality; the primary dashboard is now FYERS/NSE-only.
- FYERS OAuth/REST, instrument lookup, SDK recording, explicit gated paper intents,
  persistent paper accounting, shadow replay and disabled limit-order transport.
- Research transitions, atomic identity-based trial publication, interruption
  resolution, recovery and hash-bound report freshness checks.
- Allowlisted registered CSV portfolio backtests through the research CLI;
  executable lookahead/warm-up checks, code/config/data/cost/trial-bound
  qualification, qualified target scheduling and forward-shadow metrics.
- A continuous observation-only FYERS strategy lab pre-registers candidate IDs,
  samples fresh two-sided quotes only while FYERS reports NSE open, records
  non-overlapping forward decisions and evaluates net-of-fee returns against an
  equal-weight benchmark. Candidate parameter changes require a new ID; missed
  evaluation windows expire rather than using late prices. It cannot create intents.
- A hash-bound 20-stock liquid NIFTY 50 forward snapshot feeds a lagged broad-market
  regime gate and three predeclared long-only families: 6/12-month momentum,
  Donchian breakout and residual reversal. Frozen evidence selects qualified
  families only; ties, weak/unsafe regimes and all current unqualified evidence
  resolve to cash. Whole-share targets cap at five ₹2,000 positions.
- The registered `csv_nse_regime` research adapter applies next-bar execution,
  FYERS delivery fees, configurable spread stress, whole shares, chronological
  outputs and equal-weight benchmark returns through the immutable experiment flow.
- Disabled FYERS order transport with throttling, cancel/modify audit, trade-ID
  deduplication, monotonic order reconciliation and fail-closed account reconciliation.
- FYERS dashboard with recorder/account/quote/accounting/position/order/settlement/
  evidence/alert views; authenticated recorder, backup and halt controls.
- Persisted session-aware recorder supervision handles weekends, reviewed NSE
  holidays, start/stop times, manual stops and bounded crash restarts. Broker market
  status remains the final fail-closed entry gate. Retained verified backups,
  restore drills, Azure systemd units and configured offline CI are also present.

## External verification

Prior checks verified FYERS login/profile, REST quotes/history, instrument lookup
and short stream recordings. Recorded quotes were one-sided; usable execution
coverage and a representative market-hours soak have not been established.
No live order has been placed; no strategy qualifies at ₹10,000.

## Current phase and priorities

Current phase: external session evidence, accounting parity and strategy qualification.

1. Collect a representative market-hours session export and feed-quality evidence.
2. Verify FYERS fees against contract notes; add settlement and historical accounting parity.
3. Collect forward regime/strategy observations and promote none until registered
   historical/OOS evidence passes the unchanged qualification gate.
4. Complete streaming order updates and externally validate account reconciliation fields.
5. Qualify a strategy before extended forward shadow and reviewed live release.

Remaining blockers: strategy evidence, representative executable quotes,
contract-note costs, production release controls and operational recovery drills.
Broader lint/type cleanup and external alert delivery also remain.

## Verification and risks

Current batch: 555 tests passed in aggregate. The sandbox-safe full run passed 549
with the OAuth module excluded; the OAuth and gateway group passed 19/19 with
loopback permission. Critical Ruff,
whitespace and dependency checks passed. The 206-row historical trial file retained
its SHA-256 hash. No real experiment, market session or broker mutation was run.

Unresolved risks: optimistic costs/fills, incomplete research/execution integration,
ambiguous broker state and stale data. Historical reports stay immutable; a report
with a changed trial snapshot is stale, not newly qualified. Live stays disabled.
