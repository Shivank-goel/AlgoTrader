# Codex instructions

## Default workflow

1. Read this file, `docs/PROJECT_CONTEXT.md`, and `docs/CURRENT_STATE.md`.
2. Read only the task-specific files below and the smallest relevant code scope.
3. Make the smallest correct change; preserve unrelated user work.
4. Run targeted tests first. Run the full suite only for broad or safety-critical changes.
5. Update `CURRENT_STATE.md` only for a material status change and `DECISIONS.md`
   only for a durable architectural decision.
6. Stop when the requested task is complete.

For a large or ambiguous change, first state scope, likely files, dependencies,
implementation steps, and risks. Do not perform repo-wide rewrites unless requested.

## Project and boundaries

This is an event-driven quantitative research and trading platform. The intended
path is research → validation → shadow → qualification → controlled live deployment.
FYERS is the selected NSE broker; Dhan is legacy/optional. The Delta crypto runtime
is separate. Strategies never call brokers: market data → strategy → signal → risk
→ intent → execution → broker/simulator → fill → ledger → reconciliation.

Key paths: `main.py`, `src/core/`, `src/data/`, `src/strategies/`, `src/backtest/`,
`src/research/`, `src/risk/`, `src/execution/`, `src/fyers/`, `src/shadow/`, `tests/`,
`config/`, and `docs/`.

## Task-specific context

- Research or strategy evidence: `docs/KNOWLEDGE.md`, `docs/RESEARCH_LOG.md`,
  `src/research/`, `src/backtest/statistics.py`, `data/trials.json`.
- Backtests/costs: relevant files in `src/backtest/`, strategy implementation,
  matching tests, and applicable K-facts in `docs/KNOWLEDGE.md`.
- FYERS/data/shadow: `docs/FYERS_OPERATIONS.md`, `src/execution/fyers*.py`,
  `src/fyers/`, `src/data/`, `src/shadow/`, matching tests.
- Delta engine/execution: `src/core/`, `src/execution/exchange.py`,
  `src/execution/order_manager.py`, `src/portfolio/`, `src/risk/`, matching tests.
- Risk/live/reconciliation: `docs/AUTOMATED_SYSTEM_REFERENCE.md`,
  `docs/ARCHITECTURE.md`, `src/risk/`, reconciliation and execution modules/tests.
- Architecture/status/docs: `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, and only
  the specific operational document affected.

Do not read all research history or scan unrelated directories by default.

## Engineering rules

- Python 3.12, type public APIs, validate trust boundaries, use Pydantic v2 for
  domain inputs and enums for domain values. Async is preferred for I/O; the
  SQLite journals are deliberately synchronous.
- Reuse existing helpers. Avoid adjacent refactors, new dependencies, hidden
  global state, duplicated configuration, and hardcoded secrets/limits/URLs.
- Tunables belong in `config/*.yaml`; secrets only in `.env`. Never print or commit
  credentials. Do not expose dashboard controls without authorization.
- Preserve Delta HMAC signing, rate limiting, client-order idempotency, bracket
  behavior, startup reconciliation, and persisted circuit-breaker state.
- Existing dashboard error bodies use `{\"error\": \"message\"}`.

## Research and trading invariants

- Never weaken K-60: deflated Sharpe > 0.95 against the full trial count, positive
  in both halves, at least 100 observations, net of measured costs.
- Register every tested configuration. Failed research is immutable evidence.
  Verify signal/return timing, point-in-time universes, costs, spread/slippage,
  integer sizing, and out-of-sample behavior. Do not claim profitability without evidence.
- Risk is deterministic and runs before submission. Preserve position/exposure/loss
  limits, stale-data checks, circuit breakers, kill switches, and reconciliation.
- Live is disabled by default. No live broker access without an explicit request and
  satisfied prerequisites. Agents cannot place live orders autonomously, weaken risk,
  alter gates after results, approve their own strategy, or promote it to live.
- UNKNOWN order status must be reconciled before retry. Preserve duplicate-order
  protection and separate intended, local, broker, and reconciled positions.
- Test broker I/O with mocks. Validate execution changes in paper/shadow first.

## Commands and context efficiency

Use `.venv312/bin/python`:

```sh
.venv312/bin/python -m pytest <target> -q --disable-warnings
.venv312/bin/python -m ruff check --select E9,F63,F7,F82 src tests main.py
.venv312/bin/python -m pip check
```

Prefer targeted inspection → targeted edit → targeted tests → stop. Do not re-read
large files, repeat existing documentation, regenerate architecture plans for routine
work, or run the full suite for an isolated change unless risk requires it.
