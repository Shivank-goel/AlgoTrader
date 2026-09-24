# Shadow protection exits (Phase 4)

`ShadowProtectionMonitor` runs on each validated FYERS quote in
`runtime.observe()`, before any bar/strategy cycle. It only reads active long
`position_protection` rows and only acts on a fresh, valid bid.

For a long position:

```text
bid <= stop_loss_price  -> STOP_LOSS
bid >= take_profit_price -> TAKE_PROFIT
```

Crossing is used; equality naturally triggers. The observed bid is retained as
the trigger and executable price. PaperBroker's existing SELL path then
applies adverse slippage, FYERS fees, FIFO tax lots, settlement and P&L. A
stop-level fill is never fabricated during a gap.

Each protection has one deterministic `position_exit_triggers` row and one
deterministic exit intent (`exit:<protection_id>:intent`). The trigger is
persisted before execution. A restart after triggering reuses that row and
PaperBroker's fill idempotency, so it cannot create a second fill. Successful
execution marks the protection `CLOSED` and emits a `shadow_exit` event. No
stop/target is monitored for legacy positions without protection records.

Stale quotes, disconnected feeds and closed-market state do not execute exits;
the limitation is surfaced through existing feed-health events. This phase
does not add trailing stops, session liquidation, shorting, or real FYERS
orders. The dashboard API exposes `position_protection` and `exit_triggers`.

Inspect the VM journal:

```bash
sqlite3 /home/shivank/AlgoTrader/data/fyers/runtime.sqlite3 \
  "SELECT symbol,status,stop_loss_price,take_profit_price FROM position_protection;"
sqlite3 /home/shivank/AlgoTrader/data/fyers/runtime.sqlite3 \
  "SELECT trigger_type,trigger_price,executable_price,status,exit_fill_id FROM position_exit_triggers ORDER BY triggered_at DESC;"
sqlite3 /home/shivank/AlgoTrader/data/fyers/runtime.sqlite3 \
  "SELECT intent_id,symbol,side,quantity,price,fee FROM fills ORDER BY timestamp DESC;"
sqlite3 /home/shivank/AlgoTrader/data/fyers/runtime.sqlite3 \
  "SELECT event_type,timestamp,current_equity,realized_pnl,fees FROM equity_checkpoints ORDER BY timestamp DESC LIMIT 10;"
```
