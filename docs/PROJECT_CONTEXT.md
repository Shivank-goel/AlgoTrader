# Stable project context

- Event-driven quantitative research and trading platform with the lifecycle:
  research → validation → shadow → qualified → controlled live.
- The registry contains 206 historical trials. No strategy currently meets the
  deployment criteria at ₹10,000; do not weaken that conclusion to force profitability.
- FYERS/NSE is the selected equity execution direction. Dhan is retained as legacy;
  Delta remains a separate crypto runtime.
- Deterministic risk always sits between strategy and execution. Strategies and
  research agents cannot call brokers directly.
- Live trading is disabled by default. Agents cannot bypass risk, change gates after
  seeing results, promote strategies to live, or place live orders autonomously.
- UNKNOWN submissions require reconciliation before retry. Duplicate-order protection
  and local/broker position reconciliation are mandatory.
- Failed experiments remain recorded. Research, datasets, code/config assumptions,
  costs, execution assumptions and results must be reproducible and auditable.
- Backtests require point-in-time data, correct timing, realistic fees, spread,
  slippage, sizing and out-of-sample validation. Profitability is an empirical result,
  not a promised software feature.
