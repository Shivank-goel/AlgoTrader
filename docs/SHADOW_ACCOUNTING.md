# Shadow account (Phase 2)

The FYERS shadow path uses one persistent `shadow_simulations` row per active
campaign. The existing `PaperBroker` remains the only source of cash, fills,
fees, slippage, positions and realized P&L; this layer does not duplicate
accounting or reset the account on restart.

On first opening a journal, the broker registers one `ACTIVE` simulation with
the configured ₹10,000 starting capital and records a `SIMULATION_START`
checkpoint. Subsequent dashboard, recorder and VM restarts reuse that row.
Historical fills and trials are not rewritten; a legacy journal receives a
new prospective simulation registration only.

For a complete quote mark, the account formula is:

```text
current_equity = available_cash + Σ(quantity × validated bid)
unrealized_pnl = marked market value − open-position cost basis
```

Realized P&L and fees retain the existing FIFO/tax-lot and FYERS cost-model
semantics. Equity checkpoints are append-only and idempotent for
`SIMULATION_START`, `FILL`, and `MARK_TO_MARKET` source events. Short exposure
is zero because this remains long-only delivery simulation.

Inspect the active simulation and latest checkpoint on the VM:

```bash
sqlite3 /home/shivank/AlgoTrader/data/fyers/runtime.sqlite3 \
  "SELECT simulation_id,status,initial_capital,started_at FROM shadow_simulations WHERE status='ACTIVE';"
sqlite3 /home/shivank/AlgoTrader/data/fyers/runtime.sqlite3 \
  "SELECT event_type,timestamp,current_equity,realized_pnl,unrealized_pnl,fees,drawdown FROM equity_checkpoints ORDER BY timestamp DESC LIMIT 1;"
```

The read-only dashboard API also exposes `sections.simulation` and
`sections.equity`. Live execution remains disabled and no FYERS order gateway
is reachable from this path.
