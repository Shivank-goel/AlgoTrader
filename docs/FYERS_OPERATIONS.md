# FYERS operating guide

Updated 2026-09-19. Scope: NSE cash equity, observation and long-only delivery
paper infrastructure. This is not a live-ready or profitable trading system.

## Commands

Run from the repository with `.venv312/bin/python`:

```bash
.venv312/bin/python -m src.execution.fyers_login
.venv312/bin/python main.py fyers status
.venv312/bin/python main.py fyers preflight --json --strict
.venv312/bin/python main.py fyers sync-daily --days 550
.venv312/bin/python main.py fyers finalize-session --date YYYY-MM-DD
.venv312/bin/python main.py fyers record --seconds 3600
.venv312/bin/python main.py fyers export-session --start UNIX --end UNIX --output session.json
.venv312/bin/python main.py fyers readiness
.venv312/bin/python main.py fyers backup
.venv312/bin/python main.py fyers restore-drill --backup BACKUP --output-directory NEW_DIR
.venv312/bin/python main.py fyers costs --notional 10000
.venv312/bin/python main.py fyers reconcile-ledger normalized-ledger.csv
.venv312/bin/python main.py fyers halt "operator stop"
```

The dashboard is FYERS/NSE-only and defaults to `http://127.0.0.1:8000`. It
shows recorder/account/market health, executable quotes, paper accounting,
positions and reconciliation, orders/intents, settlements, qualification and
operational alerts. Authenticated controls can start/stop recording, create a
verified backup and persistently halt new entries. There is no order or live-enable route.

The dashboard also runs the observation-only strategy lab configured in
`config/fyers_strategy_lab.yaml`. It uses the hash-bound 20-stock snapshot in
`config/nse_forward_universe.yaml`, requires at least ten fresh two-sided books,
and records at most one immutable regime decision per session. Trending markets
admit momentum/breakout research; ranging markets admit residual-reversal research;
volatile, falling, unknown and low-confidence states remain in cash. No family is
currently qualified. Decisions use immutable completed daily-bar artifacts and
NIFTY 50 history, never an intraday quote appended to stale daily files. The
scheduler rechecks hash-bound qualification before creating paper intents; the
recorder consumes them into the paper ledger only. This path never submits orders.

Research the families through `research run --adapter bhavcopy_nse_regime` for
historical point-in-time ISIN membership. Use `csv_nse_regime` only for an explicitly
registered fixed-universe diagnostic. Registered
CSV artifacts, dates, family, rebalance cadence, spread stress and costs remain
part of the frozen experiment. Never edit a candidate or universe in place; create
a new versioned ID.

Log in through the printed browser URL when the token expires. No unattended
2FA bypass is implemented. `record` finishes after the requested duration;
Ctrl-C also shuts down the isolated SDK process. It sends no trading orders.
Do not run two recorders against the same database: an exclusive lock enforces this.

With `auto_record: true`, the dashboard supervisor starts the recorder at the
configured `session_start`, stops it after `session_end`, and stays idle on weekends
and dates in `config/nse_holidays.yaml`. Its state and a same-day manual stop survive
dashboard restarts; a manual stop clears on the next date. Unexpected exits use a
bounded restart budget and backoff. The FYERS broker market-status response is still
required before paper fills, so an unexpected exchange closure fails closed.

After the configured delay, the supervisor runs bounded post-close finalization:
official NSE bhavcopy update, immutable FYERS daily bars, a bounded session/quality
export and a verified SQLite backup. Failures use capped retries and are visible on
the dashboard. Observation due dates use reviewed NSE sessions.

Replace and review `config/nse_holidays.yaml` before each calendar year. A missing,
invalid or wrong-year file disables automatic recording and reports
`calendar_unavailable`; it never assumes that the exchange is open. Special sessions
such as Muhurat trading require an explicit reviewed schedule change.

Settings: `config/fyers_runtime.yaml`; REST endpoint: `config/fyers.yaml`;
published tariff: `config/fyers_costs.yaml`; credentials: gitignored `.env`.
SQLite WAL journal: `data/fyers/runtime.sqlite3`. The backup command uses SQLite's
backup API, validates integrity and never overwrites an existing destination.

## What works

- REST account, quotes and bounded history requests; JSON and candle validation.
- Exact daily NSE master lookup for ISIN, lot/tick size, trading-session metadata.
- Official SDK streaming with resubscription on reconnect; isolated subprocess
  avoids SDK thread shutdown hangs. Credentials never appear in process arguments.
- Bounded event queue; overflow halts paper execution. Exchange and receive
  timestamps are retained. Missing/one-sided/crossed/old books are not fill prices.
- Read-only account snapshots and broker market-session checks. These monitor
  account access; they are **not** full broker-to-local position reconciliation.
- SQLite paper account with atomic cash, positions, fees and fills. FIFO tax lots,
  holding periods, paper-order state and settlement obligations are durable. Stable intent
  IDs survive restart. Buys use ask, sells bid, plus adverse slippage/tick rounding.
  Full fills require sufficient top-of-book size; the shared estimator exposes
  partial outcomes but the paper path never invents missing liquidity.
- Paper capital/notional/loss limits, persistent halt, no cash delivery shorts,
  once-per-ISIN/day DP charges. It conservatively charges delivery costs even for
  same-day closes; settlement/T1 and intraday conversion are not modelled.
- Paper sells are restricted to existing holdings and remain possible after a
  halt or expired qualification, above the entry notional cap, or when unrelated
  holdings have stale quotes. The sold instrument still needs a fresh executable
  book, sufficient displayed size and an open session. Mixed intent batches reject
  unqualified buys without discarding valid reductions. Held symbols remain in
  the recording universe even if removed from the entry configuration.
- Equity monitoring runs independently of orders. Daily baselines carry the last
  complete prior-day valuation across restarts, so overnight gaps are not erased.
  Missing marks block entries and expose an incomplete valuation rather than fake
  equity. Realized/unrealized P&L and fees persist; old journals recover these from
  committed fills. Exit fees may create a cash debit, which is recorded rather
  than preventing risk reduction. There is no automatic liquidation on a halt.
- Disabled `FyersOrderGateway`: limit-only transport, durable pre-submit intent,
  no blind retry, throttling, cancel/modify audit, trade-ID deduplication,
  ambiguous-order adoption and monotonic partial-fill tracking.
  Its `enable` parameter is for future integration/testing, not deployment approval.
  No CLI/runtime path enables it or places real orders.

## Strategy qualification

There is no approved candidate. Do not create fake evidence to unblock paper mode.
`main.py fyers paper intents.json --seconds 60` blocks new entries until evidence
passes; validated reductions of existing paper holdings do not require qualification.
The evidence file is `data/fyers/qualification.json` and must contain:

```text
broker: "fyers"
strategy: exact strategy identity used in intents
trial_name: existing entry in the full trials registry
registry_sha256: SHA-256 of the complete trials JSON
costs_sha256: SHA-256 of config/fyers_costs.yaml
data_path: research data artifact path
data_sha256: SHA-256 of that data artifact
code_artifacts: non-empty mapping of reviewed repository code paths to SHA-256
config_artifacts: non-empty mapping of reviewed config paths to SHA-256
reviewed_net_costs_and_data: true only after reviewing fees, spread and leakage
net_returns: chronological net per-period returns from the registered run
forward_evidence: accepted review, at least 20 evaluated observations and selector hash
```

The checker recomputes DSR against all registry trials, checks sample count and
positive halves, matches trial Sharpe/count and binds code/config/data/cost/registry hashes.
This is a reproducibility gate, not proof the returns or review assertion are honest.
Existing strategy specifications and backtest tools must produce/review evidence;
the infrastructure does not invent a strategy or launch parameter sweeps.

Intents are a JSON array with `intent_id`, `strategy`, `symbol`, `side` (`BUY` or
`SELL`), positive integer `quantity`, and timezone-aware `created_at`. The paper
command remains an explicit test harness. Qualified selector rebalances and exits
are dispatched automatically by the continuous recorder.
Rejected intents are journalled and not retried; expired intents need a new signal.

## Remaining external evidence and cloud work

1. Collect representative market-hours spreads, feed gaps and restart/reconnect
   behavior. The short external test only proved connection and recording.
2. Import a normalized FYERS ledger/charge export and validate charges; qualify a strategy on clean data and
   the full registry (206 trials observed, no new trials in this integration).
3. Accumulate at least 20 non-overlapping portfolio observations spanning six
   months and run sealed-holdout research. No current strategy is qualified.
4. Deploy Azure Monitor email rules and Azure VM/disk backup, then prove alert and
   isolated restore delivery. Broker order submission remains absent.
5. Review evidence before introducing a live enable path. Current mode is observation.

Azure deployment units and the SSH-tunnel procedure are documented in
`deploy/azure/README.md`. The dashboard binds to VM localhost; do not expose port
8000 through the Azure NSG.
Delta now defaults to paper. The unused ccxt requirement was replaced with the
official FYERS SDK. SDK 3.1.18 pins older HTTP dependencies; `pip check` is clean,
but upgrading that SDK/dependency set requires a deliberate compatibility review.

## Verification (2026-09-20)

The session calendar and supervisor have focused coverage for regular sessions,
weekends, holidays, next-open reporting, wrong-year failure and manual-stop behavior.
The complete-suite count is recorded in `docs/CURRENT_STATE.md`. Existing deprecation
warnings remain; this is not a warning-free build. `pip check` reports no broken requirements.
Two bounded external recordings (15 and 10 seconds) each established one stream
connection, recorded two ticks and shut down cleanly. Both recorded books were
one-sided and therefore rejected as executable quotes. No order was submitted.

## Source provenance

- [FYERS tariff](https://fyers.in/charges-list): brokerage, taxes and DP charge.
- [DP frequency](https://support.fyers.in/portal/en/kb/articles/how-are-dp-charges-levied-in-fyers): once per ISIN/day.
- [Official SDK examples](https://github.com/FyersDev/fyers-api-sample-code): streaming callbacks and REST usage.
- [Official symbol utility](https://github.com/FyersDev/fyers-skills/blob/master/skills/fyers-trading/scripts/fyers_symbols.py): master format.

Historical Dhan backtests remain unchanged and must not be relabelled as FYERS results.
