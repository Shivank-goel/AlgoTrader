# Paper-only intraday shorts (Phase 6)

Shorts are represented explicitly with `Intent.position_side="SHORT"` and
`intraday_only=true`; ordinary long SELL exits are unchanged. A short entry is
a SELL at the executable bid with adverse slippage. The simulator reserves
entry notional plus fees from available cash in `short_positions` instead of
treating sale proceeds as reusable cash.

Short cover is a BUY at the executable ask with adverse slippage. For a short
lot, gross P&L is `(entry_price - cover_price) × quantity`; entry and cover
fees are deducted before the shared PaperBroker realized P&L is updated.
Equity is:

```text
cash + long market value + reserved short margin − short liability
```

Short protection is validated as `SL > entry` and `TP < entry`. The existing
quote monitor uses ask-side thresholds (`ask >= SL`, `ask <= TP`) and records
gap-through prices without filling at the protection level. Repeated ticks and
restarts remain idempotent through deterministic exit intents and the existing
fill key.

Short entries are intraday-only and use the configured session cutoff fields
`short_entry_cutoff` and `forced_short_exit_time`. At forced close, a fresh ask
is required; stale data does not invent a cover price. The runtime covers open
shorts through PaperBroker only.

The `short_positions` table is additive. Long delivery tax lots and settlement
logic are not applied to short entries. Candidates remain empty and live FYERS
orders remain disabled.
