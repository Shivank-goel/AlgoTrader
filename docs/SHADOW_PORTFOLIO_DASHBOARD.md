# Shadow Portfolio dashboard (Phase 8)

The existing FYERS operations page now includes a dedicated **Shadow
Portfolio** panel at the top. It is read-only and clearly states paper/shadow
mode, disabled live execution, disabled broker orders, active simulation ID and
starting capital.

The panel projects persisted data only:

- current equity, cash, realized/unrealized P&L and drawdown;
- open long and short positions;
- completed trade observations with exit reason, net P&L and review state;
- side-level performance and grouped forward-evidence counts.

The dashboard API additionally exposes `short_positions`, `equity_history`,
`performance`, `trade_forward_observations`, and `trade_forward_summary` under
`/api/fyers/dashboard`. Equity history is sourced from authoritative
`equity_checkpoints`; trade economics are sourced from completed
`trade_forward_observations`. Missing benchmark data remains unavailable and
is never shown as zero.

No BUY, SELL, CLOSE, reset, qualification-approval, or live-enable control is
present. Existing authenticated operational controls (recorder, backup and
entry halt) remain separate. Open the panel through the existing SSH tunnel:

```text
http://127.0.0.1:8000
```

Production currently has no activated candidates, so empty trade/evidence
tables are expected and are rendered as “No records.”
