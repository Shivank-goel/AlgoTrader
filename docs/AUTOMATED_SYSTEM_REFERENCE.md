# Automated trading system reference

Last updated: 2026-09-18. This is a design reference, not evidence that a strategy is profitable or approved for live use.

## Decision

Build a **FYERS/NSE research-to-execution platform**. None of the current strategies clears the pre-committed pass mark at ₹10,000 (K-47, K-56, K-57, K-60). Account inspection and feed recording can proceed now; strategy paper deployment remains subject to K-60.

The existing Delta engine remains the crypto runtime and an execution reference. FYERS is the selected NSE target (K-02); Dhan is retained as optional legacy code.

## Production shape

```text
NSE/FYERS market feed ─┐
                      ├─> immutable market-data store ─> feature/signal engine
Broker order updates ─┘                                      │
                                                             v
Research registry <─ backtest + walk-forward <─ strategy specification
     │                                                       │
     └──── qualification gate ──> shadow ledger ──> risk gate ──> FYERS gateway
                                                            │         │
                                                        kill switch  │
                                                                    v
                                               order/trade reconciliation
                                               + append-only audit record
```

1. **Research:** versioned data, strategy specification, costs, and trial registry. A candidate is eligible only when it clears K-60.
2. **Shadow execution:** records intended orders, quotes, and expected costs without risk; it validates timing, sizing, feed quality, and paper/live parity.
3. **Risk gate:** is the only component permitted to request an order. It applies capital, exposure, session, stale-data, and durable manual-kill-switch checks.
4. **Broker gateway (transport implemented, disabled):** persists a limit-order intent before submission and reconciles ambiguous submissions without retrying the POST. FYERS order tags are correlation labels, not deduplication guarantees. Production risk/position integration is still required.
5. **Reconciliation/audit:** on startup and after every disconnect, broker positions, order book, and trade book are compared with the local ledger. Any discrepancy stops new entries.

## FYERS and regulatory constraints

- Use the current FYERS Algo Trading App and registered static IP for order access. [FYERS activation guide](https://support.fyers.in/portal/en/kb/articles/how-do-i-activate-the-new-app-for-api-trading-after-april-1-2026)
- Account calls use FYERS v3 and `Authorization: app_id:access_token`. The adapter validates HTTP and API-level failures. [Official endpoint reference](https://github.com/FyersDev/fyers-skills/blob/master/skills/fyers-trading/references/endpoints.md)
- Authentication renewal, rate limiting, order updates and ambiguous-order recovery must be implemented before live execution.
- SEBI's retail API-algo framework applies to all stock brokers from 1 April 2026. Broker/exchange operational controls are part of deployment. [SEBI circular](https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf)

## Deployment gates

| Gate | Required evidence | Result when it fails |
|---|---|---|
| Research | K-60 pass mark, measured costs, no known data defect | Research only |
| Shadow | Stable data feed; intended orders, fills, and local accounting agree | Continue shadow mode |
| Broker readiness | Static IP, token handling, instrument master, rate limit, order-update path | Read-only broker access |
| Paper/small live | Reconciliation, kill switch, max-loss halt, and partial-fill/timeout drills | No scale-up |
| Ongoing | Slippage/cost within its pre-set band and qualification remains valid | Halt and investigate |

## What has been built in this repository

- Implemented: `FyersClient` account/REST data, browser login, daily master resolution, official-SDK streaming, SQLite event journal, fail-closed K-60 evidence checker, long-only delivery paper ledger, published FYERS costs and disabled limit-order transport. See `docs/FYERS_OPERATIONS.md` for limitations.
- Verified externally: profile, quotes/history, two master symbols, streaming connection and read-only account snapshots. Streaming test received two one-sided quotes; usable live execution prices were not established.
- Retained: optional `src.execution.dhan` and its tests; historical NSE costs remain Dhan-specific.
- `main.py fyers` is the NSE entry point; `main.py run` remains the Delta engine and now defaults to paper. The crypto strategy selector is not reused for NSE.

## Implementation sequence

The platform-first architecture and twelve-phase migration are documented in
[ARCHITECTURE.md](ARCHITECTURE.md). Initial safety work now protects trial-registry
appends and dashboard writes; the shared research/execution architecture is still
planned. Neither these changes nor passing tests establish a trading edge.

The prioritized remaining backlog and acceptance criteria are maintained in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Start with correctness and
data/cost evidence; strategy qualification determines when deployment can progress.

P0 paper-risk progress: entry and exit permissions are separated; stream-driven
valuation, durable daily baselines, P&L recovery and overnight-gap halts are implemented.
Qualification provenance hardening and the production release boundary remain next.

1. Done: select FYERS, add credential template, endpoint configuration and read-only account adapter.
2. Implemented: `python -m src.execution.fyers_login` serves the local callback, validates OAuth state, exchanges the code, checks the profile and saves the token in `.env`. Actual account verification requires browser login.
3. Implemented: REST/history, master resolution and streaming recording. Pending: market-hours feed quality/reconnect soak and order-update integration.
4. Implemented: published FYERS costs, account snapshot monitoring and persistent paper ledger. Pending: contract-note validation, qualified candidate, representative paper results and production position reconciliation.
5. Implemented: disabled order transport and mocked timeout/partial-fill/restart tests. Pending: integrated production risk, cancel/modify/exit handling, broker update stream, rate limiting and deployment drills. No live enable command exists.

## Non-negotiable operating rules

- No credentials in the repository, logs, dashboard, research artifacts, or exception messages.
- No live order without a current broker position/order reconciliation.
- No blind retry after an unknown submit result: reconcile broker orders/trades; halt if acceptance remains unresolved.
- A broker rejection, rate limit, stale feed, unexplained fill, or accounting mismatch blocks new entries and emits an alert.
- Do not optimise on shadow/live outcomes without recording the trial in `data/trials.json`; that would invalidate the multiple-testing defence.
