# Dynamic shadow sizing (Phase 5)

The previous shadow scheduler accepted strategy-provided target quantities as
fixed requests. PaperBroker then enforced cash, liquidity and the existing
₹2,000 order cap. It did not scale a new request from current equity.

`DynamicShadowSizer` now provides an explicit, reusable sizing boundary. It
reads the persisted PaperBroker account (`paper_valuation.equity`, falling
back to cash only before a mark) and calculates:

```text
risk_budget = current_equity × risk_per_trade_fraction × risk_multiplier
quantity_by_risk = floor(risk_budget / (reference_price − stop_loss))
final_quantity = min(strategy request, risk, cash, exposure, liquidity)
```

All quantities are whole shares/lots. Cash includes configured buffer and
FYERS fees/slippage through `maximum_buy_quantity`; exposure uses current
marked portfolio value and symbol/position/gross caps. A maximum open-position
limit and minimum trade value can reject a request. Decisions are emitted as
`shadow_sizing` events and carried in the intent's `sizing` provenance.

Policy values live in [config/fyers_sizing.yaml](../config/fyers_sizing.yaml)
and are risk controls, not optimized parameters. The scheduler applies this
only when explicitly given a current quote, protection plan, and
`DynamicShadowSizer`; otherwise existing requested-target behavior remains
compatible. A zero result is skipped without changing equity. PaperBroker
revalidates actual-fill risk against the recorded risk budget and rejects a
materially worse fill.

Current candidates remain empty, qualified semantics are unchanged, and live
execution is disabled.
