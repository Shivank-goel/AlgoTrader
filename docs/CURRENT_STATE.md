# Current state

Updated: 2026-09-20.

## Implemented utilities

- Historical crypto/NSE backtests, point-in-time bhavcopy ingestion, fee models,
  statistics and the protected 206-trial legacy history.
- Experiment specifications, campaign budgets, immutable results, validation/critic
  reports, explicit order states and broker-independent position comparison.
- FYERS bounded session exports, event replay, executable-quote diagnostics, shared
  fill estimates, latency metrics, T+1 obligation tracking, verified backups and
  fail-closed readiness reports. Completed daily-bar artifacts, FIFO tax lots,
  versioned tax/infrastructure scenarios and ledger reconciliation are included.
  The reviewed exchange calendar is local and must be replaced annually.
- Daily FYERS data now has a reproducible `DATA_READY` gate: immutable artifact
  hashes, universe/benchmark alignment, per-symbol coverage, corrupt-artifact
  detection and the 253-bar regime warm-up are exposed in the CLI, dashboard and
  strategy-lab state before selector evaluation.

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
- The regime strategy lab makes deterministic after-close decisions from immutable
  completed bars and NIFTY 50 history. It records NSE-session entry/exit dates,
  observed open/close prices, costs, benchmark excess and explicit missing outcomes.
  Candidate identities and data hashes remain immutable.
- A hash-bound 20-stock liquid NIFTY 50 forward snapshot feeds a lagged broad-market
  regime gate and three predeclared long-only families: 6/12-month momentum,
  Donchian breakout and residual reversal. Frozen evidence selects qualified
  families only; ties, weak/unsafe regimes and all current unqualified evidence
  resolve to cash. Whole-share targets cap at five ₹2,000 positions.
- The registered `csv_nse_regime` research adapter applies next-bar execution,
  FYERS delivery fees, configurable spread stress, whole shares, chronological
  outputs and equal-weight benchmark returns through the immutable experiment flow.
- `bhavcopy_nse_regime` adds ISIN-keyed point-in-time liquidity membership for
  historical research. Sealed holdout access is one-time and later tuning is blocked.
- Disabled FYERS order transport with throttling, cancel/modify audit, trade-ID
  deduplication, monotonic order reconciliation and fail-closed account reconciliation.
- FYERS dashboard with recorder/account/quote/accounting/position/order/settlement/
  evidence/alert views; authenticated recorder, backup and halt controls.
- Persisted session-aware recorder supervision handles weekends, reviewed NSE
  holidays, start/stop times, manual stops and bounded crash restarts. Broker market
  status remains the final fail-closed entry gate. Retained verified backups,
  restore drills, strict preflight, external secret storage, an idempotent Azure
  installer and systemd health timers are also present. Expired daily credentials
  enter `AUTH_REQUIRED` without an automatic restart loop.

## External verification

Prior checks verified FYERS login/profile, REST quotes/history, instrument lookup
and short stream recordings. Recorded quotes were one-sided; usable execution
coverage and a representative market-hours soak have not been established.
No live order has been placed; no strategy qualifies at ₹10,000.

## Current phase and priorities

Current phase: external session evidence, accounting parity and strategy qualification.

1. Collect a representative market-hours session export and feed-quality evidence.
2. Import actual FYERS charges and verify modelled fees/taxes and accounting parity.
3. Collect forward regime/strategy observations and promote none until registered
   historical/OOS evidence passes the unchanged qualification gate.
4. Deploy Azure Monitor email/VM backup and externally validate reconciliation fields.
5. Qualify a strategy before extended forward shadow and reviewed live release.

Remaining blockers: strategy evidence, representative executable quotes,
contract-note costs, production release controls and operational recovery drills.
Broader lint/type cleanup and external alert delivery also remain.

## Verification and risks

Current batch: 561 tests passed in aggregate. The sandbox-safe full run passed 555
with the six OAuth-login tests excluded; the OAuth and gateway group passed 19/19.
Focused changed-path Ruff,
whitespace and dependency checks passed. The 206-row historical trial file retained
its SHA-256 hash. No real experiment, market session or broker mutation was run.

Unresolved risks: unmeasured spreads/latency, unverified tax assumptions, externally
unverified corporate-action source coverage, ambiguous broker state and stale data. Historical reports stay immutable; a report
with a changed trial snapshot is stale, not newly qualified. Live stays disabled.
