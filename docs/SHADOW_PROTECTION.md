# Shadow entry protection (Phase 3)

New shadow intents may carry explicit `stop_loss`, `take_profit`,
`protection_required`, strategy version, decision and regime metadata. At the
PaperBroker fill boundary, the actual simulated fill price is finalised first;
the immutable `position_protection` row is then written in the same SQLite
transaction as the fill and position update.

For long entries, protection is valid only when `stop_loss < fill_price` and,
when present, `take_profit > fill_price`. Missing protection on an intent that
sets `protection_required=true` rejects the entry before any fill. Intents that
explicitly leave protection optional remain compatible with legacy fixtures;
such positions have no protection row and must be treated as
`legacy_unprotected` until a later reviewed policy addresses them. No stop or
target is monitored or executed in this phase.

Risk is persisted from the actual fill:

```text
risk_per_share = fill_price - stop_loss_price
initial_risk_rupees = risk_per_share × filled_quantity
initial_risk_percent = initial_risk_rupees / equity_at_entry × 100
capital_committed = fill_price × filled_quantity
```

There is one protection row per accepted entry fill. Its unique `intent_id`
and deterministic protection ID make replay/restart idempotent. Historical
fills and positions are not backfilled or rewritten. The dashboard API exposes
active rows under `sections.position_protection`.

Inspect active protections on the VM:

```bash
sqlite3 /home/shivank/AlgoTrader/data/fyers/runtime.sqlite3 \
  "SELECT symbol,strategy_family,quantity,entry_fill_price,stop_loss_price,take_profit_price,initial_risk_rupees,status FROM position_protection WHERE status='ACTIVE';"
```

The qualified-paper path and live broker gateway remain unchanged; live
execution is disabled.
