# Time-aligned NIFTY50 benchmark evidence (Phase 9)

The canonical benchmark is `NSE:NIFTY50-INDEX`. It is subscribed as a
data-only FYERS stream symbol alongside the configured equity universe; it is
never passed to instrument resolution, stock ranking, sizing, or order intent
generation.

Benchmark ticks use the existing `tick` event journal with exchange/receive
metadata and are not stored in the daily-bar regime dataset. When a protected
trade closes, the evidence recorder selects the latest benchmark tick whose
received timestamp is **at or before** the entry/exit fill timestamp. A future
tick is never selected. The configured `benchmark_max_age_seconds` policy
(default 30 seconds) marks stale or missing sides incomplete.

Benchmark evidence is stored separately in immutable
`trade_benchmark_evidence` rows linked by observation ID, so Phase 7 economic
observations are never silently rewritten. Statuses include `COMPLETE`,
`MISSING_ENTRY`, `MISSING_EXIT`, `STALE_ENTRY`, and `STALE_EXIT`. For complete
pairs:

```text
benchmark_return = exit_benchmark / entry_benchmark - 1
excess_return = strategy_net_return - benchmark_return
```

Long and short strategy returns use the same normal NIFTY return; it is not
sign-inverted for shorts. Historical daily NIFTY data remains the separate
regime/research input. Existing observations without trustworthy intraday
events remain incomplete; no daily-close backfill is performed.

The dashboard exposes latest benchmark health and linked benchmark evidence.
Candidates remain empty, qualification thresholds are unchanged, and live
execution remains disabled.
