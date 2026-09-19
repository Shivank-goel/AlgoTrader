# Current state

Updated: 2026-09-19.

## Implemented utilities

- Historical crypto/NSE backtests, point-in-time bhavcopy ingestion, fee models,
  statistics and the protected 206-trial legacy history.
- Experiment specifications, campaign budgets, immutable results, validation/critic
  reports, explicit order states and broker-independent position comparison.
- FYERS bounded session exports, event replay, executable-quote diagnostics, shared
  fill estimates, latency metrics, T+1 obligation tracking, verified backups and
  fail-closed readiness reports. Exchange-holiday ingestion and reviewed autonomous
  strategy generation remain incomplete.

## Integrated workflows

- Delta event-driven engine, risk, paper execution, portfolio, reconciliation and dashboard.
- FYERS OAuth/REST, instrument lookup, SDK recording, explicit gated paper intents,
  persistent paper accounting, shadow replay and disabled limit-order transport.
- Research transitions, atomic identity-based trial publication, interruption
  resolution, recovery and hash-bound report freshness checks.
- Allowlisted registered CSV portfolio backtests through the research CLI;
  executable lookahead/warm-up checks, code/config/data/cost/trial-bound
  qualification, qualified target scheduling and forward-shadow metrics.
- Disabled FYERS order transport with throttling, cancel/modify audit, trade-ID
  deduplication, monotonic order reconciliation and fail-closed account reconciliation.
- Dashboard FYERS readiness/authenticated halt, direct dependency lock and isolated
  verified backup restore drills.
- Dashboard write authorization and configured offline CI.

## External verification

Prior checks verified FYERS login/profile, REST quotes/history, instrument lookup
and short stream recordings. Recorded quotes were one-sided; usable execution
coverage and a representative market-hours soak have not been established.
No live order has been placed; no strategy qualifies at ₹10,000.

## Current phase and priorities

Current phase: external session evidence, accounting parity and strategy qualification.

1. Collect a representative market-hours session export and feed-quality evidence.
2. Verify FYERS fees against contract notes; add settlement and historical accounting parity.
3. Add a stronger frozen-holdout boundary and connect a reviewed NSE strategy producer.
4. Complete streaming order updates and externally validate account reconciliation fields.
5. Qualify a strategy before extended forward shadow and reviewed live release.

Remaining blockers: strategy evidence, representative executable quotes,
contract-note costs, production release controls and operational recovery drills.
Broader lint/type cleanup and external alert delivery also remain.

## Verification and risks

Current batch: 534 tests passed in aggregate. The sandbox-safe full run passed 528
with the OAuth module excluded; the OAuth and gateway group passed 19/19 with
loopback permission. Critical Ruff,
whitespace and dependency checks passed. The 206-row historical trial file retained
its SHA-256 hash. No real experiment, market session or broker mutation was run.

Unresolved risks: optimistic costs/fills, incomplete research/execution integration,
ambiguous broker state and stale data. Historical reports stay immutable; a report
with a changed trial snapshot is stale, not newly qualified. Live stays disabled.
