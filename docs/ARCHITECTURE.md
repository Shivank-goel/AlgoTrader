# Architecture and migration plan

Updated 2026-09-18. This document separates implemented behavior from the target.
FYERS is the NSE broker; Delta remains a separate crypto runtime. Dhan is legacy.
No candidate is approved for deployment. Engineering completion cannot establish edge.

## Current boundaries

- `main.py run` / `src/core/engine.py`: Delta orchestration, strategy selection,
  risk, execution and portfolio events. Trading defaults to paper.
- `src/backtest/`: historical research simulators, costs and statistics.
- `src/fyers/`: feed recording, explicit paper intents, accounting, qualification
  and disabled order transport. `src/shadow/continuous.py` is an observation-only
  forward candidate lab; it cannot create intents or call a broker.
- `src/execution/fyers.py`: read-only REST; `fyers_login.py`: explicit user login.
- `docs/KNOWLEDGE.md`, `docs/RESEARCH_LOG.md`, `data/trials.json`: existing research
  evidence. Never relabel historical Dhan results as FYERS results.

## Target dependencies

Market data → strategy → signal → deterministic risk → order intent → execution
→ broker/simulator → fill → ledger → reconciliation.

Strategies cannot call brokers. Research and execution share deterministic
decision/accounting contracts; transport stays broker-specific. Preserve both
single-symbol and portfolio strategy interfaces, with adapters rather than a rewrite.
No microservices or large directory migration is required.

Research: immutable specification → bounded experiment → backtest → validation
→ robustness/OOS → reviewed shadow → qualification → manual deployment.
Lifecycle: RESEARCH → CANDIDATE → VALIDATION → SHADOW → QUALIFIED → LIVE → RETIRED.
These lifecycle and shared-execution contracts are targets, not yet implemented.

K-60 remains mandatory before strategy paper/shadow deployment. Final qualification
adds forward and operational evidence. Synthetic engineering replay is not a
deployment or a claim that a strategy qualifies. Agents never approve live trading.

## Implementation phases

| Phase | Deliverable | Completion gate |
|---|---|---|
| 1 | Baseline, CI, research-history safety, dashboard authorization, boundary contracts | Offline regression suite and fail-closed boundary tests |
| 2 | Rich experiment registry and data/code/environment manifests | Preserve every legacy trial; reproduce representative experiments |
| 3 | Backtest correctness and shared economics | Timing, leakage, accounting and fee-parity fixtures pass |
| 4 | OOS, walk-forward, robustness and critic reports | Frozen specifications produce auditable results |
| 5 | FYERS data quality and replay | Missing intervals explicit; deterministic replay |
| 6 | Bounded, isolated research automation | No credentials, live access, self-approval or gate mutation |
| 7 | Shared intent/risk/shadow ledger | Decision parity across backtest, replay and shadow |
| 8 | Order lifecycle and FYERS updates | Partial-fill, timeout, cancellation and restart drills pass |
| 9 | Position/cash/order reconciliation | Divergence blocks entries; validated reductions remain available |
| 10 | End-to-end shadow and observability | Traceable decisions; recovery and backup drills pass |
| 11 | Extended qualification | K-60 and predeclared forward/cost/operations criteria |
| 12 | Manually authorized small live pilot | Accepted loss budget and all deployment controls active |

Data collection can proceed alongside research infrastructure. Later phases remain
blocked from live activation until evidence and explicit approval exist.

## Implemented first batch

Research integrity update (2026-09-19): experiment transitions, atomic trial
publication, explicit interruption recovery and hash-bound report snapshots are
now implemented. Schema version 2 preserves existing rows and audits inconsistencies.
See [RESEARCH_PROCESS.md](RESEARCH_PROCESS.md) for commands and compatibility;
[CURRENT_STATE.md](CURRENT_STATE.md) is the current status. The original Phase 1
verification below is historical, not the latest test result.

- Trial registry rejects unreadable/malformed records and mismatched counts.
- Cooperating writers lock, reload and atomically append without losing concurrent
  additions. Existing row values are preserved rather than re-rounded.
- A loaded registry refuses deletion or changes to its previously observed history.
- New records reject nonfinite metrics and invalid observation counts.
- Dashboard writes require bearer authorization; absent/short tokens disable writes.
- Offline CI configuration runs dependency checks, critical lint and pytest.

This is only part of Phase 1. Shared domain contracts, comprehensive deployment
authorization, rich immutable experiment storage and the remaining phases are pending.
The JSON registry is not tamper-proof: a new process cannot detect an externally
rewritten but valid historical file without a separate trusted manifest/backup.
Reader statistics are a snapshot; reopen the registry for a fresh research evaluation.

Verification for this batch: full suite 446 passed (3941 existing warnings).
After adding the final disk-replacement failure regression and directory fsync,
the focused registry suite passed all 15 tests. Critical Ruff checks, JavaScript
syntax validation, `pip check` and `git diff --check` also passed. The historical
registry was read-only validated at 206 trials; no new research trial was added.
The hosted CI workflow has been configured, not yet executed by GitHub.

## Dashboard security and migration

Set `DASHBOARD_CONTROL_TOKEN` privately to a random value of at least 32 characters,
then restart the dashboard. The existing browser buttons prompt for it and retain
it only in page memory. API callers send `Authorization: Bearer <token>` for every
POST/PUT/PATCH/DELETE. Missing/wrong authorization returns `{"error": "..."}`;
unconfigured controls return HTTP 503. Do not put the token in URLs or logs.

Read-only endpoints remain unauthenticated. Keep the default localhost binding;
remote access needs authenticated TLS access in front of the whole dashboard.
Control authorization is not strategy qualification or permission to enable live.
Stopping this dashboard is not a substitute for a broker emergency procedure.

## Development checks

Use Python 3.12 and install `requirements-dev.txt`. Run:

```sh
python -m pip check
python -m ruff check --select E9,F63,F7,F82 src tests main.py
python -m pytest -q --disable-warnings
```

CI has no broker secrets. Tests use mocks and local fixtures; the OAuth callback
test uses a loopback server. Production dependency locking and broader lint/type
cleanup remain pending; the current requirements are not a complete lockfile.
