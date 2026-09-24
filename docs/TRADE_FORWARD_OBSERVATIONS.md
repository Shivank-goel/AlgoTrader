# Trade-level forward observations (Phase 7)

One completed economic shadow trade—long entry plus exit, or short entry plus
cover—is exactly one row in `trade_forward_observations`. Ticks, bars, marks,
signals and repeated threshold checks are not observations.

The record is created only after an exit fill is committed. It links the
immutable protection, entry fill and exit trigger/fill, preserves actual
prices, fees, gross/net P&L, holding duration, account checkpoint context and
strategy/config/cost provenance. A unique `trade_id` makes restart/replay
idempotent. Losing trades remain valid evidence.

Live monitor records use `evidence_source=LIVE_FORWARD`; callers processing a
historical replay must explicitly pass `REPLAY` or `TEST_FIXTURE`. Such rows
remain distinguishable and are not silently counted as live-forward evidence.
New rows begin `GENERATED` with no human acceptance. Review metadata is kept
separate from immutable economics. No qualification artifact is changed and
no strategy is automatically qualified.

The existing `regime_forward_observations_v2` table remains a family/regime
holding-period research series, while `strategy_observations` remains the
candidate-level forward lab series. Trade observations are a separate
completed-trade evidence channel. Benchmark fields are explicitly NULL in
this phase because exact time-aligned live NIFTY prices are not available;
daily prices are never substituted for intraday intervals.

Inspect the VM database:

```bash
sqlite3 /home/shivank/AlgoTrader/data/fyers/runtime.sqlite3 \
  "SELECT trade_id,evidence_source,strategy_family,side,entry_time,exit_time,exit_reason,net_pnl,net_return,review_status FROM trade_forward_observations ORDER BY exit_time DESC;"
sqlite3 /home/shivank/AlgoTrader/data/fyers/runtime.sqlite3 \
  "SELECT strategy_family,strategy_version,evidence_source,COUNT(*),MIN(exit_time),MAX(exit_time),SUM(net_pnl) FROM trade_forward_observations GROUP BY strategy_family,strategy_version,evidence_source;"
```

The existing qualification gates, sealed holdout, DSR/K-60 inputs and live
execution boundary are unchanged. Candidates remain empty by default.
